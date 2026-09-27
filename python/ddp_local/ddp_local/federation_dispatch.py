"""App 侧联邦派发/对账：本地许可门先于外发，中心结果持久投影到工作区。

P5-INTERFACES-v3 §3：协调者只接受已批准的探索/执行许可；§5：出站凭据
Fail Closed。本模块是 App 侧唯一把批准范围变成中心写请求的地方：

- 任何要发送的字节都必须先过 `ConsentStore.authorize_dispatch`（唯一取字节路径）；
- 写请求丢响应不自动重放，只落 `outcome_unknown`，由 `reconcile()` 对账，
  显式重试沿用中心幂等键；
- 交付只在 digest 与中心 TaskStatus 随附结果一致时才允许 ack，缺字节保持 pending。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from ddp_core.application.plans import canonical_bytes, content_digest, digest, reject
from ddp_core.application.ports import ApplicationError

from ddp_local.federation_client import (
    CenterConfig,
    CenterFault,
    CenterFederationClient,
    CenterOutcomeUnknown,
    DEFAULT_TIMEOUT,
)

FEDERATION_KIND = "federation_plan"
FEDERATION_COMMAND_KIND = "federation_command"
PHASES = ("exploration", "execution")
CENTER_REFS_ENV = "DDP_FEDERATION_CENTERS"
CENTER_REF_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 50
MAX_COMMANDS = 50
MAX_REFS_BYTES = 65536


def federation_identity(runtime):
    """本监听器只有一个已认证工作区主体，与 plan_http 的身份构造保持一致。"""
    return {
        "environment_id": runtime.store.environment_id,
        "workspace_id": runtime.store.workspace_id,
        "subject": "workspace:" + runtime.store.workspace_id,
    }


def federation_state_id(identity, plan_id):
    owner = canonical_bytes(identity).decode()
    return hashlib.sha256(canonical_bytes([owner, plan_id])).hexdigest()


def _load(runtime, identity, plan_id):
    if not isinstance(plan_id, str) or not 1 <= len(plan_id) <= 512:
        reject("not_found", "plan does not exist in this identity")
    with runtime.store.lock:
        row = runtime.store.db.execute(
            "SELECT body FROM outputs WHERE id=? AND kind=?",
            (federation_state_id(identity, plan_id), FEDERATION_KIND),
        ).fetchone()
    return json.loads(row["body"]) if row else None


def _command_index_id(identity, key):
    owner = canonical_bytes(identity).decode()
    return "federation-command:" + hashlib.sha256(canonical_bytes([owner, key])).hexdigest()


def _save(runtime, identity, plan_id, state, *, command_key=None):
    document = {**state, "updated_at": time.time()}
    body = canonical_bytes(document)
    if len(body) > MAX_STATE_BYTES:
        reject("output_too_large", "federation state exceeds the local transport budget")
    with runtime.store.tx():
        if command_key is not None:
            # Receipt lookup for a key whose response was lost. Recorded in the same
            # transaction as the command, before any request leaves this process.
            index = _command_index_id(identity, command_key)
            row = runtime.store.db.execute(
                "SELECT body FROM outputs WHERE id=? AND kind=?", (index, FEDERATION_COMMAND_KIND),
            ).fetchone()
            if row is not None and json.loads(row["body"]).get("plan_id") != plan_id:
                reject("idempotency_conflict", "same key refers to a different plan")
            if row is None:
                runtime.store.db.execute(
                    "INSERT INTO outputs(id,kind,body,created_at) VALUES(?,?,?,?)",
                    (index, FEDERATION_COMMAND_KIND, canonical_bytes({"plan_id": plan_id}).decode(),
                     time.time()),
                )
        runtime.store.db.execute(
            "INSERT INTO outputs(id,kind,body,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET body=excluded.body, created_at=excluded.created_at",
            (federation_state_id(identity, plan_id), FEDERATION_KIND, body.decode(), time.time()),
        )
    return document


def federation_receipt(runtime, key):
    """Recorded dispatch/ack key -> persisted federation state; None when never admitted."""
    identity = federation_identity(runtime)
    with runtime.store.lock:
        row = runtime.store.db.execute(
            "SELECT body FROM outputs WHERE id=? AND kind=?",
            (_command_index_id(identity, key), FEDERATION_COMMAND_KIND),
        ).fetchone()
    if row is None:
        return None
    return load_federation_state(runtime, json.loads(row["body"])["plan_id"])


def federation_summary(runtime, plan_id):
    """Small mirror summary for plan lists; None before the first dispatch."""
    state = _load(runtime, federation_identity(runtime), plan_id)
    if state is None:
        return None
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    error = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
    reconciled = state.get("reconcile") if isinstance(state.get("reconcile"), dict) else {}
    return {"state": state.get("state"), "phase": state.get("phase"),
            "root_task_id": state.get("root_task_id"),
            "delivery_state": delivery.get("state"), "delivery_verified": delivery.get("verified"),
            "delivery_reason": delivery.get("reason"), "last_error": error.get("code"),
            "reconciled_at": reconciled.get("at"), "updated_at": state.get("updated_at")}


def delivery_result_bytes(runtime, plan_id):
    """Exact canonical bytes of the locally verified result, for independent rehashing."""
    state = load_federation_state(runtime, plan_id)
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    if delivery.get("verified") is not True or not isinstance(delivery.get("result"), dict):
        reject("not_found", "plan has no locally verified delivery result")
    return canonical_bytes(delivery["result"])


def file_delivery_manifest(runtime, plan_id):
    """Host-only: fixed manifest + verified flag + import result for a file delivery."""
    state = load_federation_state(runtime, plan_id)
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    if not isinstance(delivery.get("manifest"), dict):
        reject("not_found", "plan has no file delivery manifest")
    return {"manifest": delivery["manifest"],
            "result_manifest_digest": delivery.get("result_manifest_digest"),
            "verified": delivery.get("verified") is True,
            "bytes_verified": delivery.get("bytes_verified") is True,
            "import_result": delivery.get("import_result")}


def _require_reviewed_endpoint(runtime, identity, plan_id, config):
    """A plan with a reviewed transport may only reach that exact center endpoint."""
    transports = runtime.consents.get(identity, plan_id)["scope"].get("transport_bindings") or []
    if transports and config.endpoint not in {item["endpoint"] for item in transports}:
        reject("policy_denied", "center endpoint differs from the reviewed transport binding")


def _center_client(runtime, identity, plan_id, config, actor_headers):
    """Authorize and commit one fresh reservation at each physical HTTP send."""
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    scope_digest = runtime.consents.get(identity, plan_id)["scope_digest"]

    async def before_send(request):
        path = request.url.path
        discovery_requests = 0
        if request.method == "POST" and path.endswith("/api/v1/federation/scopes"):
            kind = "scope-create"
            discovery_requests = json.loads(request.content).get("max_discovery_requests")
            if type(discovery_requests) is not int or discovery_requests < 0:
                reject("budget_exceeded", "scope creation must declare its discovery bound")
        elif request.method == "GET":
            if "/api/v1/federation/scopes/" in path:
                kind = "scope-targets"
            elif "/api/v1/deliveries/" in path or path.endswith("/bundle"):
                kind = "fetch"
            else:
                kind = "reconcile"
        elif path.endswith(("/api/v1/task-intents", "/api/v1/remote-compute")):
            kind = "intent"
        elif path.endswith("/api/v1/task-plans"):
            kind = "plan"
        elif path.endswith("/approve"):
            kind = "approve"
        elif path.endswith("/ack"):
            kind = "ack"
        elif path.endswith("/cancel"):
            kind = "cancel"
        elif path.endswith("/resume"):
            kind = "resume"
        else:
            kind = "submit"
        # The transport has serialized the entire body, including consent and
        # scope metadata. Business idempotency keys are deliberately not tickets.
        runtime.consents.authorize_control(
            identity, plan_id, ticket="control-" + uuid.uuid4().hex, kind=kind,
            size_bytes=len(request.content), discovery_requests=discovery_requests,
            confirmed_scope_digest=scope_digest)

    return CenterFederationClient(
        config, actor_headers=actor_headers, before_send=before_send)


def load_federation_state(runtime, plan_id):
    """读取本身份的持久联邦投影；没有派发记录时报 not_found。"""
    state = _load(runtime, federation_identity(runtime), plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    return state


def _new_state(plan_id, view):
    return {
        "plan_id": plan_id,
        "scope_digest": view["scope_digest"],
        "phase": None,
        "state": "prepared",
        "root_task_id": None,
        "task_spec_digest": None,
        "center_ref": None,
        "plan_revision": None,
        "center_plan_digest": None,
        "center_plan": None,
        "probes": [],
        "idempotency_key": None,
        "task": None,
        "coverage": None,
        "delivery": None,
        "attempts": [],
        "commands": {},
        "last_error": None,
    }

def _frozen_scope_id(scope):
    ref = ((scope.get("task_spec") or {}).get("resource_scope") or {}).get("scope_ref")
    return ref if isinstance(ref, str) and ref else None


def _allowed_node_ids(scope, center_node):
    recipients = scope.get("exploration", {}).get("allowed_recipients") or []
    ordered = [center_node] + [node for node in recipients if node != center_node]
    if len(ordered) > 100:
        reject("policy_denied", "frozen recipient set exceeds the scope budget")
    return ordered


async def _ensure_frozen_scope(runtime, client, plan_id, view, identity, state, scope, center_node, seed):
    """Create the frozen federation scope inside the approved exploration budget.

    Runs only after exploration approval and before create_intent, reserving
    each HTTP call against the consent cost ledger (pre-reserved N=1+pages for
    directory expansion + reads, conservative and non-refunding). With an
    existing sealed scope_ref, reuses it after verifying its targets are a
    subset of the frozen recipients. Never accepts a renderer-supplied
    manifest as authority: the persisted center mirror is canonical.
    """
    if scope.get("task_spec", {}).get("execution_policy", {}).get("mode") != "trusted_federation":
        return None
    existing = _frozen_scope_id(scope)
    allowed = _allowed_node_ids(scope, center_node)
    base = _submit_key(plan_id, view["scope_digest"], "frozen-scope")
    if existing is not None:
        # Reuse path still costs reads: reserve each target page pre-send.
        cursor, pages, manifest_digest = None, 0, None
        while True:
            
            page = await client.scope_targets(existing, cursor=cursor)
            if not isinstance(page, dict) or page.get("scope_id") != existing:
                raise CenterFault("invalid_response", 0, False)
            for entry in page.get("targets") or []:
                key = (entry or {}).get("target_key") or {}
                if key.get("origin_node_id") not in allowed:
                    reject("policy_denied", "sealed scope reaches nodes outside the frozen recipient set")
            manifest_digest = page.get("manifest_digest") or manifest_digest
            cursor = page.get("next_cursor")
            pages += 1
            if not cursor:
                break
            if pages >= 64:
                raise CenterFault("scope_incomplete", 0, False)
        state["frozen_scope"] = {"scope_id": existing, "manifest_digest": manifest_digest}
        return state["frozen_scope"]
    body = {"operation": "corpus.retrieve", "allowed_node_ids": allowed,
            "page_size": 50, "max_members": 1000,
            "max_discovery_requests": 8, "max_remote_members": len(allowed), "ttl_seconds": 900}
    # The physical request hook atomically reserves create + the bounded
    # internal discovery calls before this request leaves the process.
    envelope = await client.create_scope(body, idempotency_key=base)
    manifest = (envelope or {}).get("manifest") if isinstance(envelope, dict) else None
    if not isinstance(manifest, dict) or manifest.get("scope_id") != (envelope or {}).get("manifest", {}).get("scope_id"):
        raise CenterFault("invalid_response", 0, False)
    for member in manifest.get("expanded_members") or []:
        if (member or {}).get("origin_node_id") not in allowed:
            reject("policy_denied", "frozen scope reaches nodes outside the frozen recipient set")
    state["frozen_scope"] = {"scope_id": manifest["scope_id"], "manifest_digest": manifest.get("manifest_digest"),
                             "manifest": manifest}
    return state["frozen_scope"]



def review_center_plan(runtime, plan_id, *, operation_key):
    """Mint a reviewed child scope bound to center C's persisted plan mirror.

    Body is only the operation key (Idempotency-Key header): reads the
    locally authenticated center mirror (root_task_id/plan/probes persisted
    by exploration dispatch), validates root/writer/spec/digest/ready/allowed
    nodes/expiry against the parent scope, and persists a new prepared child
    plan view with scope.center_execution (no inherited execution consent).
    Same-key replay with the same parent+center digest returns the child;
    a different mirror under the same key conflicts.
    """
    from ddp_core.application.plans import canonical_bytes as _bytes, digest as _digest, instant as _instant, task_plan_digest as _plan_digest, task_spec_digest as _spec_digest, validate_plan as _validate_plan
    import uuid as _uuid
    identity = federation_identity(runtime)
    store = runtime.consents
    owner = store._owner(identity)
    store._key(operation_key)
    with store.tx():
        parent_row = store._row(owner, plan_id)
        parent_view = store._view(parent_row)
        parent_scope = parent_view["scope"]
        if parent_view["revoked"] or parent_view["planning_state"] == "invalidated":
            reject("consent_revoked", "parent scope was revoked or expired; the child is invalid")
        if store._local_only() and parent_scope["task_spec"]["execution_policy"]["mode"] != "local_only":
            reject("local_only", "current workspace policy forbids remote plans")
        store._active(parent_row, parent_view["scope_digest"])
        state = _load(runtime, identity, plan_id)
        if state is None or not isinstance(state.get("root_task_id"), str) or not isinstance(state.get("center_plan"), dict):
            reject("consent_required", "review needs an exploration result; run dispatch exploration first")
        if state.get("center_plan_digest") != state["center_plan"].get("plan_digest"):
            reject("plan_changed", "persisted center mirror digest differs from its content")
        center_plan = state["center_plan"]
        if center_plan.get("plan_digest") != _plan_digest(center_plan):
            reject("plan_changed", "center plan digest differs from its content")
        if center_plan.get("planning_state") not in {"ready", "awaiting_approval", "approved"}:
            reject("plan_changed", "center plan is not in a reviewable revision")
        if center_plan.get("task_spec_digest") != _spec_digest(parent_scope["task_spec"]):
            reject("plan_changed", "center plan was not planned for the approved task")
        now = store.clock()
        if min(_instant(center_plan["valid_until"]), _instant(center_plan["budget"]["deadline"])) <= now:
            reject("consent_expired", "center plan revision has expired")
        transports = {item["transport_ref"]: item for item in parent_scope.get("transport_bindings", [])}
        if "center" not in transports:
            reject("policy_denied", "parent scope has no reviewed center transport")
        center = transports["center"]["recipient_node_id"]
        nodes = _validate_plan(center_plan, parent_scope["task_spec"], local_node_id=store.local_node_id, now=now)
        allowed = set(parent_scope["exploration"]["allowed_recipients"]) | {store.local_node_id, center}
        if nodes - allowed:
            reject("policy_denied", "center plan reaches nodes outside the frozen recipient set")
        if center_plan["root_coordinator_node_id"] != center or center_plan["final_result_writer"] != center:
            reject("policy_denied", "center execution must be rooted at the paired center")
        request = {"action": "review-center", "parent_plan_id": plan_id,
                   "scope_digest": parent_view["scope_digest"],
                   "center_digest": center_plan["plan_digest"], "root_task_id": state["root_task_id"]}
        previous = store._existing_command(owner, operation_key, request)
        if previous:
            return store._view(store._row(owner, previous["plan_id"]))
        child_id = "plan-" + _uuid.uuid4().hex
        child_scope = json.loads(_bytes(parent_scope))
        child_scope["plan"] = json.loads(_bytes(parent_scope["plan"]))
        child_scope["plan"]["plan_id"] = child_id
        child_scope["plan"]["planning_state"] = "ready"
        child_scope["plan"]["revision"] = 1
        child_scope["payload_bindings"] = []
        child_scope["parent_plan_id"] = plan_id
        child_scope["parent_scope"] = parent_scope
        child_scope["center_execution"] = {"root_task_id": state["root_task_id"], "parent_plan_id": plan_id,
                                           "transport_ref": "center", "plan_digest": center_plan["plan_digest"],
                                           "plan": center_plan, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        child_scope["plan"]["task_spec_digest"] = _spec_digest(child_scope["task_spec"])
        child_scope["plan"]["plan_digest"] = _plan_digest(child_scope["plan"])
        store._scope_shape(child_scope)
        result = store._admit(owner, identity, child_scope)
        store._command(owner, operation_key, request, {"plan_id": child_scope["plan"]["plan_id"]})
        return result


def _record(state, action, outcome, code=None, status=None, extra=None):
    attempts = state.setdefault("attempts", [])
    attempts.append({"at": time.time(), "action": action, "outcome": outcome,
                     "code": code, "status": status, "extra": extra})
    del attempts[:-MAX_ATTEMPTS]


def _command_matches(state, key, request):
    commands = state.setdefault("commands", {})
    fingerprint = digest(request)
    previous = commands.get(key)
    if previous is not None and previous != fingerprint:
        reject("idempotency_conflict", "same key refers to a different dispatch request")
    return previous is not None


def _remember_command(state, key, request):
    commands = state.setdefault("commands", {})
    commands[key] = digest(request)
    while len(commands) > MAX_COMMANDS:
        commands.pop(next(iter(commands)))


def _input_bytes(runtime, scope):
    resolver = runtime.consents.input_resolver
    if scope["input_manifest"] and resolver is None:
        reject("input_changed", "fixed inputs require a trusted local snapshot resolver")
    return {item["ref"]: resolver(item["ref"]) for item in scope["input_manifest"]}


def _payload_bytes(runtime, scope, binding):
    """只从已批准 scope 的既有内容取字节；本地没有可信来源的类别 Fail Closed。"""
    kind = binding["payload_kind"]
    if kind in {"query_text", "subquery_text"}:
        query = scope["task_spec"].get("query")
        if not isinstance(query, str):
            reject("input_changed", "this plan has no approved query text to send")
        return query.encode("utf-8")
    if kind == "source_files":
        inputs = _input_bytes(runtime, scope)
        for item in scope["input_manifest"]:
            if item["digest"] == binding["digest"] and item["size_bytes"] == binding["size_bytes"]:
                return inputs[item["ref"]]
        reject("input_changed", "source-file payload does not match a verified snapshot")
    reject("policy_denied", "this local adapter cannot resolve that payload category")


def _current_transport(scope, binding, config):
    if not binding.get("transport_ref"):
        return None
    reviewed = next(
        (item for item in scope.get("transport_bindings", [])
         if item["transport_ref"] == binding["transport_ref"]),
        None,
    )
    if reviewed is None:
        return None
    # Control bytes go to the reviewed center endpoint. Storage bytes go to
    # the reviewed `center-storage` upload origin; the storage endpoint never
    # goes through the credential broker (presigned PUT carries its own
    # query-string credential, `redirect:error`, no Authorization header).
    if binding.get("transport_ref") == "center-storage":
        upload_origin = getattr(config, "upload_origin", None) or getattr(config, "upload_endpoint", None)
        if not isinstance(upload_origin, str) or not upload_origin:
            return dict(reviewed)
        if upload_origin != reviewed["endpoint"]:
            return None
        return dict(reviewed)
    # The endpoint that actually receives the bytes comes from the caller's center
    # configuration. Handing the ledger the reviewed value itself made its
    # "current transport equals approval" comparison true by construction.
    return {**reviewed, "endpoint": config.endpoint}


def _send_key(seed, plan_id, phase, payload_id):
    return "dispatch-" + digest([seed, plan_id, phase, payload_id]).removeprefix("sha256:")[:64]


def _authorize_bindings(runtime, identity, plan_id, view, scope, phase, seed, config):
    bindings = [item for item in scope["payload_bindings"] if item["phase"] == phase]
    if not bindings:
        reject("policy_denied", "no approved payload is bound to this dispatch phase")
    inputs = _input_bytes(runtime, scope)
    verified = {}
    for binding in bindings:
        payload = _payload_bytes(runtime, scope, binding)
        verified[binding["payload_id"]] = runtime.consents.authorize_dispatch(
            identity, plan_id, phase=phase, payload_id=binding["payload_id"],
            recipient_node_id=binding["recipient_node_id"], payload=payload,
            operation_key=_send_key(seed, plan_id, phase, binding["payload_id"]),
            confirmed_scope_digest=view["scope_digest"], current_spec=scope["task_spec"],
            current_plan=scope["plan"], input_bytes=inputs,
            output_location=scope["output_locations"][0], retention=scope["retention"],
            local_only=False, current_transport=_current_transport(scope, binding, config),
        )
    return verified


def _required_id(payload, field):
    if not isinstance(payload, dict) or not isinstance(payload.get(field), str) or not payload[field]:
        raise CenterFault("invalid_response", 0, False)
    return payload[field]


def _submit_key(plan_id, plan_digest, phase):
    return "submit-" + digest([plan_id, plan_digest, phase]).removeprefix("sha256:")[:64]


def _state_from_status(status):
    if not isinstance(status, dict):
        return None
    planning, execution = status.get("planning_state"), status.get("status")
    if execution == "cancelled":
        # 中心 task_status 的显式终态。不映射就会保留旧投影（submitted），
        # 界面永远显示一个再也不会结束的"执行中"。
        return "cancelled"
    if planning in {"ready", "awaiting_approval"}:
        return "planned"
    if planning == "exploring":
        return "exploring"
    if status.get("delivery_state") == "confirmed":
        return "delivered"
    if execution == "succeeded":
        return "succeeded"
    if execution == "failed":
        return "failed"
    if execution in {"queued", "claimed", "running"}:
        return "submitted"
    if planning == "approved":
        return "approved"
    return None


def _merge_delivery(state, status):
    delivery_id, delivery_state = status.get("delivery_id"), status.get("delivery_state")
    if not delivery_id and not delivery_state:
        return
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    if isinstance(delivery_id, str):
        delivery["id"] = delivery_id
    if isinstance(delivery_state, str):
        delivery["state"] = delivery_state
    state["delivery"] = delivery


def _is_file_plan(scope):
    return isinstance(scope, dict) and isinstance(scope.get("task_spec"), dict) and scope["task_spec"].get("operation") == "corpus.parse"


async def _explore_file(runtime, client, plan_id, view, identity, state, seed, operation_key=None):
    """File-compute exploration: persist a waiting-input center record, never queue work.

    The exploration payload is only the bounded descriptor (filename + digest
    + size); the original bytes stay local until the execution phase. The
    center record fixes input digest/size, plan digest and source/target
    identity for the same actor/org. Unknown write outcomes persist as
    `explore_unknown` and reconcile with a read; explicit retries reuse the
    caller's stable idempotency key, never minting a second record.
    """
    scope = view["scope"]
    verified = _authorize_bindings(runtime, identity, plan_id, view, scope, "exploration", seed,
                                   client.config)
    pinned = scope["input_manifest"][0] if len(scope["input_manifest"]) == 1 else None
    if pinned is None:
        reject("input_changed", "file compute binds exactly one pinned input")
    descriptor_binding = next((item for item in scope["payload_bindings"]
                               if item["phase"] == "exploration"
                               and item["payload_kind"] == "query_text"), None)
    if descriptor_binding is not None:
        verified[descriptor_binding["payload_id"]].decode("utf-8")
    identity_block = {"actor": identity["subject"], "workspace": identity["workspace_id"],
                      "environment": identity["environment_id"]}
    body = {"input_sha256": pinned["digest"].removeprefix("sha256:"),
            "input_size": pinned["size_bytes"],
            "plan_digest": scope["plan"]["plan_digest"],
            "source_identity": identity_block,
            "target_identity": {"recipient": descriptor_binding["recipient_node_id"]
                                if descriptor_binding else scope["plan"]["steps"][0]["executor_node_id"]},
            "retention": scope["retention"]}
    
    record = await client.create_remote_compute(
        body, idempotency_key=operation_key or _submit_key(plan_id, scope["plan"]["plan_digest"], "file-waiting"))
    compute_id = _required_id(record, "id")
    if record.get("input_sha256") != body["input_sha256"] or record.get("input_size") != body["input_size"]:
        reject("plan_changed", "center waiting record does not match the approved input")
    state["remote_compute_id"] = compute_id
    state["remote_compute"] = record
    state["root_task_id"] = record.get("upload_id") or state.get("root_task_id")
    state["state"] = "waiting_input"
    _record(state, "create_remote_compute", "ok")
    return _save(runtime, identity, plan_id, state)


async def _explore(runtime, client, plan_id, view, identity, state, seed, scope_manifest):
    if _is_file_plan(view["scope"]):
        return await _explore_file(runtime, client, plan_id, view, identity, state, seed)
    scope = view["scope"]
    verified = _authorize_bindings(runtime, identity, plan_id, view, scope, "exploration", seed,
                                   client.config)
    spec = dict(scope["task_spec"])
    center_node = spec["execution_policy"].get("coordinator_ref")
    center_budget = runtime.consents.reserve_center_budget(
        identity, plan_id, recipient_node_id=center_node,
        confirmed_scope_digest=view["scope_digest"])
    # 中心 `validate_exploration_consent` 要求 TaskSpec 引用**这一份**探索许可。
    # 本地 prepare 只收未授权的 spec（consent_refs 全空），approve 发出许可却不
    # 回写 spec；这里在发出前把引用绑上。consent_refs 不进 task_spec_digest，
    # 所以已批准的计划摘要不变。执行引用由中心审批时写回，不在这里伪造。
    exploration = view["consents"]["exploration"]
    spec["consent_refs"] = {**(spec.get("consent_refs") or {}),
                            "exploration": exploration["consent_id"]}
    # 发出去的 query 必须就是 authorize_dispatch 校验过并返回的那份字节。
    query_binding = next((item for item in scope["payload_bindings"]
                          if item["phase"] == "exploration"
                          and item["payload_kind"] == "query_text"), None)
    if query_binding is not None:
        spec["query"] = verified[query_binding["payload_id"]].decode("utf-8")
    frozen = None
    if scope_manifest is not None:
        reject("policy_denied", "dispatch never accepts a renderer-supplied scope manifest")
    if scope["task_spec"].get("execution_policy", {}).get("mode") == "trusted_federation":
        transports = {item["transport_ref"]: item for item in scope.get("transport_bindings", [])}
        center_node = transports["center"]["recipient_node_id"] if "center" in transports else None
        if center_node is None:
            reject("policy_denied", "trusted scope needs its reviewed center transport")
        frozen = await _ensure_frozen_scope(runtime, client, plan_id, view, identity, state, scope, center_node, seed)
        state = _load(runtime, identity, plan_id) or state
    manifest_arg = (frozen or {}).get("manifest") if frozen else None
    
    intent = await client.create_intent(spec, exploration, manifest_arg, budget=center_budget)
    root = _required_id(intent, "root_task_id")
    state["root_task_id"] = root
    state["task_spec_digest"] = intent.get("task_spec_digest")
    _record(state, "create_intent", "ok")
    
    plan = await client.create_plan(root)
    if not isinstance(plan, dict):
        raise CenterFault("invalid_response", 0, False)
    state["center_plan"] = plan
    state["center_plan_digest"] = plan.get("plan_digest")
    state["plan_revision"] = plan.get("revision")
    probes = plan.get("probes")
    state["probes"] = list(probes)[:1000] if isinstance(probes, list) else []
    state["state"] = "planned" if plan.get("planning_state") in {"ready", "awaiting_approval"} else "exploring"
    _record(state, "create_plan", "ok")
    return _save(runtime, identity, plan_id, state)

async def _execute_file(runtime, client, plan_id, view, identity, state, seed):
    """File-compute execution reservation: idempotent per plan, never double-charged.

    `_authorize_bindings` rehashes the local snapshot against the approved
    manifest; any mutation fails closed as `input_changed` before a byte moves.
    The actual multipart upload travels the control `/api/uploads` channel with
    purpose=`temporary_compute` (owned by the desktop host upload path), so
    this ledger step only records the binding contract: the same center record,
    the same actor, the same input digest/size.

    Execution only validates the grant. Every multipart attempt reserves its
    actual bytes immediately before sending; re-dispatching this local command
    cannot consume or refund transfer costs. The first execution fixes the
    host-kept key, and subsequent executions must retain that identity.
    Host journal persists the create key/uploadId; recovery only GETs
    reconcile/missing parts, never mints a second task. An old SDK receipt is
    never privilege: every external action re-queries transfer/authorize first.
    """
    scope = view["scope"]
    compute_id = state.get("remote_compute_id")
    if not isinstance(compute_id, str) or not compute_id:
        reject("consent_required", "file execution needs a waiting remote compute; run dispatch exploration first")
    if state.get("execution_authorized"):
        # Explicit re-dispatch: re-validate everything, charge nothing new.
        _authorize_bindings(runtime, identity, plan_id, view, scope, "execution", seed, client.config)
        state["phase"] = "execution"
        if state.get("state") not in ("uploading", "submitted", "content_verified",
                                      "content_verifying", "waiting_input"):
            state["state"] = "uploading"
        _record(state, "authorize_file_execution_revalidated", "ok")
        return _save(runtime, identity, plan_id, state)
    _authorize_bindings(runtime, identity, plan_id, view, scope, "execution", seed, client.config)
    key = state.get("idempotency_key") or _submit_key(plan_id, scope["plan"]["plan_digest"], "file-execution")
    state["idempotency_key"] = key
    state["execution_authorized"] = True
    state["phase"] = "execution"
    state["state"] = "uploading"
    _record(state, "authorize_file_execution", "ok")
    return _save(runtime, identity, plan_id, state)
TRANSFER_ACTIONS = ("create", "resume", "part", "finalize")


def authorize_file_transfer(runtime, plan_id, config, *, action, operation_key,
                            upload_id=None, offset=None, length=None):
    """Fixed restricted transfer authorization for the host upload loop.

    The host owns the real transfer loop (file pick/snapshot/credential broker/
    connection, same multipart protocol as `apps/web/src/api/uploads.ts`: POST
    `/api/uploads` -> PUT presigned part URLs serially -> POST finalize). It
    MUST call this before every upload/resume action. Each call revalidates:
    execution approval still present, plan not revoked/invalidated/expired,
    pinned bytes unchanged (rehash), endpoint equals the reviewed transport,
    and the center record is not terminal. Every call is audited in `attempts`
    with its byte length. Each real action charges one request + its bytes on
    the root-shared cost_ledger; a retransmit with a fresh ticket is charged
    again, while the identical ticket replays free without re-sending.
    Every actual send uses a fresh operation_key; resume only re-authorizes
    missing ranges. Returns a fixed host-only ticket
    with no credential, path or URL (the renderer never sees it). The ledger
    never PUTs bytes itself: there is exactly one uploader (the host).
    """
    if action not in TRANSFER_ACTIONS:
        reject("invalid_plan", "unknown file transfer action")
    if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 128:
        reject("invalid_key", "an idempotency key of 1-128 characters is required")
    identity = federation_identity(runtime)
    view = runtime.consents.get(identity, plan_id)
    if view["revoked"] or view["planning_state"] == "invalidated":
        reject("consent_revoked", "approval was revoked; prepare and approve a new plan")
    if "execution" not in view["consents"]:
        reject("consent_required", "file transfer needs execution approval")
    scope = view["scope"]
    if not _is_file_plan(scope):
        reject("invalid_plan", "transfer authorization is only for file plans")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    compute_id = state.get("remote_compute_id")
    if not isinstance(compute_id, str) or not compute_id:
        reject("consent_required", "file transfer needs a waiting remote compute")
    if state.get("state") in ("delivered", "cancelled", "expired", "failed"):
        reject("plan_changed", "transfer record is terminal")
    pinned = scope["input_manifest"][0] if len(scope["input_manifest"]) == 1 else None
    if pinned is None:
        reject("input_changed", "file compute binds exactly one pinned input")
    inputs = _input_bytes(runtime, scope)
    content = inputs.get(pinned["ref"])
    if content is None or content_digest(content) != pinned["digest"] or len(content) != pinned["size_bytes"]:
        reject("input_changed", "current input bytes differ from approved snapshot")
    if action == "part":
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
            reject("invalid_plan", "part range must be a non-negative offset and positive length")
        if offset + length > pinned["size_bytes"]:
            reject("input_changed", "part range exceeds the pinned input size")
    elif offset is not None or length is not None:
        reject("invalid_plan", "offset/length are only for part transfers")
    if upload_id is not None and (not isinstance(upload_id, str) or not upload_id or len(upload_id) > 128):
        reject("invalid_plan", "upload_id must be a bounded string")
    if upload_id is not None and upload_id != state.get("upload_id") and state.get("upload_id") is not None:
        reject("idempotency_conflict", "upload binding differs from the recorded upload")
    # Sole cost basis is the root-shared cost_ledger (charged below via
    # authorize_transfer): every real action with a fresh ticket charges one
    # request + its bytes. attempts/file_covered stay as idempotency/audit
    # state only, never a second budget deduction.
    request = {"action": "file-transfer", "transfer": action, "plan_id": plan_id,
               "scope_digest": view["scope_digest"], "endpoint": config.endpoint,
               "upload_id": upload_id, "offset": offset, "length": length}
    stored = runtime.store.version(pinned["ref"])
    storage_binding = next((item for item in scope.get("transport_bindings", [])
                            if item.get("transport_ref") == "center-storage"), None)
    storage_endpoint = storage_binding["endpoint"] if isinstance(storage_binding, dict) else None
    configured = getattr(config, "upload_origin", None)
    upload_origin = configured or storage_endpoint
    if upload_origin != storage_endpoint:
        reject("policy_denied", "upload origin differs from the reviewed storage binding")
    ticket = {"plan_id": plan_id, "remote_compute_id": compute_id,
              "input_ref": pinned["ref"], "filename": stored["filename"],
              "input_sha256": pinned["digest"].removeprefix("sha256:"),
              "input_size": pinned["size_bytes"],
              "recipient_node_id": scope["plan"]["steps"][0]["executor_node_id"],
              "retention": scope["retention"], "action": action,
              "upload_id": upload_id if upload_id is not None else state.get("upload_id"),
              "upload_origin": upload_origin}
    # Match the host's fixed upload protocol, including serialized metadata.
    # Order does not affect UTF-8 length; both encoders use compact JSON and
    # unescaped Unicode. The original bytes are charged only by actual parts.
    if action == "create":
        body = {"filename": stored["filename"], "size": pinned["size_bytes"],
                "mime": "application/pdf", "sha256": ticket["input_sha256"],
                "purpose": "temporary_compute", "remote_compute_id": compute_id}
        action_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    elif action == "finalize":
        action_bytes = 2
    else:
        action_bytes = length if action == "part" else 0
    if _command_matches(state, operation_key, request):
        return ticket
    # Pre-send atomic reserve on the root-shared ledger happens BEFORE any
    # byte moves: a fresh ticket charges, the identical ticket replays free,
    # a different body under the same ticket conflicts. Limit/expiry/consent
    # refusals therefore happen before the network, never after.
    runtime.consents.authorize_transfer(
        identity, plan_id, ticket="transfer:" + action + ":" + operation_key,
        action=action, size_bytes=action_bytes, confirmed_scope_digest=view["scope_digest"])
    _remember_command(state, operation_key, request)
    if upload_id is not None and state.get("upload_id") is None:
        state["upload_id"] = upload_id
    _record(state, "authorize_file_transfer:" + action, "ok",
            extra={"length": length or 0})
    state = _save(runtime, identity, plan_id, state, command_key=operation_key)
    return {**ticket, "upload_id": state.get("upload_id")}


async def _execute(runtime, client, plan_id, view, identity, state, seed):
    if _is_file_plan(view["scope"]):
        return await _execute_file(runtime, client, plan_id, view, identity, state, seed)
    scope = view["scope"]
    if "center_execution" in scope:
        return await _execute_child(runtime, client, plan_id, view, identity, state, seed)
    root = state.get("root_task_id")
    if not isinstance(root, str) or not root:
        reject("consent_required", "submit needs an exploration result; run dispatch exploration first")
    center_digest = state.get("center_plan_digest")
    if isinstance(center_digest, str) and center_digest != scope["plan"]["plan_digest"]:
        # The execution consent names the reviewed revision. A center that planned a
        # different one would receive consent for a plan the user never saw: refuse
        # before reserving budget or sending anything, and keep the reason visible.
        reject("plan_changed", "center planned a different revision than the one approved")
    _authorize_bindings(runtime, identity, plan_id, view, scope, "execution", seed, client.config)
    center_plan = state.get("center_plan") if isinstance(state.get("center_plan"), dict) else {}
    plan_digest = state.get("center_plan_digest") or scope["plan"]["plan_digest"]
    if center_plan.get("planning_state") != "approved":
        
        approved = await client.approve(root, plan_digest, view["consents"]["execution"])
        if not isinstance(approved, dict):
            raise CenterFault("invalid_response", 0, False)
        state["center_plan"] = approved
        state["center_plan_digest"] = approved.get("plan_digest") or plan_digest
        state["plan_revision"] = approved.get("revision", state.get("plan_revision"))
        plan_digest = state["center_plan_digest"]
        _record(state, "approve", "ok")
    key = state.get("idempotency_key") or _submit_key(plan_id, plan_digest, "execution")
    state["idempotency_key"] = key
    
    status = await client.submit_task(root, plan_digest, key)
    if not isinstance(status, dict):
        raise CenterFault("invalid_response", 0, False)
    state["task"] = status
    state["state"] = _state_from_status(status) or "submitted"
    _record(state, "submit_task", "ok")
    return _save(runtime, identity, plan_id, state)


async def _execute_child(runtime, client, plan_id, view, identity, state, seed):
    """Approve/submit the exact reviewed C revision; never the local transport plan.

    The child scope carries no payloads (the query already left during parent
    exploration). Only the approve/submit control requests are authorized, and
    only against the bound C digest. Local source-policy/transport guards stay:
    dispatch_plan already enforced the reviewed endpoint and cost ledger.
    """
    scope = view["scope"]
    center = scope["center_execution"]
    consent = view["consents"].get("execution") or {}
    bound = (consent.get("center_execution") or {}).get("plan_digest") or center.get("plan_digest")
    if center.get("plan", {}).get("plan_digest") != bound or center.get("plan_digest") != bound:
        reject("plan_changed", "execution approval must name the exact reviewed center revision")
    if state.get("center_plan_digest") not in (None, bound):
        reject("plan_changed", "center has a new revision that requires fresh review")
    _require_reviewed_endpoint(runtime, identity, scope.get("parent_plan_id") or plan_id, client.config)
    parent = runtime.consents.get(identity, scope["parent_plan_id"])
    if parent["revoked"] or parent["planning_state"] == "invalidated":
        reject("consent_revoked", "parent scope was revoked or expired; the child is invalid")
    approved = center["plan"] if center["plan"].get("planning_state") == "approved" else None
    if approved is None:
        
        approved = await client.approve(center["root_task_id"], bound, view["consents"]["execution"])
        if not isinstance(approved, dict):
            raise CenterFault("invalid_response", 0, False)
        if approved.get("plan_digest") != bound:
            reject("plan_changed", "center approved a different revision than the one reviewed")
        state["center_plan"] = approved
        state["center_plan_digest"] = bound
        _record(state, "approve", "ok")
    key = state.get("idempotency_key") or _submit_key(plan_id, bound, "execution")
    state["idempotency_key"] = key
    state["root_task_id"] = center["root_task_id"]
    
    status = (await client.resume(center["root_task_id"])
              if center["plan"]["revision"] > 1 else
              await client.submit_task(center["root_task_id"], bound, key))
    if not isinstance(status, dict):
        raise CenterFault("invalid_response", 0, False)
    state["task"] = status
    state["state"] = _state_from_status(status) or "submitted"
    _record(state, "submit_task", "ok")
    return _save(runtime, identity, plan_id, state)


async def dispatch_plan(runtime, plan_id, config, *, phase, operation_key=None,
                        actor_headers=None, scope_manifest=None, center_ref=None):
    """执行一个已批准阶段的中心派发，返回持久化的派发状态。

    - `exploration`：`create_intent` + `create_plan`，持久中心 plan revision 与 probes；
    - `execution`：必要时 `approve`，再以稳定幂等键 `submit_task`；
    - 每次实际发送前对 phase 内每个 payload 调 `ConsentStore.authorize_dispatch`；
    - 中心写请求结果未知时先落 `outcome_unknown` 再原样抛出，等 `reconcile()`。
    """
    if phase not in PHASES:
        reject("invalid_plan", "dispatch phase must be exploration or execution")
    identity = federation_identity(runtime)
    view = runtime.consents.get(identity, plan_id)
    if "center_execution" in view["scope"] and phase == "exploration":
        reject("policy_denied", "a reviewed child never re-explores; approve execution only")
    if view["revoked"]:
        reject("consent_revoked", "approval was revoked; prepare and approve a new plan")
    if phase not in view["consents"]:
        reject("consent_required", "this dispatch phase has no explicit user approval")
    if "center_execution" not in view["scope"]:
        _require_reviewed_endpoint(runtime, identity, plan_id, config)
    state = _load(runtime, identity, plan_id) or _new_state(plan_id, view)
    state["phase"] = phase
    state["center_ref"] = center_ref
    request = {"plan_id": plan_id, "phase": phase, "scope_digest": view["scope_digest"],
               "endpoint": config.endpoint, "center_ref": center_ref}
    if operation_key is not None:
        if _command_matches(state, operation_key, request):
            return state
        _remember_command(state, operation_key, request)
        # 先落命令再出网：崩溃后同键重放只返回状态，不产生无记录的第二次发送。
        _save(runtime, identity, plan_id, state, command_key=operation_key)
    seed = operation_key or uuid.uuid4().hex
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        if phase == "exploration":
            state = await _explore(runtime, client, plan_id, view, identity, state, seed, scope_manifest)
        else:
            state = await _execute(runtime, client, plan_id, view, identity, state, seed)
    except CenterFault as exc:
        unknown = isinstance(exc, CenterOutcomeUnknown)
        state["last_error"] = {"code": exc.code, "status": exc.status, "retryable": exc.retryable}
        state["state"] = (
            "explore_unknown" if phase == "exploration" else "submit_unknown"
        ) if unknown else "failed"
        _record(state, phase, "unknown" if unknown else "rejected", exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    except ApplicationError as exc:
        _record(state, "authorize:" + phase, "rejected", exc.code)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()
    state["last_error"] = None
    return _save(runtime, identity, plan_id, state)


async def _reconcile_file(runtime, plan_id, config, identity, state, *, actor_headers=None):
    """只读远端计算权威状态；绝不重放创建/上传/取消/确认写请求。"""
    compute_id = state.get("remote_compute_id")
    if not isinstance(compute_id, str) or not compute_id:
        state["reconcile"] = {"at": time.time(), "result": "no_remote_compute"}
        return _save(runtime, identity, plan_id, state)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        
        record = await client.remote_compute(compute_id)
        if not isinstance(record, dict):
            raise CenterFault("invalid_response", 0, False)
        if record.get("id") != compute_id:
            raise CenterFault("invalid_response", 0, False)
        state["remote_compute"] = record
        status = record.get("status")
        mapped = {"waiting_input": "waiting_input", "content_verifying": "content_verifying",
                  "content_verified": "content_verified", "running": "submitted",
                  "succeeded": "succeeded", "failed": "failed", "expired": "expired",
                  "cancelled": "cancelled", "acked": "delivered"}.get(status)
        if mapped:
            state["state"] = mapped
        manifest = record.get("manifest")
        if isinstance(manifest, dict):
            delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
            delivery["manifest"] = manifest
            state["delivery"] = delivery
        state["reconcile"] = {"at": time.time(), "result": "ok"}
        _record(state, "reconcile", "ok")
        return _save(runtime, identity, plan_id, state)
    except CenterFault as exc:
        _record(state, "reconcile", "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected",
                exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()


async def _merge_task_status(state, status, client, root):
    if not isinstance(status, dict) or status.get("root_task_id") != root:
        raise CenterFault("invalid_response", 0, False)
    state["task"] = status
    derived = _state_from_status(status)
    if derived:
        state["state"] = derived
    if isinstance(status.get("plan_digest"), str):
        state["center_plan_digest"] = status["plan_digest"]
    if type(status.get("plan_revision")) is int:
        state["plan_revision"] = status["plan_revision"]
    if (status.get("planning_state") == "ready"
            and state.get("center_plan", {}).get("plan_digest") != status.get("plan_digest")):
        state["center_plan"] = await client.read_plan(root)
    _merge_delivery(state, status)


async def resume_plan(runtime, plan_id, config, *, operation_key, actor_headers=None):
    """Explicitly resume a known root; a changed graph remains unapproved."""
    identity = federation_identity(runtime)
    view = runtime.consents.get(identity, plan_id)
    runtime.consents._key(operation_key)
    if _is_file_plan(view["scope"]):
        reject("policy_denied", "file transfers use their recorded upload recovery")
    if view["revoked"]:
        reject("consent_revoked", "approval was revoked")
    if "execution" not in view["consents"]:
        reject("consent_required", "resume needs the previous execution approval")
    state = _load(runtime, identity, plan_id)
    if state is None or not isinstance(state.get("root_task_id"), str):
        reject("not_found", "plan has no submitted center task")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    root = state["root_task_id"]
    request = {"action": "resume", "root_task_id": root,
               "scope_digest": view["scope_digest"], "endpoint": config.endpoint}
    if _command_matches(state, operation_key, request):
        return state
    _remember_command(state, operation_key, request)
    state["state"] = "resume_unknown"
    _save(runtime, identity, plan_id, state, command_key=operation_key)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        await _merge_task_status(state, await client.resume(root), client, root)
        state["last_error"] = None
        _record(state, "resume", "ok")
    except CenterFault as exc:
        state["last_error"] = {"code": exc.code, "status": exc.status,
                               "retryable": exc.retryable}
        _record(state, "resume", "unknown" if isinstance(exc, CenterOutcomeUnknown)
                else "rejected", exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    except ApplicationError as exc:
        _record(state, "authorize:resume", "rejected", exc.code)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()
    return _save(runtime, identity, plan_id, state)


async def reconcile(runtime, plan_id, config, *, actor_headers=None):
    """只读中心权威状态/覆盖账本并更新本地投影；绝不重放任何写请求。"""
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    view = runtime.consents.get(identity, plan_id)
    if _is_file_plan(view["scope"]):
        return await _reconcile_file(runtime, plan_id, config, identity, state,
                                     actor_headers=actor_headers)
    root = state.get("root_task_id")
    if not isinstance(root, str) or not root:
        state["reconcile"] = {"at": time.time(), "result": "no_root_task"}
        return _save(runtime, identity, plan_id, state)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        
        status = await client.task(root)
        await _merge_task_status(state, status, client, root)
        try:
            
            coverage = await client.coverage(root)
            if isinstance(coverage, dict):
                state["coverage"] = coverage
        except CenterFault as exc:
            state["coverage_error"] = {"code": exc.code, "status": exc.status}
        state["reconcile"] = {"at": time.time(), "result": "ok"}
        _record(state, "reconcile", "ok")
        return _save(runtime, identity, plan_id, state)
    except CenterFault as exc:
        _record(state, "reconcile", "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected",
                exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()


async def _fetch_file_delivery(runtime, plan_id, config, identity, state, *, actor_headers=None):
    """File-compute delivery: fixed manifest contract only; bytes verify in host.

    The center manifest fixes source/version/output hash where
    `delivery.result_manifest_digest` is `sha256:` of the actual ZIP bytes
    (never a JSON receipt hashed as ZIP). The host streams the ZIP with
    resume, hashes the complete bytes, and only after full-hash match plus
    atomic local import sets `bytes_verified` via `note_file_bytes_verified`.
    Until then `verified` stays false and `confirm` refuses. Expired TTL never
    displays as saved locally.

    Resumable path: one workspace-owned partial file
    (`delivery-<plan_id>.<64hex>.part`) holds only durable complete 1MiB
    chunks. Each Range chunk is validated (206 Content-Range/Content-Length/
    ETag/X-Output-SHA256 against the fixed digest and requested bounds) before
    it is appended and fsynced. A 200 full response is never appended onto
    partial bytes. Transport failure keeps durable complete chunks; a rebuilt
    runtime resumes at the persisted offset. Wrong range/length/header/digest
    never imports or acks. Import happens only after the reassembled bytes
    match the fixed manifest digest, atomically, exactly once per digest.
    Only this transfer's partial is cleaned on expiry/cancel/mismatch; already
    imported versions are never deleted.
    """
    from ddp_core.bundle import MAX_ARCHIVE
    from ddp_local.remote_compute import (
        DOWNLOAD_CHUNK_BYTES, append_complete_chunk, discard_partial,
        import_verified_bundle, partial_identity, partial_path,
        verify_output_bytes,
    )
    compute_id = state.get("remote_compute_id")
    if not isinstance(compute_id, str) or not compute_id:
        reject("not_found", "plan has no remote compute to fetch")
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        record = await client.remote_compute(compute_id)
        if not isinstance(record, dict) or record.get("id") != compute_id:
            raise CenterFault("invalid_response", 0, False)
        state["remote_compute"] = record
        manifest = record.get("manifest")
        delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
        digest = record.get("result_manifest_digest")
        if isinstance(manifest, dict) and isinstance(manifest.get("output_sha256"), str):
            digest = "sha256:" + manifest["output_sha256"]
        if not isinstance(manifest, dict) or not isinstance(digest, str):
            delivery["verified"] = False
            delivery["reason"] = "result_unavailable"
            state["delivery"] = delivery
        else:
            delivery["manifest"] = manifest
            delivery["result_manifest_digest"] = digest
            state["delivery"] = delivery
            state = _save(runtime, identity, plan_id, state)
            name = partial_identity(plan_id, digest)
            # A single concurrent writer per partial: an exclusive non-blocking
            # fd lock fails closed when another fetch holds this transfer.
            target = partial_path(runtime, name)
            try:
                fcntl.flock(target, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(target)
                raise ApplicationError("task_in_progress", "delivery fetch is already running") from exc
            try:
                with runtime.store.lock:
                    offset = os.fstat(target).st_size
                    total = None
                    try:
                        while True:
                            chunk, total = await client.download_range(
                                compute_id, start=offset,
                                end=offset + DOWNLOAD_CHUNK_BYTES - 1,
                                output_sha256=digest.removeprefix("sha256:"),
                                total=total)
                            offset = append_complete_chunk(
                                target, chunk, expected_offset=offset,
                                manifest_digest=digest)
                            if len(chunk) < DOWNLOAD_CHUNK_BYTES:
                                break
                    except CenterFault as exc:
                        if exc.code in ("result_unavailable", "delivery_expired", "not_found",
                                        "precondition_failed", "invalid_range",
                                        "range_not_satisfiable", "result_manifest_mismatch"):
                            delivery["verified"] = False
                            delivery["bytes_verified"] = False
                            delivery["reason"] = exc.code
                            delivery["received_bytes"] = offset
                            state["delivery"] = delivery
                            _record(state, "fetch_delivery", "pending", exc.code, exc.status)
                            return _save(runtime, identity, plan_id, state)
                        raise
                    # Reassemble only from the durable partial: seek back to
                    # zero, stream the persisted prefix, and rehash the
                    # complete bytes. A 200 full response path does not exist
                    # here, so a full body can never append onto partial bytes.
                    os.lseek(target, 0, os.SEEK_SET)
                    staged = bytearray()
                    while True:
                        piece = os.read(target, 65536)
                        if not piece:
                            break
                        staged += piece
                        if len(staged) > MAX_ARCHIVE:
                            reject("result_unavailable", "delivery body is missing or over budget")
                    raw = bytes(staged)
                try:
                    checked = verify_output_bytes(raw, digest)
                    stored = import_verified_bundle(
                        runtime, checked,
                        operation_key="import:" + plan_id + ":" + digest.removeprefix("sha256:")[:32])
                except Exception as exc:
                    code = getattr(exc, "code", None) or "result_manifest_mismatch"
                    delivery["verified"] = False
                    delivery["bytes_verified"] = False
                    delivery["reason"] = code if isinstance(code, str) else "result_manifest_mismatch"
                    delivery["received_bytes"] = len(raw)
                    state["delivery"] = delivery
                    _record(state, "fetch_delivery", "rejected", delivery["reason"])
                    # Bytes that fail the fixed manifest can never validate on
                    # retry: discard only this transfer's partial so the next
                    # fetch restarts cleanly. Imported versions are untouched.
                    discard_partial(runtime, name)
                    return _save(runtime, identity, plan_id, state)
                delivery["bytes_verified"] = True
                delivery["verified"] = True
                delivery.pop("reason", None)
                delivery["received_bytes"] = len(raw)
                if isinstance(stored, dict):
                    delivery["import_result"] = {
                        key: stored[key] for key in ("version_id", "resource_id", "id") if key in stored}
                state["delivery"] = delivery
                discard_partial(runtime, name)
            finally:
                os.close(target)
        _record(state, "fetch_delivery",
                "ok" if delivery.get("verified") else "pending", delivery.get("reason"))
        return _save(runtime, identity, plan_id, state)
    except CenterFault as exc:
        _record(state, "fetch_delivery",
                "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected",
                exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()


def note_file_bytes_verified(runtime, plan_id, result_manifest_digest, *, import_result=None):
    """Host-only: mark complete ZIP bytes hashed and atomically imported.

    Called by the session-authenticated host after it streamed the delivery
    ZIP, matched `sha256:` of the complete bytes against
    `delivery.result_manifest_digest`, atomically imported the Bundle, and
    persisted the local version. Only then may `confirm` ack. `import_result`
    (local version/resource identity) is retained for the UI; it never leaves
    the local ledger as a credential.
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    if delivery.get("result_manifest_digest") != result_manifest_digest:
        reject("plan_changed", "verified digest differs from the fixed manifest")
    delivery["bytes_verified"] = True
    delivery["verified"] = True
    delivery.pop("reason", None)
    if import_result is not None:
        delivery["import_result"] = import_result
    state["delivery"] = delivery
    _record(state, "fetch_delivery", "ok")
    return _save(runtime, identity, plan_id, state)


async def fetch_delivery(runtime, plan_id, config, *, actor_headers=None):
    """下载交付字节、本地校验摘要、持久投影；**校验通过前绝不 ack**。

    流程与判据（计划 §8.3）：

    1. 交付 id 缺失时先读一次中心任务状态补齐（首次 fetch / 旧投影）；
    2. `GET /api/v1/deliveries/{id}` 取 `{state, result_manifest_digest, result}`；
    3. `content_digest(canonical_bytes(result))` 必须等于声明的
       `result_manifest_digest` —— 对不上（被篡改/被替换）就记
       `result_manifest_mismatch`，`verified=false`，**不落 result、不 ack**；
    4. 中心 410 `delivery_expired` -> 本地状态 `expired` + 原因，绝不显示
       "已保存本地"；
    5. 校验通过才把结果文档持久到本地投影并置 `verified=true`，后续
       `confirm_delivery` 才可能向中心 ack。

    File-compute plans (`corpus.parse`) branch to `_fetch_file_delivery`:
    fixed manifest bytes are verified before any ack, hash mismatch never
    imports, expired TTL never displays as saved locally.
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    view = runtime.consents.get(identity, plan_id)
    if _is_file_plan(view["scope"]):
        return await _fetch_file_delivery(runtime, plan_id, config, identity, state,
                                          actor_headers=actor_headers)
    root = state.get("root_task_id")
    if not isinstance(root, str) or not root:
        reject("not_found", "plan has no center task to fetch")
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        if not isinstance(delivery.get("id"), str) or not delivery["id"]:
            
            status = await client.task(root)
            if not isinstance(status, dict):
                raise CenterFault("invalid_response", 0, False)
            state["task"] = status
            _merge_delivery(state, status)
            delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
        delivery_id = delivery.get("id")
        if not isinstance(delivery_id, str) or not delivery_id:
            delivery["verified"] = False
            delivery["reason"] = "delivery_id_missing"
            state["delivery"] = delivery
            _record(state, "fetch_delivery", "pending", "delivery_id_missing")
            return _save(runtime, identity, plan_id, state)
        try:
            
            body = await client.delivery(delivery_id)
        except CenterFault as exc:
            if exc.code == "delivery_expired":
                # 过期件永远不是"已保存"：本地状态与原因都要落库。
                delivery["verified"] = False
                delivery["state"] = "expired"
                delivery["reason"] = "delivery_expired"
                state["delivery"] = delivery
            elif exc.code == "delivery_not_found":
                # 中心暂时没有这份交付（或投影过期）：保持 pending、如实记原因，
                # 绝不据此 ack；下次对账/重试可以再取。
                delivery["verified"] = False
                delivery["reason"] = "delivery_not_found"
                state["delivery"] = delivery
                _record(state, "fetch_delivery", "pending", exc.code, exc.status)
                return _save(runtime, identity, plan_id, state)
            raise
        if not isinstance(body, dict):
            raise CenterFault("invalid_response", 0, False)
        received_state = body.get("state")
        if isinstance(received_state, str):
            delivery["state"] = received_state
        if received_state == "expired":
            delivery["verified"] = False
            delivery["reason"] = "delivery_expired"
            state["delivery"] = delivery
            _record(state, "fetch_delivery", "expired")
            return _save(runtime, identity, plan_id, state)
        digest_value = body.get("result_manifest_digest")
        result = body.get("result")
        if not isinstance(result, dict) or not isinstance(digest_value, str):
            delivery["verified"] = False
            delivery["reason"] = "result_unavailable"
        else:
            actual = content_digest(canonical_bytes(result))
            if actual != digest_value:
                delivery["verified"] = False
                delivery["reason"] = "result_manifest_mismatch"
                delivery["received_digest"] = actual
            else:
                delivery["verified"] = True
                delivery["result"] = result
                delivery["result_manifest_digest"] = digest_value
                delivery.pop("reason", None)
        state["delivery"] = delivery
        _record(state, "fetch_delivery",
                "ok" if delivery.get("verified") else "pending", delivery.get("reason"))
        return _save(runtime, identity, plan_id, state)
    except CenterFault as exc:
        _record(state, "fetch_delivery",
                "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected",
                exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()


async def _confirm_file_delivery(runtime, plan_id, delivery_id, result_manifest_digest, config,
                                 identity, state, *, actor_headers=None, operation_key=None):
    """File-compute ack: only a locally verified fixed manifest digest is confirmed.

    `delivery_id` is the remote compute id; the digest must equal the verified
    manifest output hash recorded by the host import path. Same-key replays
    return stored state; a new key may re-send the center idempotent ack after
    a lost reply. Expired TTL never becomes confirmed.
    """
    if state.get("remote_compute_id") != delivery_id:
        reject("not_found", "delivery does not belong to this plan")
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    if delivery.get("state") == "confirmed":
        return state
    if not delivery.get("bytes_verified") or not delivery.get("verified") or delivery.get("result_manifest_digest") != result_manifest_digest:
        reject("plan_changed", "refusing to confirm a delivery whose bytes were not verified locally")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    
    
    request = {"action": "ack-file", "delivery_id": delivery_id, "digest": result_manifest_digest}
    if operation_key is not None and _command_matches(state, operation_key, request):
        return state
    if operation_key is not None:
        _remember_command(state, operation_key, request)
        state = _save(runtime, identity, plan_id, state, command_key=operation_key)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        receipt = await client.ack_remote_compute(delivery_id, result_manifest_digest)
    except CenterFault as exc:
        _record(state, "ack_delivery",
                "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected", exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()
    ack_state = receipt.get("status") if isinstance(receipt, dict) else None
    if ack_state != "acked":
        ack_state = receipt.get("state") if isinstance(receipt, dict) else None
    if ack_state == "acked" or ack_state == "confirmed":
        state["delivery"] = {**delivery, "state": "confirmed", "confirmed_at": time.time()}
        state["state"] = "delivered"
        _record(state, "ack_delivery", "ok")
    else:
        mapped = ack_state if ack_state in ("pending", "expired", "cancelled", "failed") else "pending"
        updated = {**delivery, "state": mapped}
        if mapped == "expired":
            updated["verified"] = False
            updated["reason"] = "delivery_expired"
        else:
            updated["reason"] = "ack_not_confirmed"
        state["delivery"] = updated
        _record(state, "ack_delivery",
                "expired" if mapped == "expired" else "rejected", "ack_not_confirmed")
    return _save(runtime, identity, plan_id, state)


async def cancel_remote_compute(runtime, plan_id, config, *,
                                  actor_headers=None, operation_key=None):
    """Operable file-compute cancel: calls the center cancel, saves the truth.

    Allowed even after local revoke (reconcile stays available), but never
    sends original bytes again: revoke blocks every future transfer/authorize
    while this path only issues the center cancel for the persisted
    remote_compute_id. Same-key replays return stored state; a new key may
    re-send after a lost reply. Transport failures stay `unknown` (raised),
    never mapped to `cancelled`: only the center's authoritative receipt (or a
    later reconcile read) moves the mirror to cancelled.
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    view = runtime.consents.get(identity, plan_id)
    if not _is_file_plan(view["scope"]):
        reject("invalid_plan", "cancel_remote_compute is only for file plans")
    compute_id = state.get("remote_compute_id")
    if not isinstance(compute_id, str) or not compute_id:
        reject("not_found", "plan has no remote compute to cancel")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    if state.get("state") == "cancelled":
        return state
    request = {"action": "cancel-file", "plan_id": plan_id,
               "scope_digest": view["scope_digest"], "endpoint": config.endpoint,
               "remote_compute_id": compute_id}
    if operation_key is not None and _command_matches(state, operation_key, request):
        return state
    if operation_key is not None:
        _remember_command(state, operation_key, request)
        state = _save(runtime, identity, plan_id, state, command_key=operation_key)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        record = await client.cancel_remote_compute(compute_id)
    except CenterFault as exc:
        _record(state, "cancel_remote_compute",
                "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected",
                exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()
    if not isinstance(record, dict) or record.get("id") != compute_id:
        _record(state, "cancel_remote_compute", "rejected", "invalid_response")
        _save(runtime, identity, plan_id, state)
        raise CenterFault("invalid_response", 0, False)
    state["remote_compute"] = record
    if record.get("status") == "cancelled":
        state["state"] = "cancelled"
        _record(state, "cancel_remote_compute", "ok")
    else:
        _record(state, "cancel_remote_compute", "rejected",
                record.get("status") or "ack_not_confirmed")
    return _save(runtime, identity, plan_id, state)


async def confirm_delivery(runtime, plan_id, delivery_id, result_manifest_digest, config, *,
                           actor_headers=None, operation_key=None):
    """本地校验通过后才向中心 ack；digest 不符或不曾校验就拒绝，交付保持 pending。

    ack 的回执状态才是确认的判据：只有 `state=confirmed` 才落本地 confirmed，
    `state=expired` 落本地 expired，其它/缺失状态保持 pending 并记原因 ——
    HTTP 200 本身不构成确认（中心对过期件也回 200）。

    File-compute plans (`corpus.parse`) branch to `_confirm_file_delivery`:
    only the locally verified fixed manifest digest is confirmed, lost and
    duplicate acks replay safely, expired TTL never becomes confirmed.
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    if _is_file_plan(runtime.consents.get(identity, plan_id)["scope"]):
        return await _confirm_file_delivery(runtime, plan_id, delivery_id, result_manifest_digest,
                                            config, identity, state, actor_headers=actor_headers,
                                            operation_key=operation_key)
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    request = {"action": "ack", "delivery_id": delivery_id, "digest": result_manifest_digest}
    if operation_key is not None and _command_matches(state, operation_key, request):
        # Same key: this ack was already recorded (and possibly sent). Report stored
        # state; a new key may re-send the center's idempotent ack after a lost reply.
        return state
    if delivery.get("state") == "confirmed" and delivery.get("id") == delivery_id:
        return state
    if delivery.get("id") != delivery_id:
        reject("not_found", "delivery does not belong to this plan")
    if delivery.get("state") != "pending":
        reject("plan_changed", "delivery is not pending")
    if not delivery.get("verified") or delivery.get("result_manifest_digest") != result_manifest_digest:
        reject("plan_changed", "refusing to confirm a delivery whose bytes were not verified locally")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    
    
    if operation_key is not None:
        _remember_command(state, operation_key, request)
        state = _save(runtime, identity, plan_id, state, command_key=operation_key)
    client = _center_client(runtime, identity, plan_id, config, actor_headers)
    try:
        receipt = await client.ack_delivery(delivery_id, result_manifest_digest)
    except CenterFault as exc:
        _record(state, "ack_delivery",
                "unknown" if isinstance(exc, CenterOutcomeUnknown) else "rejected", exc.code, exc.status)
        _save(runtime, identity, plan_id, state)
        raise
    finally:
        await client.aclose()
    # **200 不等于确认。** ack 端点是幂等的，TTL 到期的交付会回 200 +
    # state=expired；只看 HTTP 码就把本地标成 confirmed，等于把中心明确说
    # "已失效"的结果显示成"已保存本地"。只有回执本身 state=confirmed 才是确认；
    # expired 按 expired 落库并保留原因，其余状态一律不写 confirmed。
    ack_state = receipt.get("state") if isinstance(receipt, dict) else None
    if ack_state == "confirmed":
        state["delivery"] = {**delivery, "state": "confirmed", "confirmed_at": time.time()}
        _record(state, "ack_delivery", "ok")
    else:
        mapped = ack_state if ack_state in ("pending", "transferring", "expired",
                                            "not_requested") else "pending"
        updated = {**delivery, "state": mapped}
        if mapped == "expired":
            updated["verified"] = False
            updated["reason"] = "delivery_expired"
        else:
            updated["reason"] = "ack_not_confirmed"
        state["delivery"] = updated
        _record(state, "ack_delivery",
                "expired" if mapped == "expired" else "rejected", "ack_not_confirmed")
    return _save(runtime, identity, plan_id, state)


def workspace_directory(runtime):
    row = runtime.store.db.execute("PRAGMA database_list").fetchone()
    path = row[2] if row is not None and len(row) > 2 else ""
    if not path:
        reject("not_found", "workspace database has no filesystem path")
    return Path(path).parent


def load_center_ref(runtime, center_ref):
    """center_ref → 本地登记的 `{endpoint, credential, ...}`。

    优先读工作区 0600 的 `federation-centers.json`，再用环境变量
    `DDP_FEDERATION_CENTERS` 兜底；凭据只进内存，绝不回显或入库。
    """
    if not isinstance(center_ref, str) or not CENTER_REF_PATTERN.fullmatch(center_ref):
        reject("invalid_key", "center_ref must be a bounded lowercase identifier")
    entries = {}
    path = workspace_directory(runtime) / "federation-centers.json"
    if path.is_symlink():
        reject("unsafe_path", "center configuration cannot be a symlink")
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            reject("policy_denied", "center configuration must be private (mode 0600)")
        if path.stat().st_size > MAX_REFS_BYTES:
            reject("input_too_large", "center configuration is too large")
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            reject("invalid_plan", "center configuration is not valid JSON")
        if not isinstance(entries, dict):
            reject("invalid_plan", "center configuration must be a JSON object")
    raw = os.environ.get(CENTER_REFS_ENV)
    if raw:
        try:
            fallback = json.loads(raw)
        except ValueError:
            reject("invalid_plan", "DDP_FEDERATION_CENTERS is not valid JSON")
        if not isinstance(fallback, dict):
            reject("invalid_plan", "DDP_FEDERATION_CENTERS must be a JSON object")
        for name, entry in fallback.items():
            entries.setdefault(name, entry)
    entry = entries.get(center_ref)
    if not isinstance(entry, dict):
        reject("not_found", "center_ref is not configured in this workspace")
    return entry


def resolve_center_ref(runtime, center_ref):
    entry = load_center_ref(runtime, center_ref)
    endpoint, credential = entry.get("endpoint"), entry.get("credential")
    if not isinstance(endpoint, str) or not isinstance(credential, str):
        reject("invalid_plan", "center_ref entry requires endpoint and credential strings")
    upload_origin = entry.get("upload_origin", entry.get("upload_endpoint"))
    if upload_origin is not None and not isinstance(upload_origin, str):
        reject("invalid_plan", "center_ref upload origin must be a string")
    return CenterConfig(
        endpoint=endpoint,
        credential=credential,
        timeout_seconds=entry.get("timeout_seconds", DEFAULT_TIMEOUT),
        allow_loopback=entry.get("allow_loopback", False),
        upload_origin=upload_origin,
    )


def resolve_center(runtime, *, center_ref=None, endpoint=None, credential=None, timeout_seconds=None,
                   upload_origin=None, upload_endpoint=None):
    """请求体可以内联 endpoint/credential，也可以给 center_ref 让本地配置解析。

    只要给了 center_ref 就以本地登记为准（内联值被忽略），避免请求体用同名
    引用顶替管理员登记的端点。storage origin 同理：host 配对元数据固定，
    renderer 永不提供 URL；内联 upload origin 仅供 host 内部调用。
    """
    if center_ref is not None:
        return resolve_center_ref(runtime, center_ref)
    if endpoint is None or credential is None:
        reject("invalid_plan", "center requires endpoint and credential, or a configured center_ref")
    origin = upload_origin if upload_origin is not None else upload_endpoint
    return CenterConfig(
        endpoint=endpoint,
        credential=credential,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT,
        allow_loopback=False,
        upload_origin=origin,
    )
