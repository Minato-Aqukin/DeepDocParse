"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.collection_models import Collection
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationRequest,
    FederationTaskEvent,
)
from ddp_contracts.enums import FEDERATION_TASK_OPERATION_VALUES
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, func, or_, select
from ddp_corpus.models import as_aware, new_id, utcnow
import base64
from ddp_corpus import catalog, federation, federation_budget
from ddp_core.application import (
    coverage as coverage_kernel,
    plans,
    routing,
    wiki as wiki_kernel,
)
from datetime import datetime, timedelta, timezone
import hashlib
import json
from sqlalchemy.orm import load_only
import re

from ddp_corpus.federation_tasks.common import (
    EVIDENCE_BYTES_PER_TARGET,
    GENERATION_OPERATIONS,
    GENERATION_TOKEN_BUDGET,
    RETRIEVAL_OPERATION,
    SCOPE_TTL_SECONDS,
    WIKI_OPERATION,
    _EVENT_INTENT,
    _append_event,
    _entry_from_row,
    _enumeration_state,
    _instant,
    _load_request,
    _manifest_digest_python,
    _recorded_conflicts,
    _revocation_sweep,
    _scope_identity,
    _status_output,
    _ts,
    _validate_manifest,
)
from ddp_corpus.federation_tasks.consent import (validate_exploration_consent)
from ddp_corpus.federation_tasks.targets import (_all_targets)

async def _local_manifest(session: AsyncSession, actor: Actor, *, consent: dict,
                          now: datetime) -> dict:
    """site_public / local_only 的本地枚举：把已发布集合封存成 ScopeManifest。

    `catalog.visible_catalog` 会**静默跳过**读不出成员快照的已发布集合
    （成员被撤权/删除）。跳过不等于不存在 —— 这里把差集记进
    `unexpanded_subtrees` 并把 enumeration_state 降为 partial，
    否则"没能去看"会被当成"那里没有资料"。
    """
    node = federation.local_node_id()
    valid_until = min(datetime.fromtimestamp(plans.instant(consent["valid_until"]), timezone.utc),
                      now + timedelta(seconds=SCOPE_TTL_SECONDS))
    descriptors, _readiness = await catalog.visible_catalog(session, actor, node, valid_until)
    published = list(await session.scalars(select(Collection.id).where(
        Collection.organization_id == actor.organization_id,
        Collection.publication == "published").order_by(Collection.id)))
    seen = {descriptor["collection_id"] for descriptor in descriptors}
    members = sorted((descriptor["origin_node_id"], descriptor["collection_id"],
                      RETRIEVAL_OPERATION) for descriptor in descriptors)
    unexpanded = [{"node_id": node, "reason": "denied"}
                  for collection_id in published if collection_id not in seen]
    revision_material = plans.canonical_bytes(
        sorted((descriptor["collection_id"], descriptor["revision"], descriptor["index_revision"])
               for descriptor in descriptors))
    registry_revision = int(hashlib.sha256(revision_material).hexdigest()[:15], 16) + 1
    caller_scope = "sha256:" + hashlib.sha256(plans.canonical_bytes(
        [actor.organization_id, actor.kind, actor.id, actor.principal_id, actor.role])
    ).hexdigest()
    manifest = {
        "schema": "ddp-scope-coverage/1#ScopeManifest",
        "scope_id": "site-" + new_id(), "caller_scope_hash": caller_scope,
        "created_at": _instant(now), "valid_until": _instant(valid_until),
        "registry_revision_vector": [{
            "node_id": node, "registry_revision": registry_revision,
            "fetched_at": _instant(now), "directory_ref": "collections",
            "snapshot_ref": hashlib.sha256(revision_material).hexdigest()[:32]}],
        "expanded_members": [
            {"origin_node_id": origin, "collection_id": collection_id,
             "operation": operation} for origin, collection_id, operation in members],
        "unexpanded_subtrees": unexpanded,
        "enumeration_state": "partial" if unexpanded else "sealed",
    }
    manifest["manifest_digest"] = _manifest_digest_python(manifest)
    coverage_kernel.validate_manifest(manifest)
    return manifest

# ---------------------------------------------------------------------------
# 意图
# ---------------------------------------------------------------------------


def _intent_request_digest(task_spec, exploration_consent, scope_manifest,
                           caller_budget=None) -> str:
    """入参实体摘要。**不能拿持久化后的 scope manifest 比** —— `site_public`
    的本地枚举每次都生成新的 scope_id/时间戳，同键重放会被误判成异实体。"""
    body: dict = {"task_spec": task_spec,
                  "exploration_consent": exploration_consent,
                  "scope_manifest": scope_manifest}
    if caller_budget is not None:
        # 同键不同 caller 预算必须 409；省略时沿用旧三键形状，老 intent 重放不炸。
        body["caller_budget"] = caller_budget
    return plans.digest(body)

def _replay_intent(row: FederationRequest, request_digest: str) -> dict:
    if row.intent_request_digest != request_digest:
        raise APIError(409, "same idempotency key with a different request body",
                       "invalid_request_error", "idempotency_conflict")
    return _intent_output(row)

async def _find_intent_by_key(session: AsyncSession, actor: Actor,
                              idempotency_key: str) -> FederationRequest | None:
    return await session.scalar(select(FederationRequest).where(
        FederationRequest.organization_id == actor.organization_id,
        FederationRequest.actor_id == federation.acting_actor(actor),
        FederationRequest.intent_idempotency_key == idempotency_key))

async def create_intent(session: AsyncSession, actor: Actor, *, task_spec,
                        exploration_consent, scope_manifest=None, budget=None,
                        now: datetime, idempotency_key: str) -> dict:
    """持久任务需求 + 已批准的探索许可。**协调者只校验，不代签。**

    `Idempotency-Key` 是必填的受理锚：同键同实体返回同一个 TaskIntent（固定
    201），同键异实体 409 `idempotency_conflict`。丢响应后的显式重试因此不会
    造出第二个 root task（T80/T81）。键与 `/tasks` 的执行受理键分开存，
    互不干扰。

    scope 的处理按冻结语义：`federation_public` 必须带 ScopeManifest（缺了
    409 discovery_incomplete）；`site_public`/`local_only` 缺省由本节点枚举
    已发布集合；`fixed_resources` 的资源列表本身就是完整分母，不需要 manifest。
    """
    caller = federation_budget.validate_caller_budget(budget)
    request_digest = _intent_request_digest(task_spec, exploration_consent, scope_manifest,
                                            caller_budget=caller)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise APIError(400, "an idempotency key of 1-128 characters is required",
                       "invalid_request_error", "idempotency_key_required")
    # 串行化同键并发，UNIQUE 约束兜底；先查后写才不会把重放变成第二个 task。
    await catalog.lock_key(session, "federation-intent:" + hashlib.sha256(
        plans.canonical_bytes([actor.organization_id, federation.acting_actor(actor),
                               idempotency_key])).hexdigest())
    existing = await _find_intent_by_key(session, actor, idempotency_key)
    if existing is not None:
        return _replay_intent(existing, request_digest)
    if not isinstance(task_spec, dict):
        raise APIError(400, "task_spec must be an object", "invalid_request_error",
                       "protocol_incompatible")
    try:
        plans.validate_spec(task_spec)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    # requirements.query_plan.subqueries 的形状由内核保证
    #（plans.validate_spec -> requirements_query_plan）；下面的摘要 helper
    # 只读校验过的值。
    if task_spec["operation"] not in FEDERATION_TASK_OPERATION_VALUES:
        # 闭集（契约 `federation_task_operation`）：认不出来的 operation 当场拒绝。
        # 放进去的后果不是报错而是静默错义 —— 规划会按"有没有生成能力"给它配一个
        # `answer` 步，于是调用方拿回一个它没要的 RAG 答案。
        raise APIError(400, f"unsupported coordinator operation {task_spec['operation']!r}",
                       "invalid_request_error", "capability_unsupported")
    consent = validate_exploration_consent(exploration_consent, task_spec, now=now)
    kind = task_spec["resource_scope"]["kind"]
    manifest = None
    if kind == "federation_public":
        if not isinstance(scope_manifest, dict):
            raise APIError(409, "federation scope requires a scope manifest",
                           "invalid_request_error", "discovery_incomplete")
        _validate_manifest(scope_manifest, now=now)
        manifest = scope_manifest
    elif scope_manifest is not None:
        _validate_manifest(scope_manifest, now=now)
        manifest = scope_manifest
    elif kind in ("site_public", "local_only"):
        manifest = await _local_manifest(session, actor, consent=consent, now=now)
    scope_id, scope_digest = _scope_identity(task_spec, manifest)
    root_task_id = new_id()
    row = FederationRequest(
        root_task_id=root_task_id, organization_id=actor.organization_id,
        actor_id=federation.acting_actor(actor),
        task_spec_digest=plans.task_spec_digest(task_spec),
        scope_id=scope_id, scope_digest=scope_digest,
        search_mode=task_spec["search_policy"]["mode"], planning_state="draft",
        plan_revision=0, plan_digest="", status="queued",
        retrieval_completeness="not_started", evidence_sufficiency="unknown",
        result_json={}, task_spec_json=task_spec, exploration_consent_json=consent,
        scope_manifest_json=manifest, intent_idempotency_key=idempotency_key,
        intent_request_digest=request_digest, created_at=now, updated_at=now)
    session.add(row)
    try:
        # Ledger creation and event reads can both autoflush the intent's
        # unique key. Keep every flush inside the same replay/conflict guard.
        await federation_budget.ensure_ledger(
            session, root_task_id=root_task_id, organization_id=actor.organization_id,
            caller_budget=caller,
            server_caps=_intent_budget(task_spec, manifest, consent, now=now),
            legacy_result=None, now=now)
        await _append_event(session, root_task_id, _EVENT_INTENT,
                            {"planning_state": "draft", "scope_ref": scope_id,
                             "caller_budget": caller}, now=now)
        await session.commit()
    except IntegrityError:
        # 并发同键：唯一约束替我们仲裁；重放已有行，异实体如实报冲突。
        await session.rollback()
        existing = await _find_intent_by_key(session, actor, idempotency_key)
        if existing is not None:
            return _replay_intent(existing, request_digest)
        raise APIError(409, "concurrent intent for the same idempotency key",
                       "invalid_request_error", "idempotency_conflict") from None
    return _intent_output(row)

def _intent_output(row: FederationRequest) -> dict:
    return {
        "root_task_id": row.root_task_id,
        "task_spec_digest": row.task_spec_digest,
        "planning_state": row.planning_state,
        "status": row.status,
        "task_spec": row.task_spec_json,
        "exploration_consent": row.exploration_consent_json,
        "created_at": _instant(row.created_at),
        "updated_at": _instant(row.updated_at) if row.updated_at else None,
    }

TASK_LIST_LIMIT_MAX = 100

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def _list_item(row: FederationRequest) -> dict:
    """列表项只带需求摘要与状态轴；结果、证据、许可原文要按 id 读（再过一遍可见性）。"""
    # 需求在建任务时已按契约校验过（`plans.validate_spec`）：这几个字段必然存在。
    # 不用 `or ""` 兜底 —— 兜出来的空串本身就违反契约（minLength 1 / enum）。
    spec = row.task_spec_json
    return {
        "root_task_id": row.root_task_id,
        "query": spec.get("query") or "",
        "operation": spec["operation"],
        "scope_kind": spec["resource_scope"]["kind"],
        "search_mode": row.search_mode,
        "status": row.status,
        "planning_state": row.planning_state,
        "retrieval_completeness": row.retrieval_completeness,
        "evidence_sufficiency": row.evidence_sufficiency,
        "delivery_state": row.delivery_state,
        "created_at": _instant(row.created_at),
        "updated_at": _instant(row.updated_at),
    }

def _list_cursor(row: FederationRequest) -> str:
    """不透明游标：创建时刻（微秒）+ root_task_id。键集翻页，插入新任务不会让下一页重复。"""
    # 游标必须保留到微秒：同一秒内创建的相邻任务，精度一丢就会在翻页边界被
    # 跳过或重复。用整数运算，不依赖浮点舍入。
    micros = (as_aware(row.created_at) - _EPOCH) // timedelta(microseconds=1)
    raw = json.dumps([micros, row.root_task_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

#: root_task_id 的形状（`new_id()` 是 32 位十六进制；放宽到契约允许的安全字符）。
#: 游标里的 id 会原样进 SQL 参数：孤立代理字符在 SQLite 上是 UnicodeEncodeError，
#: NUL 在 PostgreSQL 上是 CharacterNotInRepertoire —— 都会变成 500，而契约说是 400。
_ROOT_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

def _parse_list_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        # validate=True：非字母表字符报错而不是被静默丢掉。它仍接受标准字母表的 `+/`，
        # 但合法游标只含数字、ASCII 标点与 `[A-Za-z0-9_-]` 的 id —— 这些字节的 base64
        # 取不到下标 62/63，所以不存在"同一个合法游标的第二种写法"。
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        micros, root_task_id = json.loads(raw)
        if type(micros) is not int or not isinstance(root_task_id, str) \
                or not _ROOT_TASK_ID.fullmatch(root_task_id):
            raise ValueError("cursor fields")
        created = _EPOCH + timedelta(microseconds=micros)
    except (ValueError, TypeError, UnicodeError, OverflowError, OSError):
        # 静默从头开始会让翻页悄悄重复同一批任务 —— 解析不了的游标必须显式失败。
        # （游标没有签名：格式合法但被改过的游标会被接受，只是查的仍是本人的任务。）
        raise APIError(400, "task list cursor is invalid", "invalid_request_error",
                       "invalid_cursor") from None
    return created, root_task_id

async def list_tasks(session: AsyncSession, actor: Actor, *, limit: int,
                     cursor: str | None) -> dict:
    """调用者本人的任务，创建时间倒序。管理员也只列自己的（按 id 仍可读别人的）。"""
    limit = max(1, min(int(limit), TASK_LIST_LIMIT_MAX))
    # 只取列表项要用的列：`result_json` / `plan_json` / 许可与范围原文都可能很大，
    # 整行加载时一页 100 条会拉回几 MB 到十几 MB 用不上的 JSON。
    query = select(FederationRequest).options(load_only(
        FederationRequest.root_task_id, FederationRequest.task_spec_json,
        FederationRequest.search_mode, FederationRequest.status,
        FederationRequest.planning_state, FederationRequest.retrieval_completeness,
        FederationRequest.evidence_sufficiency, FederationRequest.delivery_state,
        FederationRequest.created_at, FederationRequest.updated_at)).where(
        FederationRequest.organization_id == actor.organization_id,
        FederationRequest.actor_id == federation.acting_actor(actor))
    if cursor:
        created, root_task_id = _parse_list_cursor(cursor)
        query = query.where(or_(
            FederationRequest.created_at < created,
            and_(FederationRequest.created_at == created,
                 FederationRequest.root_task_id < root_task_id)))
    rows = list(await session.scalars(query.order_by(
        FederationRequest.created_at.desc(), FederationRequest.root_task_id.desc())
        .limit(limit + 1)))
    page = rows[:limit]
    return {"items": [_list_item(row) for row in page],
            "next_cursor": _list_cursor(page[-1]) if len(rows) > limit else None}

# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


def _root_budget(consent: dict, *, target_count: int, route_hops: int, route_requests: int,
                 deadline: str, generation_ready: bool = False) -> dict:
    """Freeze a bounded allowance before the first planning request.

    Admission, lookup, evidence reads and up to 64 status polls per execution
    all need request capacity — for every direct and every delegated leaf, plus
    each admission along a route (`route_requests`, routing.request_need).
    Capability readiness controls the eventual graph, not whether its root can
    ever allocate generation tokens.
    """
    probes = int(consent["budget"]["max_probe_requests"])
    discovery = int(consent["budget"].get("max_discovery_requests", 0))
    executions = max(1, target_count) + int(generation_ready)
    return {
        "max_requests": min(2**63 - 1, probes + discovery + max(68, route_requests)
                            + 68 * int(generation_ready)),
        "max_bytes": min(2**63 - 1, max(
            4096, int(consent["budget"]["max_egress_bytes"])
            + executions * EVIDENCE_BYTES_PER_TARGET)),
        "max_hops": max(1, route_hops + 2 * int(generation_ready)),
        "deadline": deadline,
        "max_generation_tokens": GENERATION_TOKEN_BUDGET if generation_ready else 0,
        "max_probe_requests": probes,
        "max_egress_bytes": int(consent["budget"]["max_egress_bytes"]),
        "max_discovery_requests": discovery,
    }


def _subquery_digests(task_spec: dict) -> list[str]:
    """Plan-revision-bound subquery digests; default [content_digest(query)]."""
    requirements = task_spec.get("requirements") or {}
    plan = requirements.get("query_plan") if isinstance(requirements, dict) else None
    subqueries = plan.get("subqueries") if isinstance(plan, dict) else None
    if subqueries is None:
        return [plans.content_digest((task_spec.get("query") or "").encode("utf-8"))]
    return [plans.content_digest(subquery.encode("utf-8")) for subquery in subqueries]



def _intent_budget(task_spec, manifest, consent, *, now):
    node = federation.local_node_id()
    targets = _all_targets(task_spec, manifest, node)
    deadline = min(plans.instant(consent["valid_until"]), _ts(now) + SCOPE_TTL_SECONDS)
    if manifest is not None:
        deadline = min(deadline, plans.instant(manifest["valid_until"]))
    node_routes = (manifest or {}).get("node_routes", [])
    budget = _root_budget(
        consent, target_count=len(targets),
        # Direct remote targets plus, per first-hop delegate, its admission and a
        # share deep enough for every relay's strict depth gate (routing.hop_need);
        route_hops=routing.hop_need(targets=targets, coordinator_node_id=node,
                                    node_routes=node_routes),
        route_requests=routing.request_need(targets=targets, coordinator_node_id=node,
                                            node_routes=node_routes),
        deadline=plans.utc_instant(deadline),
        generation_ready=task_spec["operation"] in GENERATION_OPERATIONS)
    # No post-carve floor: hop_need already sizes every relay depth gate, and
    # plan_steps hard-rejects a hops-short pool with budget_exceeded at planning
    # (routing.py). A routed-but-short pool must fail there, never silently grow.
    if task_spec["operation"] == WIKI_OPERATION:
        # Wiki's planner/writer/optional relations share this kernel allowance.
        # Cited answers have a different output profile; never lend their cap to Wiki.
        budget["max_generation_tokens"] = wiki_kernel.DEFAULT_LIMITS["max_output_tokens"]
    return budget

async def read_task(session: AsyncSession, actor: Actor, root_task_id: str) -> dict:
    row = await _load_request(session, actor, root_task_id)
    out = await _status_output(session, row)
    fused = (row.result_json or {}).get("evidence") or []
    if out.get("result") is not None and fused:
        revoked = await _revocation_sweep(session, actor, fused=fused, now=utcnow())
        if revoked is not None:
            # 撤销读路径 fail-closed：证据与派生（answer/wiki）不得因缓存继续暴露。
            failed = {**out["result"], "evidence": [], "answer": None,
                      "answer_reason": revoked, "validation_state": "failed",
                      "wiki": None, "error": revoked}
            out = {**out, "result": failed, "error": revoked}
    return out

async def read_coverage(session: AsyncSession, actor: Actor, root_task_id: str) -> dict:
    row = await _load_request(session, actor, root_task_id)
    ledger_row = await session.get(CoverageLedger, root_task_id)
    rows = list(await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == root_task_id)))
    entries = [_entry_from_row(item, row.scope_id) for item in rows]
    enumeration = ledger_row.enumeration_state if ledger_row is not None \
        else _enumeration_state(row)
    return coverage_kernel.ledger(
        root_task_id=root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
        enumeration_state=enumeration, entries=entries, conflicts=_recorded_conflicts(row))

async def read_events(session: AsyncSession, actor: Actor, root_task_id: str, *,
                      after: int = 0) -> dict:
    await _load_request(session, actor, root_task_id)
    rows = list(await session.scalars(select(FederationTaskEvent).where(
        FederationTaskEvent.root_task_id == root_task_id,
        FederationTaskEvent.seq > max(0, after)).order_by(FederationTaskEvent.seq).limit(500)))
    events = [{"seq": row.seq, "type": row.type, "at": _instant(row.created_at),
               "payload": row.payload or {}} for row in rows]
    next_seq = events[-1]["seq"] if events else max(0, after)
    remaining = await session.scalar(select(func.count()).select_from(FederationTaskEvent).where(
        FederationTaskEvent.root_task_id == root_task_id,
        FederationTaskEvent.seq > next_seq))
    return {"root_task_id": root_task_id, "events": events, "next_seq": next_seq,
            "complete": not remaining}
