"""App 侧联邦派发/对账：本地许可门先于外发，中心结果持久投影到工作区。

P5-INTERFACES-v3 §3：协调者只接受已批准的探索/执行许可；§5：出站凭据
Fail Closed。本模块是 App 侧唯一把批准范围变成中心写请求的地方：

- 任何要发送的字节都必须先过 `ConsentStore.authorize_dispatch`（唯一取字节路径）；
- 写请求丢响应不自动重放，只落 `outcome_unknown`，由 `reconcile()` 对账，
  显式重试沿用中心幂等键；
- 交付只在 digest 与中心 TaskStatus 随附结果一致时才允许 ack，缺字节保持 pending。
"""
from __future__ import annotations

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


def _require_reviewed_endpoint(runtime, identity, plan_id, config):
    """A plan with a reviewed transport may only reach that exact center endpoint."""
    transports = runtime.consents.get(identity, plan_id)["scope"].get("transport_bindings") or []
    if transports and config.endpoint not in {item["endpoint"] for item in transports}:
        reject("policy_denied", "center endpoint differs from the reviewed transport binding")


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


def _record(state, action, outcome, code=None, status=None):
    attempts = state.setdefault("attempts", [])
    attempts.append({"at": time.time(), "action": action, "outcome": outcome,
                     "code": code, "status": status})
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
    if status.get("delivery_state") == "confirmed":
        return "delivered"
    if execution == "succeeded":
        return "succeeded"
    if execution == "failed":
        return "failed"
    if execution == "cancelled":
        # 中心 task_status 的显式终态。不映射就会保留旧投影（submitted），
        # 界面永远显示一个再也不会结束的"执行中"。
        return "cancelled"
    if execution in {"queued", "claimed", "running"}:
        return "submitted"
    if planning == "approved":
        return "approved"
    if planning in {"ready", "awaiting_approval"}:
        return "planned"
    if planning == "exploring":
        return "exploring"
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


async def _explore(runtime, client, plan_id, view, identity, state, seed, scope_manifest):
    scope = view["scope"]
    verified = _authorize_bindings(runtime, identity, plan_id, view, scope, "exploration", seed,
                                   client.config)
    spec = dict(scope["task_spec"])
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
    intent = await client.create_intent(spec, exploration, scope_manifest)
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


async def _execute(runtime, client, plan_id, view, identity, state, seed):
    scope = view["scope"]
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
    if view["revoked"]:
        reject("consent_revoked", "approval was revoked; prepare and approve a new plan")
    if phase not in view["consents"]:
        reject("consent_required", "this dispatch phase has no explicit user approval")
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
    client = CenterFederationClient(config, actor_headers=actor_headers)
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


async def reconcile(runtime, plan_id, config, *, actor_headers=None):
    """只读中心权威状态/覆盖账本并更新本地投影；绝不重放任何写请求。"""
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    root = state.get("root_task_id")
    if not isinstance(root, str) or not root:
        state["reconcile"] = {"at": time.time(), "result": "no_root_task"}
        return _save(runtime, identity, plan_id, state)
    client = CenterFederationClient(config, actor_headers=actor_headers)
    try:
        status = await client.task(root)
        if not isinstance(status, dict):
            raise CenterFault("invalid_response", 0, False)
        state["task"] = status
        derived = _state_from_status(status)
        if derived:
            state["state"] = derived
        if isinstance(status.get("plan_digest"), str):
            state["center_plan_digest"] = status["plan_digest"]
        if type(status.get("plan_revision")) is int:
            state["plan_revision"] = status["plan_revision"]
        _merge_delivery(state, status)
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
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
    _require_reviewed_endpoint(runtime, identity, plan_id, config)
    root = state.get("root_task_id")
    if not isinstance(root, str) or not root:
        reject("not_found", "plan has no center task to fetch")
    delivery = state.get("delivery") if isinstance(state.get("delivery"), dict) else {}
    client = CenterFederationClient(config, actor_headers=actor_headers)
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


async def confirm_delivery(runtime, plan_id, delivery_id, result_manifest_digest, config, *,
                           actor_headers=None, operation_key=None):
    """本地校验通过后才向中心 ack；digest 不符或不曾校验就拒绝，交付保持 pending。

    ack 的回执状态才是确认的判据：只有 `state=confirmed` 才落本地 confirmed，
    `state=expired` 落本地 expired，其它/缺失状态保持 pending 并记原因 ——
    HTTP 200 本身不构成确认（中心对过期件也回 200）。
    """
    identity = federation_identity(runtime)
    state = _load(runtime, identity, plan_id)
    if state is None:
        reject("not_found", "plan has no local federation state")
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
    client = CenterFederationClient(config, actor_headers=actor_headers)
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
    return CenterConfig(
        endpoint=endpoint,
        credential=credential,
        timeout_seconds=entry.get("timeout_seconds", DEFAULT_TIMEOUT),
        allow_loopback=entry.get("allow_loopback", False),
    )


def resolve_center(runtime, *, center_ref=None, endpoint=None, credential=None, timeout_seconds=None):
    """请求体可以内联 endpoint/credential，也可以给 center_ref 让本地配置解析。

    只要给了 center_ref 就以本地登记为准（内联值被忽略），避免请求体用同名
    引用顶替管理员登记的端点。
    """
    if center_ref is not None:
        return resolve_center_ref(runtime, center_ref)
    if endpoint is None or credential is None:
        reject("invalid_plan", "center requires endpoint and credential, or a configured center_ref")
    return CenterConfig(
        endpoint=endpoint,
        credential=credential,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT,
        allow_loopback=False,
    )
