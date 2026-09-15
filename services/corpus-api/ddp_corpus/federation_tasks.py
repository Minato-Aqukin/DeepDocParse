"""P5 协调者：任务需求、规划、审批、执行、覆盖账本与交付回执。

执行权威：工作区计划 v3 §6–§9；接口冻结：`docs/refactor/P5-INTERFACES-v3.md`
§3、§5。这个模块**不复制节点侧实现** —— 本地目标一律经
`federation.run_probe` / `federation.admit` 走同一条检索与证据路径，
本模块只负责编排、外发许可门与账本。

本切片的三条边界：

1. **答案生成可以在协调者本地，也可以整项委托给已就绪的远端执行者。** 协调者
   自己的 `rag.answer.cited` 就绪时保留指派给自己的 `answer` 步骤；未就绪则按
   探索许可与根预算探测候选执行节点，把**有界**证据摘录经 `evidence_excerpts`
   数据边外发（先有计划、后有批准），并校验回传绑定是所发证据 id 的子集。任何
   失败都显式带 `answer=null` 与原因，有证据就不许标失败。
2. **不递归联邦。** 只对 ScopeManifest 的直接成员发请求，不展开 child manifest。
3. **交付字节经 `GET /api/v1/deliveries/{id}` 有界下载并本地校验摘要**；
   确认只在本地校验通过后发生，TTL 到期一律 expired。

覆盖语义（§7.2/§7.4，由 `ddp_core.application.coverage` 判定，本模块只填分子
与分母）：fast 的结局永远 `partial`；exhaustive 只有在 manifest `sealed`、
所有目标 succeeded/有依据排除时才可能是 `complete`；没有证据一律
`insufficient`，不读任何模型自报信心。

执行位置（P5 队列切片起）：受理（`POST /tasks`）与 `resume` 只把
`federation_plan` 排进 `corpus.tasks`，`_execute_plan` 由 corpus-worker 领取；
本地目标经 `federation.admit` 排出的 `federation_execute` 也由 worker 执行，
协调者在 `_local_execution_outcome` 里等它落终态。进程重启不再把已受理的
协调/执行任务永远留在 running（企业边界 7）。
`FEDERATION_EXECUTION_INLINE=true` 恢复请求内执行的旧行为，只给没有 worker
的部署与验收夹具用。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ddp_core.application import coverage as coverage_kernel
from ddp_core.application import plans, routing
from ddp_core.application.ports import ApplicationError
from ddp_contracts.enums import FEDERATED_ANSWER_REASON_VALUES

from ddp_corpus import cache, capabilities, catalog, federation, policy, queue, upstream
from ddp_corpus.collection_models import Collection
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationDelivery,
    FederationProbe,
    FederationRequest,
    FederationTaskEvent,
)
from ddp_corpus.federation_peers import PeerDirectory, PeerUnavailable
from ddp_corpus.models import ResourceVersion, as_aware, new_id, utcnow

#: 取数目标统一用这个 operation 进覆盖账本；与节点能力清单的 operation 同名。
RETRIEVAL_OPERATION = "corpus.retrieve"
#: 固定资源目标的 operation：本节点只对指定版本做授权定位，不把它当集合。
LOCATE_OPERATION = "corpus.locate"
#: fast 的有界候选数（§7.2）。exhaustive 不受它限制。
FAST_CANDIDATE_LIMIT = 8
#: 命中探测复用（P6 缓存回执）时覆盖账本 search_profile 上的可见标记。
#: 它不声称"本次真的探测过" —— 回执本身的 observed_at 才是新鲜度事实。
CACHED_PROBE_PROFILE = "cached_probe_receipt"
#: 本地枚举的 scope 有效期上限：比探索许可短，到期重新枚举。
SCOPE_TTL_SECONDS = 900
#: 交付暂存期。TTL 到期未确认 -> expired，不得再显示"已保存本地"。
DELIVERY_TTL_SECONDS = 86400
#: 远端执行的轮询上限。超时记 `unreachable/peer_execution_timeout` 并**保留**
#: 对端执行（与本地 `local_execution_timeout` 同一语义），本轮不再等；resume
#: 按业务键对账到同一条执行接着等。超时就取消会让这个"可重做"的目标永远
#: 重做不了：对账拿回的是已取消的执行，换代次重新受理又是同键异体 409。
PEER_POLL_DEADLINE_SECONDS = 20.0
PEER_POLL_INTERVAL_SECONDS = 0.05
#: 本地执行的等待上限。本地目标现在也排在持久队列里（`federation_execute`），
#: 协调者在拿到回执后等它落终态；超时记 `local_execution_timeout` 并保留
#: 覆盖缺口，绝不无限挂住协调者任务。
LOCAL_POLL_DEADLINE_SECONDS = 20.0
LOCAL_POLL_INTERVAL_SECONDS = 0.05
#: 每个目标一份检索结果的字节预算（证据信封 + locator，够宽但有限）。
EVIDENCE_BYTES_PER_TARGET = 64 * 1024
#: 答案生成的 token 上限（`RootBudget.max_generation_tokens`）。本地生成或
#: 远端 answer 委托就绪时进根预算；0 表示这份计划不生成。没有上游 tokenizer，
#: 计数用本仓共享的 `ddp_core.tokenize.tokens`（确定性、可复核）；它是**上限
#: 口径**，不是模型侧的真实 token 数。
GENERATION_TOKEN_BUDGET = 1024
#: 生成提示词与结构验收的**唯一实现**在 `federation`（远端执行者与本地生成
#: 共用同一份）。这里保留模块级别名，避免历史引用点漂移。
ANSWER_SYSTEM_PROMPT = federation.ANSWER_SYSTEM_PROMPT
#: 规划 answer 委托时最多探测几个候选执行节点。有界且确定：先按目标出现顺序，
#: 再按稳定排序；预算（`max_probe_requests`）还会先一步封顶。
MAX_ANSWER_CANDIDATES = 8
#: 交付结果文档的字节上限（规范 JSON）。结果本身不含正文摘录，正常远小于它；
#: 超限**不持久化文档、也不截断**，读取端点如实返回 result=null，客户端据此
#: 拒绝确认 —— 静默截断会让本地"校验通过"的哈希对不上真正交付的内容。
DELIVERY_RESULT_MAX_BYTES = 1024 * 1024
#: 这些状态的目标可以在 resume 时补做；denied/unsupported/revoked 需要新授权。
_RETRYABLE_STATES = {"planned", "in_flight", "partial", "failed", "unreachable",
                     "not_attempted"}
#: 事件类型。seq 由追加方在事务内取 max+1，唯一约束兜底。
_EVENT_INTENT = "intent_created"
_EVENT_PLAN_READY = "plan_ready"
_EVENT_APPROVED = "plan_approved"
_EVENT_STARTED = "execution_started"
_EVENT_RESUMED = "task_resumed"
_EVENT_COMPLETED = "task_completed"
_EVENT_FAILED = "task_failed"
_EVENT_CANCELLED = "task_cancelled"
_EVENT_DELIVERY_PENDING = "delivery_pending"
_EVENT_DELIVERY_CONFIRMED = "delivery_confirmed"
_EVENT_DELIVERY_EXPIRED = "delivery_expired"


def _ts(now: datetime) -> float:
    return now.timestamp()


def _instant(value: datetime) -> str:
    return as_aware(value).isoformat()


def _egress_denied(message: str) -> APIError:
    return APIError(403, message, "invalid_request_error", "egress_denied")


def peer_directory(actor: Actor) -> PeerDirectory:
    """按登记的目录建出站客户端。测试 monkeypatch 它注入 ASGI transport。

    目录配置坏掉是部署问题（管理员配错 JSON / endpoint），不是调用方错误：
    503 并说明是哪条登记坏了，而不是 500。
    """
    try:
        return PeerDirectory.from_settings(actor=actor)
    except PeerUnavailable as exc:
        raise APIError(503, str(exc), "server_error", "peer_directory_invalid") from None


# ---------------------------------------------------------------------------
# 许可校验（探索/执行）—— 全部 Fail Closed，失败一律 egress_denied
# ---------------------------------------------------------------------------

_EXPLORATION_FIELDS = {"schema", "consent_id", "granted_by", "granted_at", "valid_until",
                       "egress_mode", "allowed_payload", "allowed_recipients",
                       "trust_domain_revision", "budget"}
_EXECUTION_FIELDS = {"schema", "consent_id", "plan_digest", "granted_by", "granted_at",
                     "valid_until", "allowed_recipients", "allowed_edges",
                     "output_locations", "retention"}
_BUDGET_FIELDS = {"max_probe_requests", "max_egress_bytes", "max_discovery_requests"}


def _valid_node_list(value) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and plans.NODE.fullmatch(item) for item in value)
            and len(set(value)) == len(value))


def validate_exploration_consent(consent, task_spec: dict, *, now: datetime) -> dict:
    """结构 + 绑定校验；**缺、过期、与 TaskSpec 对不上都 403 egress_denied**。

    `trust_domain` 在 P4 密钥交换完成前没有可核验的固定接收方集合，按
    "没有有效许可"处理，而不是静默放行。本地目标不走这个门（它们不出网）。
    """
    if not isinstance(consent, dict) or set(consent) - _EXPLORATION_FIELDS:
        raise _egress_denied("exploration consent is missing or has unknown fields")
    if consent.get("schema") != "ddp-task-probe/1#ExplorationConsent":
        raise _egress_denied("unsupported exploration consent schema")
    if not isinstance(consent.get("consent_id"), str) or not consent["consent_id"].strip():
        raise _egress_denied("exploration consent has no consent_id")
    if not isinstance(consent.get("granted_by"), str) or not consent["granted_by"].strip():
        raise _egress_denied("exploration consent has no granted_by")
    try:
        plans.instant(consent.get("granted_at"))
        valid_until = plans.instant(consent.get("valid_until"))
    except ApplicationError:
        raise _egress_denied("exploration consent has invalid timestamps") from None
    if valid_until <= _ts(now):
        raise _egress_denied("exploration consent has expired")
    mode = consent.get("egress_mode")
    if mode not in ("local_only", "listed_nodes", "trust_domain"):
        raise _egress_denied("unknown exploration egress mode")
    payload = consent.get("allowed_payload")
    if (not isinstance(payload, list) or len(set(payload)) != len(payload)
            or any(item not in plans.PROBE_PAYLOADS for item in payload)):
        raise _egress_denied("exploration consent has invalid allowed_payload")
    recipients = consent.get("allowed_recipients", [])
    if not _valid_node_list(recipients):
        raise _egress_denied("exploration consent has invalid allowed_recipients")
    budget = consent.get("budget")
    if not isinstance(budget, dict) or set(budget) - _BUDGET_FIELDS:
        raise _egress_denied("exploration consent has an invalid budget")
    for key in ("max_probe_requests", "max_egress_bytes"):
        if type(budget.get(key)) is not int or budget[key] < 0:
            raise _egress_denied(f"exploration budget {key} must be a nonnegative integer")
    if "max_discovery_requests" in budget and (
            type(budget["max_discovery_requests"]) is not int
            or budget["max_discovery_requests"] < 0):
        raise _egress_denied("exploration budget max_discovery_requests must be nonnegative")
    if mode == "local_only":
        if payload or recipients or any(budget.values()):
            raise _egress_denied(
                "local_only exploration forbids payloads, recipients and remote budget")
    elif mode == "trust_domain":
        raise _egress_denied("trust_domain exploration has no verifiable recipient set yet")
    elif not recipients:
        raise _egress_denied("listed_nodes exploration requires fixed recipients")
    refs = task_spec.get("consent_refs") or {}
    if refs.get("exploration") != consent["consent_id"]:
        raise _egress_denied("task spec does not reference this exploration consent")
    return consent


def validate_execution_consent(consent, *, now: datetime) -> dict:
    """执行许可的结构与有效期校验；失败 403 egress_denied（缺/过期/形状坏）。"""
    if not isinstance(consent, dict) or set(consent) - _EXECUTION_FIELDS:
        raise _egress_denied("execution consent is missing or has unknown fields")
    if consent.get("schema") != "ddp-plan-admission/1#ExecutionConsent":
        raise _egress_denied("unsupported execution consent schema")
    for key in ("consent_id", "granted_by"):
        if not isinstance(consent.get(key), str) or not consent[key].strip():
            raise _egress_denied(f"execution consent has no {key}")
    if not isinstance(consent.get("plan_digest"), str) \
            or not plans.DIGEST.fullmatch(consent["plan_digest"]):
        raise _egress_denied("execution consent has an invalid plan_digest")
    try:
        plans.instant(consent.get("granted_at"))
        valid_until = plans.instant(consent.get("valid_until"))
    except ApplicationError:
        raise _egress_denied("execution consent has invalid timestamps") from None
    if valid_until <= _ts(now):
        raise _egress_denied("execution consent has expired")
    if not _valid_node_list(consent.get("allowed_recipients")) \
            or not consent["allowed_recipients"]:
        raise _egress_denied("execution consent needs a fixed nonempty recipient set")
    edges = consent.get("allowed_edges")
    if not isinstance(edges, list) or any(not isinstance(item, str) or not item for item in edges) \
            or len(set(edges)) != len(edges):
        raise _egress_denied("execution consent has invalid allowed_edges")
    if consent.get("retention") not in plans.RETENTION:
        raise _egress_denied("execution consent has an invalid retention class")
    if "output_locations" in consent and not isinstance(consent["output_locations"], list):
        raise _egress_denied("execution consent has invalid output_locations")
    return consent


# ---------------------------------------------------------------------------
# ScopeManifest
# ---------------------------------------------------------------------------

def _manifest_digest_python(manifest: dict) -> str:
    return plans.digest({key: value for key, value in manifest.items()
                         if key != "manifest_digest"})


def _go_manifest_digest(manifest: dict) -> str:
    """控制面（Go `FinalizeScope`）用的摘要编码。

    Go 的 `json.Marshal` 按结构体字段顺序序列化、省略空的可选字段，时间戳
    保留原始 RFC3339 文本。这里只做**逐字段重排**、不重新格式化时间 ——
    收到什么字节就按什么字节算，"跨语言摘要"才不会在时间精度上悄悄分叉。

    字段顺序以 `scope-control-format.md` 为准。`child_manifests` 是 omitempty：
    远端展开过的 manifest 才有它，空列表与缺省同一原像。漏掉它会让每个
    展开过远端目录的 federation_public scope 都 409 `plan_changed`。
    """
    body = {
        "schema": manifest["schema"], "scope_id": manifest["scope_id"],
        "caller_scope_hash": manifest["caller_scope_hash"],
        "created_at": manifest["created_at"], "valid_until": manifest["valid_until"],
        "registry_revision_vector": [
            {key: item[key] for key in ("node_id", "registry_revision", "fetched_at",
                                        "directory_ref", "snapshot_ref") if key in item}
            for item in manifest.get("registry_revision_vector", [])],
    }
    children = manifest.get("child_manifests") or []
    if children:
        body["child_manifests"] = [
            {key: item[key] for key in ("node_id", "scope_ref", "enumeration_state")}
            for item in children]
    body.update({
        "expanded_members": [
            {key: item[key] for key in ("origin_node_id", "collection_id", "operation")}
            for item in manifest.get("expanded_members", [])],
        "unexpanded_subtrees": [
            {key: item[key] for key in ("node_id", "reason")}
            for item in manifest.get("unexpanded_subtrees", [])],
        "enumeration_state": manifest["enumeration_state"],
    })
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    raw = (raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
           .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest_digest_matches(manifest: dict) -> bool:
    declared = manifest.get("manifest_digest")
    if not isinstance(declared, str):
        return False
    if declared == _manifest_digest_python(manifest):
        return True
    try:
        return declared == _go_manifest_digest(manifest)
    except (KeyError, TypeError, ValueError):
        return False


def _validate_manifest(manifest: dict, *, now: datetime) -> None:
    try:
        coverage_kernel.validate_manifest(manifest)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    if not _manifest_digest_matches(manifest):
        raise APIError(409, "scope manifest digest does not match its content",
                       "invalid_request_error", "plan_changed")
    if plans.instant(manifest["valid_until"]) <= _ts(now):
        raise APIError(410, "scope manifest has expired", "invalid_request_error",
                       "scope_expired")


def _scope_identity(task_spec: dict, manifest: dict | None) -> tuple[str, str]:
    if manifest is not None:
        return manifest["scope_id"], manifest["manifest_digest"]
    refs = sorted(set(task_spec["resource_scope"].get("resource_refs") or []))
    scope_id = "fixed:" + hashlib.sha256(plans.canonical_bytes(refs)).hexdigest()[:24]
    return scope_id, plans.digest({"resource_refs": refs})


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

def _intent_request_digest(task_spec, exploration_consent, scope_manifest) -> str:
    """入参实体摘要。**不能拿持久化后的 scope manifest 比** —— `site_public`
    的本地枚举每次都生成新的 scope_id/时间戳，同键重放会被误判成异实体。"""
    return plans.digest({"task_spec": task_spec,
                         "exploration_consent": exploration_consent,
                         "scope_manifest": scope_manifest})


def _replay_intent(row: FederationRequest, request_digest: str) -> dict:
    if row.intent_request_digest != request_digest:
        raise APIError(409, "same idempotency key with a different request body",
                       "invalid_request_error", "idempotency_conflict")
    return _intent_output(row)


async def _find_intent_by_key(session: AsyncSession, organization_id: str,
                              idempotency_key: str) -> FederationRequest | None:
    return await session.scalar(select(FederationRequest).where(
        FederationRequest.organization_id == organization_id,
        FederationRequest.intent_idempotency_key == idempotency_key))


async def create_intent(session: AsyncSession, actor: Actor, *, task_spec,
                        exploration_consent, scope_manifest=None, now: datetime,
                        idempotency_key: str) -> dict:
    """持久任务需求 + 已批准的探索许可。**协调者只校验，不代签。**

    `Idempotency-Key` 是必填的受理锚：同键同实体返回同一个 TaskIntent（固定
    201），同键异实体 409 `idempotency_conflict`。丢响应后的显式重试因此不会
    造出第二个 root task（T80/T81）。键与 `/tasks` 的执行受理键分开存，
    互不干扰。

    scope 的处理按冻结语义：`federation_public` 必须带 ScopeManifest（缺了
    409 discovery_incomplete）；`site_public`/`local_only` 缺省由本节点枚举
    已发布集合；`fixed_resources` 的资源列表本身就是完整分母，不需要 manifest。
    """
    request_digest = _intent_request_digest(task_spec, exploration_consent, scope_manifest)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise APIError(400, "an idempotency key of 1-128 characters is required",
                       "invalid_request_error", "idempotency_key_required")
    # 串行化同键并发，UNIQUE 约束兜底；先查后写才不会把重放变成第二个 task。
    await catalog.lock_key(session, "federation-intent:" + hashlib.sha256(
        plans.canonical_bytes([actor.organization_id, idempotency_key])).hexdigest())
    existing = await _find_intent_by_key(session, actor.organization_id, idempotency_key)
    if existing is not None:
        return _replay_intent(existing, request_digest)
    if not isinstance(task_spec, dict):
        raise APIError(400, "task_spec must be an object", "invalid_request_error",
                       "protocol_incompatible")
    try:
        plans.validate_spec(task_spec)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
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
        # 事件追加里的 `SELECT max(seq)` 会触发 autoflush，它必须和 commit 在
        # 同一个 try 里：并发同键时唯一约束会在这里就炸，放在 try 外面则重放/
        # 409 分支永远到不了，裸 IntegrityError 直接变 500（N4）。
        await _append_event(session, root_task_id, _EVENT_INTENT,
                            {"planning_state": "draft", "scope_ref": scope_id}, now=now)
        await session.commit()
    except IntegrityError:
        # 并发同键：唯一约束替我们仲裁；重放已有行，异实体如实报冲突。
        await session.rollback()
        existing = await _find_intent_by_key(session, actor.organization_id, idempotency_key)
        if existing is not None:
            return _replay_intent(existing, request_digest)
        raise APIError(409, "concurrent intent for the same idempotency key",
                       "invalid_request_error", "idempotency_conflict") from None
    return _intent_output(row)


# ---------------------------------------------------------------------------
# 行读写与事件
# ---------------------------------------------------------------------------

async def _load_request(session: AsyncSession, actor: Actor,
                        root_task_id: str) -> FederationRequest:
    row = await session.get(FederationRequest, root_task_id)
    if row is None or row.organization_id != actor.organization_id \
            or (row.actor_id != federation.acting_actor(actor) and not actor.can_manage):
        # 不同用户不能因共用 root 名称读到别人的探测与结果（§7.5）。
        raise APIError(404, "task intent not found", "invalid_request_error", "task_not_found")
    return row


async def _append_event(session: AsyncSession, root_task_id: str, type_: str,
                        payload: dict, *, now: datetime) -> None:
    current = await session.scalar(select(func.max(FederationTaskEvent.seq)).where(
        FederationTaskEvent.root_task_id == root_task_id))
    session.add(FederationTaskEvent(id=new_id(), root_task_id=root_task_id,
                                    seq=int(current or 0) + 1, type=type_,
                                    payload=payload, created_at=now))


async def _commit(session: AsyncSession) -> None:
    """提交协调者写路径；唯一约束冲突映射成 409，而不是裸 500。

    事件流的 `(root_task_id, seq)` 唯一约束是并发兜底。正常路径由每 root 的
    咨询锁串行化，真撞上时答案是"同一任务已有并发写"，不是服务端错误。
    """
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise APIError(409, "concurrent write for the same task", "invalid_request_error",
                       "idempotency_conflict") from None


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


def _status_output(row: FederationRequest) -> dict:
    return {
        "root_task_id": row.root_task_id,
        "status": row.status,
        "planning_state": row.planning_state,
        "plan_revision": row.plan_revision,
        "plan_digest": row.plan_digest or None,
        "task_spec_digest": row.task_spec_digest,
        "search_mode": row.search_mode,
        "retrieval_completeness": row.retrieval_completeness,
        "evidence_sufficiency": row.evidence_sufficiency,
        "execution_consent_ref": row.execution_consent_ref,
        "coverage_ref": row.coverage_ref,
        "delivery_id": row.delivery_id,
        "delivery_state": row.delivery_state,
        "result": _public_result(row.result_json),
        "error": row.error,
        "scope_ref": row.scope_id or None,
        "created_at": _instant(row.created_at),
        "updated_at": _instant(row.updated_at),
    }


# ---------------------------------------------------------------------------
# 目标与候选
# ---------------------------------------------------------------------------

def _all_targets(task_spec: dict, manifest: dict | None, node: str) -> list[dict]:
    if manifest is not None:
        return routing.targets(manifest)
    refs: list[str] = []
    for ref in task_spec["resource_scope"].get("resource_refs") or []:
        if ref not in refs:
            refs.append(ref)
    return [{"origin_node_id": node, "collection_id": ref, "operation": LOCATE_OPERATION}
            for ref in refs]


def _ordered_targets(targets: list[dict]) -> list[dict]:
    """与 `routing._dedup_sorted` 同一顺序：retrieve-{i} 与这个列表逐位对应。"""
    keys = {}
    for target in targets:
        keys[(target["origin_node_id"], target["collection_id"], target["operation"])] = {
            "origin_node_id": target["origin_node_id"],
            "collection_id": target["collection_id"],
            "operation": target["operation"]}
    return [keys[key] for key in sorted(keys)]


def _select_targets(targets: list[dict], task_spec: dict, node: str, *,
                    descriptors: list[dict] | None = None) -> list[dict]:
    """按集合目录摘要给候选定序并截到模式上限（排序实现只在路由内核里）。

    没有摘要（未取到/未授权/预算耗尽）时排序退化为确定性 local_first ——
    摘要只影响顺序与 fast 取谁，**永不删除成员**：穷查仍然枚举全部目标。
    """
    if not targets:
        raise APIError(409, "scope enumerates no retrieval targets", "invalid_request_error",
                       "discovery_incomplete")
    mode = task_spec["search_policy"]["mode"]
    fixed = task_spec["resource_scope"]["kind"] == "fixed_resources"
    limit = len(targets) if mode == "exhaustive_scope" or fixed \
        else min(FAST_CANDIDATE_LIMIT, len(targets))
    try:
        ranked = routing.candidates(
            targets, list(descriptors or []), query=task_spec.get("query") or "", limit=limit,
            ordering=task_spec["search_policy"].get("ordering", "local_first"),
            local_node_id=node)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    return [item["target_key"] for item in ranked]


def _plan_selected_targets(plan: dict, all_targets: list[dict]) -> list[dict]:
    """从已落定的计划恢复被选中的目标；执行阶段不再重排。

    `create_plan` 给每个 retrieve 步写了 `fixed_inputs=["query", "collection:<id>"]`
    （固定资源目标是裸资源 id），`executor_node_id` 是 origin —— 这两个字段把
    步骤映回枚举目标。**不能在这里重跑 `_select_targets`**：计划是按目录摘要
    选出来的，执行阶段没有（也不该重新外发）同一份摘要，重排会选出不同集合；
    而 retrieve 步骤与目标是按计划顺序 zip 的，错位会把一个目标的探测回执
    静默挂到另一个目标上。
    """
    index: dict[tuple[str, str], list[dict]] = {}
    for target in all_targets:
        reference = (target["collection_id"] if target["operation"] == LOCATE_OPERATION
                     else "collection:" + target["collection_id"])
        index.setdefault((target["origin_node_id"], reference), []).append(target)
    for matches in index.values():
        matches.sort(key=lambda item: (item["collection_id"], item["operation"]))
    selected: list[dict] = []
    consumed: dict[tuple[str, str], int] = {}
    for step in plan.get("steps") or []:
        if step.get("operation") != "retrieve":
            continue
        inputs = step.get("fixed_inputs") or []
        if len(inputs) != 2:
            raise APIError(409, "stored plan has no target binding",
                           "invalid_request_error", "plan_changed")
        lookup = (step["executor_node_id"], inputs[1])
        matches = index.get(lookup)
        position = consumed.get(lookup, 0)
        if not matches or position >= len(matches):
            raise APIError(409, "stored plan references a target outside the scope",
                           "invalid_request_error", "plan_changed")
        selected.append(matches[position])
        consumed[lookup] = position + 1
    return selected


def _target_digest(target: dict) -> str:
    return hashlib.sha256(plans.canonical_bytes(target)).hexdigest()


def _target_key(target: dict) -> dict:
    return {"origin_node_id": target["origin_node_id"],
            "collection_id": target["collection_id"], "operation": target["operation"]}


def _steps_by_target(plan: dict, candidates: list[dict]) -> dict:
    retrieve = [step for step in plan["steps"] if step["operation"] == "retrieve"]
    mapping = {}
    for step, target in zip(retrieve, _ordered_targets(candidates), strict=False):
        mapping[(target["origin_node_id"], target["collection_id"], target["operation"])] = step
    return mapping


def _peer_probe_denial(consent: dict, node_id: str) -> str | None:
    """探索许可门：一个字节都不外发时的理由（None 表示允许发）。

    条件按 §6.2：模式必须是 listed_nodes、节点在固定接收方集合里、
    问题类别（query_text/subquery_text）在允许外发的载荷里。三者缺一不发。
    """
    if consent["egress_mode"] != "listed_nodes":
        return "egress_mode:" + str(consent["egress_mode"])
    if node_id not in consent["allowed_recipients"]:
        return "recipient_not_allowed"
    if not ({"query_text", "subquery_text"} & set(consent["allowed_payload"])):
        return "payload_not_allowed"
    return None


def _directory_denial(consent: dict, node_id: str) -> str | None:
    """集合目录读的探索许可门：读远端目录是**元数据外发**，与 Probe 同级审查。

    载荷类别是 `collection_filters`：它描述"我想按哪些集合过滤"，不是问题正文。
    未列入接收方或载荷不允许就不读 —— 排序退化为 local_first，不是错误。
    """
    if consent["egress_mode"] != "listed_nodes":
        return "egress_mode:" + str(consent["egress_mode"])
    if node_id not in consent["allowed_recipients"]:
        return "recipient_not_allowed"
    if "collection_filters" not in consent["allowed_payload"]:
        return "payload_not_allowed"
    return None


def _registry_revisions(manifest: dict | None) -> dict[str, str]:
    """ScopeManifest 的 registry_revision_vector -> {node_id: revision 文本}。

    负面缓存的键分量与探测复用的策略修订都取自这里；manifest 缺该节点的条目
    时**不编造修订号**（返回空映射），调用方据此跳过或保守处理。
    """
    if not manifest:
        return {}
    revisions: dict[str, str] = {}
    for item in manifest.get("registry_revision_vector") or []:
        if not isinstance(item, dict):
            continue
        node_id, revision = item.get("node_id"), item.get("registry_revision")
        if isinstance(node_id, str) and node_id and revision is not None:
            revisions.setdefault(node_id, str(revision))
    return revisions


def _probe_policy_revision(manifest: dict | None, node_id: str) -> str:
    """探测复用的策略修订口径：该节点的目录修订（登记/成员可见性变化的载体）。

    当前 P5 回执不记录 policy_revision（缓存文档已声明这是已知缺口），所以这个
    值只在有记录时才参与比对；无该节点修订时用显式 `unbound` 而不是编一个号。
    """
    revision = _registry_revisions(manifest).get(node_id)
    return f"registry:{node_id}:{revision}" if revision else "registry:unbound"


def _descriptor_index(descriptors: list[dict]) -> dict[tuple[str, str], dict]:
    """描述符列表 -> {(origin, collection): descriptor}；坏条目直接丢弃。

    排序只读这几个字段，坏描述符最多让排序退化，不能让规划失败（对端失约
    不是调用方的错误）。
    """
    index: dict[tuple[str, str], dict] = {}
    for item in descriptors:
        if not isinstance(item, dict):
            continue
        origin, collection = item.get("origin_node_id"), item.get("collection_id")
        if isinstance(origin, str) and origin and isinstance(collection, str) and collection:
            index.setdefault((origin, collection), item)
    return index


async def _gather_descriptors(session: AsyncSession, actor: Actor, *, node: str,
                              all_targets: list[dict], manifest: dict | None,
                              consent: dict, budget: routing.RootBudget,
                              peers: PeerDirectory,
                              valid_until: datetime) -> tuple[list[dict], dict]:
    """计划期集合摘要：本地已发布集合 + 许可允许的远端自发布目录。

    - 本地摘要是本库读，不出网，不受探索许可约束；
    - 远端目录读先过 `_directory_denial`（接收方 + `collection_filters`），
      每页请求先占根预算的 `discovery` 额度（与请求预算共用，T87）；
    - 任一步失败（未授权/超时/对端错误/预算耗尽/目录过大）只意味着该节点没有
      摘要可用，规划继续，排序退化为确定性顺序 —— **绝不因此少枚举一个成员**。
    """
    descriptors: list[dict] = []
    notes: dict = {"local": 0, "remote": {}}
    if manifest is not None:
        try:
            local, _readiness = await catalog.visible_catalog(session, actor, node, valid_until)
        except APIError as exc:
            notes["local"] = exc.code or "catalog_unavailable"
        else:
            descriptors.extend(local)
            notes["local"] = len(local)

    exhausted = {"budget": False}

    def _reserve() -> bool:
        try:
            budget.reserve("discovery")
        except ApplicationError:
            exhausted["budget"] = True
            return False
        return True

    remote_nodes = sorted({target["origin_node_id"] for target in all_targets
                           if target["origin_node_id"] != node})
    for origin in remote_nodes:
        denial = _directory_denial(consent, origin)
        if denial is not None:
            notes["remote"][origin] = denial
            continue
        exhausted["budget"] = False
        try:
            fetched = await peers.collections(origin, reserve=_reserve)
        except PeerUnavailable as exc:
            notes["remote"][origin] = exc.code or "peer_unavailable"
            continue
        # 只采信"这个节点说自己的集合"：对端描述符的 origin 必须就是它自己，
        # 否则一个坏对端可以用别人的 origin 抬高/压低别家目标的排序。
        usable = [item for item in fetched if item.get("origin_node_id") == origin]
        descriptors.extend(usable)
        if exhausted["budget"] and not usable:
            notes["remote"][origin] = "budget_exhausted"
        else:
            notes["remote"][origin] = len(usable)
    return descriptors, notes


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def _probe_key(root_task_id: str, target: dict) -> str:
    return "plan:" + hashlib.sha256(plans.canonical_bytes(
        [root_task_id, target["origin_node_id"], target["collection_id"],
         target["operation"]])).hexdigest()


def _negative_reason(exc: PeerUnavailable) -> str:
    """不可达/被拒观测的可复核理由，前缀就是负面命中时的目标状态。

    只把传输失败与显式 403 记进负面缓存：5xx/4xx 协议错误是系统问题，
    缓存它们等于把"对端坏了"伪装成"对端不可达"并挡住恢复后的重试。
    """
    state = "denied" if exc.status == 403 else "unreachable"
    return state + ":" + (exc.code or "peer_unavailable")


def _negative_state(reason) -> str:
    return "denied" if str(reason).startswith("denied:") else "unreachable"


def _probe_request(*, task_spec: dict, consent_ref: str, scope_ref: str, target: dict,
                   query: str) -> dict:
    # 集合目标与固定资源目标的唯一判据是操作名：manifest 里的 operation 由控制面
    # 枚举参数决定（"search"、"corpus.retrieve" 都可能），本切片一律按集合检索
    # 执行；只有协调者自己构造的 LOCATE_OPERATION 才是固定资源。
    evidence = target["operation"] != LOCATE_OPERATION
    return {
        "schema": "ddp-task-probe/1#ProbeRequest",
        "task_spec_digest": plans.task_spec_digest(task_spec),
        "consent_ref": consent_ref,
        "probe_kind": "evidence_retrieval" if evidence else "resource_locate",
        "target_node_id": target["origin_node_id"],
        "scope_ref": scope_ref,
        "collection_id": target["collection_id"] if evidence else None,
        "query": query,
        "query_digest": plans.content_digest(query.encode("utf-8")),
        "candidate_limit": 8,
    }


def _probe_state(probe: dict) -> str:
    retrieval = probe.get("retrieval") or {}
    if retrieval:
        if retrieval.get("internal_limits"):
            return "partial"
        return str(retrieval.get("status") or "failed")
    readiness = (probe.get("capability_check") or {}).get("readiness")
    return "succeeded" if readiness == "ready" else "failed"


async def _persist_remote_probe(session: AsyncSession, actor: Actor, probe: dict,
                                evidence: list[dict], *, key: str, task_spec_digest: str,
                                consent_ref: str, query_digest: str,
                                request_digest: str, now: datetime) -> str:
    """把远端回执存成**本节点**的一行探测（org 作用域），GET/覆盖路径才统一。

    行 id 由本节点从 (actor, 幂等键) 确定性推导；`result` 里的 probe_id 仍是
    对端的真实回执 id —— 不篡改来源身份。
    """
    probe_row_id = "probe-" + hashlib.sha256(plans.canonical_bytes(
        [actor.organization_id, actor.kind, actor.id, actor.principal_id, key])
    ).hexdigest()[:26]
    existing = await session.get(FederationProbe, probe_row_id)
    if existing is not None:
        return probe_row_id
    retrieval = probe.get("retrieval") or {}
    try:
        # 确定性键的插入放 SAVEPOINT 里：并发同键撞唯一约束时只回滚这一条，
        # 规划事务里已追加的其它探测不被连带丢掉（N4；PG 咨询锁兜底）。
        async with session.begin_nested():
            session.add(FederationProbe(
                probe_id=probe_row_id, organization_id=actor.organization_id,
                actor_id=federation.acting_actor(actor),
                target_node_id=probe["target_node_id"],
                task_spec_digest=task_spec_digest, consent_ref=consent_ref,
                probe_kind=probe["probe_kind"],
                collection_id=str(retrieval.get("collection_ref") or ""),
                query_digest=query_digest, state=_probe_state(probe),
                result_json={"kind": probe["probe_kind"], "result": probe,
                             "evidence": evidence, "request_digest": request_digest},
                expires_at=now + timedelta(seconds=federation.PROBE_TTL_SECONDS),
                created_at=now))
            await session.flush()
    except IntegrityError:
        existing = await session.get(FederationProbe, probe_row_id)
        if existing is not None:
            return probe_row_id
        raise
    return probe_row_id


async def _find_reusable_probe(session: AsyncSession, actor: Actor, *, target: dict,
                               query_digest: str, index_revision: str,
                               manifest: dict | None,
                               now: datetime) -> tuple[dict | None, str | None]:
    """冻结的缓存复用 API + **调用者绑定复核**；返回 (回执, 本地行 id)。

    `cache.find_reusable_probe` 的作用域是 organization（缓存切片冻结的语义）；
    同一组织里不同调用者仍是可见性边界，命中后要把回执归回它那一行的 actor：
    远端回执的行 id 与回执内 `probe_id` 不同，不能按 id 取，因此按同一
    (org, target, collection, kind) 的至多 64 行做内容比对，取到后再核 actor。
    第二个返回值必须是**本地行 id**：计划的 probe_refs 与执行读回都用它，
    用对端回执里的 `probe_id` 会指不到行（覆盖条目会丢回执）。
    """
    found = await cache.find_reusable_probe(
        session, actor, target_key=_target_key(target), query_digest=query_digest,
        index_revision=index_revision,
        policy_revision=_probe_policy_revision(manifest, target["origin_node_id"]),
        now=now)
    if found is None:
        return None, None
    rows = await session.scalars(select(FederationProbe).where(
        FederationProbe.organization_id == actor.organization_id,
        FederationProbe.target_node_id == target["origin_node_id"],
        FederationProbe.collection_id == target["collection_id"],
        FederationProbe.probe_kind == cache.PROBE_KIND,
    ).order_by(FederationProbe.created_at.desc(),
               FederationProbe.probe_id.desc()).limit(cache.PROBE_SCAN_LIMIT))
    for row in rows:
        if (row.result_json or {}).get("result") == found:
            if row.actor_id != federation.acting_actor(actor):
                return None, None
            return found, row.probe_id
    return None, None


async def _probe_targets(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                         targets: list[dict], now: datetime, http, index,
                         peers: PeerDirectory, budget: routing.RootBudget,
                         manifest: dict | None = None,
                         descriptors: dict[tuple[str, str], dict] | None = None
                         ) -> tuple[list[dict], dict, dict]:
    """执行探索许可允许的 Probe。返回 (probes, target_outcome, extra)。

    顺序对每个目标固定：**先看能不能复用已有回执（零外发、零预算）**，再看
    负面缓存（零外发），最后才发真请求。复用只在前端拿到"当前索引修订"时
    才可能发生（描述符给的口径），所以不会拿旧修订的回执冒充当前检索。
    """
    task_spec = row.task_spec_json
    consent = row.exploration_consent_json
    scope_ref = row.scope_id
    node = federation.local_node_id()
    query = task_spec.get("query") or ""
    query_digest = plans.content_digest(query.encode("utf-8"))
    revisions = _registry_revisions(manifest)
    negative_scope = cache.organization_scope(actor.organization_id)
    probes: list[dict] = []
    outcome: dict[tuple, tuple[str, str | None]] = {}
    evidence: dict[tuple, list[dict]] = {}
    probe_ids: dict[tuple, str] = {}
    reused: dict[tuple, str] = {}
    for target in targets:
        key = (target["origin_node_id"], target["collection_id"], target["operation"])
        origin = target["origin_node_id"]
        descriptor = (descriptors or {}).get((origin, target["collection_id"]))
        index_revision = descriptor.get("index_revision") if isinstance(descriptor, dict) else None
        if target["operation"] != LOCATE_OPERATION \
                and isinstance(index_revision, str) and index_revision:
            found, row_id = await _find_reusable_probe(
                session, actor, target=target, query_digest=query_digest,
                index_revision=index_revision, manifest=manifest, now=now)
            if found is not None and row_id is not None:
                # 复用不是新观测：不加 attempts（由执行阶段的账本照实记），
                # 不占探索预算，也绝不落一条冒充本次探测的新行。probe_refs
                # 引用的必须是本地行 id，执行读回才找得到这一行。
                probes.append(found)
                probe_ids[key] = row_id
                reused[key] = row_id
                continue
        request = _probe_request(task_spec=task_spec, consent_ref=consent["consent_id"],
                                 scope_ref=scope_ref, target=target, query=query)
        if origin == node:
            probe_key = _probe_key(row.root_task_id, target)
            try:
                # commit=False：规划持有每 root 的事务级咨询锁，探测在规划
                # 事务里落行、随计划一起提交；中途提交会把锁提前放掉。
                probe = await federation.run_probe(
                    session, actor, request, now=now, http=http, index=index,
                    idempotency_key=probe_key, commit=False)
            except APIError as exc:
                outcome[key] = ("failed", exc.code)
                continue
            probes.append(probe)
            probe_ids[key] = probe["probe_id"]
            stored = await session.get(FederationProbe, probe["probe_id"])
            if stored is not None:
                evidence[key] = list((stored.result_json or {}).get("evidence") or [])
            continue
        denial = _peer_probe_denial(consent, origin)
        if denial is not None:
            outcome[key] = ("denied", denial)
            continue
        node_revision = revisions.get(origin)
        if node_revision:
            seen = await cache.get_negative(session, scope_key=negative_scope,
                                            node_id=origin, node_revision=node_revision,
                                            now=now)
            if seen is not None:
                # 命中负面条目：不发一个字节、不占探测预算，也不续命
                # （get_negative 只读，expires_at 原样）。
                reason = str(seen.get("reason") or "unreachable")
                outcome[key] = (_negative_state(reason), reason)
                continue
        try:
            # 预占在真发请求之前；失败的预占不退款 —— 真实外发尝试的成本照记。
            budget.reserve("probe")
        except ApplicationError as exc:
            outcome[key] = ("not_attempted", exc.code)
            continue
        try:
            remote = await peers.client(origin).probe(
                request, idempotency_key=_probe_key(row.root_task_id, target))
        except PeerUnavailable as exc:
            if node_revision and (exc.status is None or exc.status == 403):
                await cache.record_negative(session, scope_key=negative_scope,
                                            node_id=origin, node_revision=node_revision,
                                            reason=_negative_reason(exc), now=now)
            outcome[key] = ("unreachable" if exc.status is None else "failed",
                            exc.code or "peer_unavailable")
            continue
        items: list[dict] = []
        set_ref = (remote.get("retrieval") or {}).get("evidence_set_ref")
        if set_ref:
            try:
                items = list((await peers.client(origin)
                              .evidence_set(str(set_ref))).get("items") or [])
            except PeerUnavailable as exc:
                outcome[key] = ("unreachable" if exc.status is None else "failed",
                                exc.code or "peer_unavailable")
                continue
        probes.append(remote)
        probe_ids[key] = await _persist_remote_probe(
            session, actor, remote, items,
            key=_probe_key(row.root_task_id, target),
            task_spec_digest=row.task_spec_digest, consent_ref=consent["consent_id"],
            query_digest=query_digest,
            request_digest=plans.content_digest(plans.canonical_bytes(request)), now=now)
    return probes, outcome, {"evidence": evidence, "probe_ids": probe_ids, "reused": reused}


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------

def _root_budget(consent: dict, *, target_count: int, remote_count: int,
                 deadline: str, generation_ready: bool = False,
                 discovery_count: int = 0) -> dict:
    """从探索许可 + 目标数推导根预算（可复核、只增不减）。

    - `max_requests` = 许可的 `max_probe_requests`（每个远端探测一次）
      + 每个目标一次 retrieve 受理 + 已消耗的目录发现请求
      （`discovery_count`，T87：发现与探测共用一份请求额度）。
      本地探测不出网，不占探索额度。
    - `max_bytes` = 许可的 `max_egress_bytes`（探测外发）
      + 每个目标一份检索结果的字节上限（`EVIDENCE_BYTES_PER_TARGET`）。
    - `max_hops` = 每个远端目标一来一回两条数据边；本地生成不产生跨节点边。
      answer 委托的 `edge-answer-1` 在计划落定时给这份预算 +1（调用方补，
      因为能不能委托要等能力探测结果）。
    - `max_generation_tokens` = 本地生成或远端 answer 委托就绪时才给固定额度
      （`GENERATION_TOKEN_BUDGET`），否则为 0（"本计划不生成"）。
    - `deadline` = min(scope 有效期, 探索许可有效期, 计划有效期)；三者一致时
      取同一个时刻。
    """
    probe_requests = int(consent["budget"]["max_probe_requests"])
    return {
        "max_requests": max(1, probe_requests + target_count + max(0, discovery_count)),
        "max_bytes": max(4096, int(consent["budget"]["max_egress_bytes"])
                         + target_count * EVIDENCE_BYTES_PER_TARGET),
        "max_hops": max(1, 2 * remote_count),
        "deadline": deadline,
        "max_generation_tokens": GENERATION_TOKEN_BUDGET if generation_ready else 0,
        # RootBudget 的子额度：探测次数与探测外发字节仍然受探索许可约束。
        # 这几个键只在根预算账本里用，不会进 TaskPlan.budget（那里是白名单）。
        "max_probe_requests": probe_requests,
        "max_egress_bytes": int(consent["budget"]["max_egress_bytes"]),
        "max_discovery_requests": int(consent["budget"].get("max_discovery_requests") or 0),
    }


def _drop_answer_steps(steps: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """丢掉 `plan_steps` 因远端 `can_generate` 生成的 answer 步（连同它的边）。

    `plan_steps` 的生成节点判据是**证据探测回执自称的 can_generate**，而这条
    路径没有经过授权的能力探测。协调者的 answer 步只有两种来源：
    `_append_local_answer_step`（本地生成就绪）与 `_append_delegated_answer_step`
    （`rag.answer.cited` 能力探测确认就绪）。两者都在计划落定时单独补上，
    所以 `plan_steps` 的答案步一律丢掉，避免绕过能力探测。
    """
    removed = {step["step_id"] for step in steps if step["operation"] == "answer"}
    if not removed:
        return steps, edges
    steps = [step for step in steps if step["step_id"] not in removed]
    edges = [edge for edge in edges if not edge["edge_id"].startswith("edge-answer")]
    return steps, edges


def _answer_probe_key(root_task_id: str, node_id: str) -> str:
    return "answer-probe:" + hashlib.sha256(plans.canonical_bytes(
        [root_task_id, node_id, "rag.answer.cited"])).hexdigest()


def _append_delegated_answer_step(steps: list[dict], edges: list[dict], *,
                                  coordinator: str, executor: str) -> tuple[list[dict], list[dict]]:
    """在远端生成节点上补一个消费融合证据的 answer 步骤与类型化数据边。

    依赖 `fuse-1`（协调者）：跨节点依赖必须有一条 `evidence_excerpts` 边，
    这条边在**审批之前**就进计划，因此 ExecutionConsent.allowed_edges 覆盖不到
    它时审批会 `egress_denied`，证据一个字节都发不出去。
    """
    steps.append({"step_id": "answer-1", "operation": "answer",
                  "executor_node_id": executor, "depends_on": ["fuse-1"],
                  "fixed_inputs": ["query"]})
    edges.append({"edge_id": "edge-answer-1", "from_node_id": coordinator,
                  "to_node_id": executor, "payload_kind": "evidence_excerpts",
                  "retention": "temporary", "authorised_by": f"relay:{coordinator}"})
    return steps, edges


def _answer_candidates(targets: list[dict], *, node: str) -> list[str]:
    """候选生成执行节点：本轮取数目标里的远端节点，按稳定顺序、有界。

    只在本切片已知的范围内找候选（不递归目录、不联系未进 scope 的节点）；
    顺序 = `_ordered_targets` 的顺序，保证同一计划每次得到同一个选择。
    """
    candidates: list[str] = []
    for target in _ordered_targets(targets):
        origin = target["origin_node_id"]
        if origin != node and origin not in candidates:
            candidates.append(origin)
    return candidates[:MAX_ANSWER_CANDIDATES]


async def _probe_answer_candidates(*, root_task_id: str, task_spec_digest: str,
                                   consent: dict, scope_ref: str, targets: list[dict],
                                   peers: PeerDirectory,
                                   budget: routing.RootBudget) -> tuple[str | None, dict]:
    """探测候选节点的 `rag.answer.cited` 就绪度，返回 (选中节点, 逐节点结果)。

    探索许可门先于任何字节：模式/接收方/载荷任一不覆盖就不发。每次外发先
    `budget.reserve("probe")` —— 超预算的预占不退款，也绝不继续发。

    输入全部是**已摊平的普通值**，不是 ORM 行：`_probe_targets` 可能在碰撞时
    回滚过会话（N8 同款），这里再摸 `row.attr` 会触发异步懒加载。
    """
    chosen: str | None = None
    outcomes: dict[str, str] = {}
    for candidate in _answer_candidates(targets, node=federation.local_node_id()):
        denial = _peer_probe_denial(consent, candidate)
        if denial is not None:
            outcomes[candidate] = denial
            continue
        try:
            budget.reserve("probe")
        except ApplicationError as exc:
            outcomes[candidate] = exc.code
            break
        request = {
            "schema": "ddp-task-probe/1#ProbeRequest",
            "task_spec_digest": task_spec_digest,
            "consent_ref": consent["consent_id"], "probe_kind": "capability_input",
            "target_node_id": candidate, "scope_ref": scope_ref,
            "operation": "rag.answer.cited",
        }
        try:
            probe = await peers.client(candidate).probe(
                request, idempotency_key=_answer_probe_key(root_task_id, candidate))
        except PeerUnavailable as exc:
            outcomes[candidate] = exc.code or "peer_unavailable"
            continue
        check = probe.get("capability_check")
        readiness = check.get("readiness") if isinstance(check, dict) else None
        if probe.get("can_generate") is True and readiness == "ready":
            chosen = candidate
            outcomes[candidate] = "ready"
            break
        outcomes[candidate] = "not_ready"
    return chosen, outcomes


async def _generation_available(http, *, now: datetime) -> bool:
    """协调者本地现在真的能做 `rag.answer.cited` 吗。

    **只认本层给 control / peer 的那份能力清单**（`capabilities.answer_generation_ready`
    就是同一个观测，远端执行者接单前用它复核）：模型名、`settings.chat_url`
    配过、远端探测里的 `can_generate`，单独哪一个都不是可用性证据。取不到
    观测、清单里没有这条 operation、readiness 不是 `ready`，一律 False ——
    读不出来就按不可用处理，绝不猜。
    """
    return await capabilities.answer_generation_ready(http, now=now)


def _append_local_answer_step(steps: list[dict], *, coordinator: str) -> list[dict]:
    """在协调者上补一个消费全部检索结果的 answer 步骤。

    执行权威在合成证据之后（`_execute_plan` 的 `_grounded_answer`），所以依赖
    直接写 retrieve 步骤；跨节点来源的证据回传边由 `routing.plan_steps` 生成，
    本地来源同节点不出边（`plans.validate_plan` 的同节点豁免）。
    """
    retrieve_ids = [step["step_id"] for step in steps if step["operation"] == "retrieve"]
    steps.append({"step_id": "answer-1", "operation": "answer",
                  "executor_node_id": coordinator, "depends_on": retrieve_ids,
                  "fixed_inputs": ["query"]})
    return steps


async def create_plan(session: AsyncSession, actor: Actor, root_task_id: str, *,
                      now: datetime, http, index) -> dict:
    """按持久化的 scope 与探索许可做 Probe、生成并持久化 TaskPlan。"""
    # 每 root 串行化：并发重放不能各做一轮 Probe（外发是有副作用的事实）。
    # 锁内先读现状，计划落定后的重放直接返回已存修订，不再探测。
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.plan_json and row.planning_state in ("ready", "approved"):
        # 重放：计划修订已经定了，不隐式重规划（要新范围就新建任务）。
        return row.plan_json
    if row.planning_state != "draft":
        raise APIError(409, "plan is not open for planning", "invalid_request_error",
                       "plan_changed")
    # 探索许可可能已经过期 —— 这一轮再验一次，过期就不发任何探测。
    consent = validate_exploration_consent(row.exploration_consent_json,
                                           row.task_spec_json, now=now)
    manifest = row.scope_manifest_json
    if manifest is not None:
        _validate_manifest(manifest, now=now)
    node = federation.local_node_id()
    task_spec = row.task_spec_json
    all_targets = _all_targets(task_spec, manifest, node)
    valid_until = min(plans.instant(manifest["valid_until"]) if manifest is not None
                      else plans.instant(consent["valid_until"]),
                      plans.instant(consent["valid_until"]), _ts(now) + SCOPE_TTL_SECONDS)
    deadline = plans.utc_instant(valid_until)
    # 本地生成就绪与否决定计划里有没有 answer 步与 token 额度。判据只来自
    # 能力清单，不来自模型名（"注册即就绪"是这个项目反复吃亏的地方）。
    generation_ready = await _generation_available(http, now=now)
    # 探索阶段的额度用**全量目标**做上界：目录摘要要先把远端目录读回来才拿得到，
    # 而读目录本身要先占发现额度。摘要只改变 fast 选谁、不改变选多少，所以这个
    # 上界一定覆盖最终选择；计划声明的预算是按最终选择 + 已消耗的发现请求重算的。
    upper_remote = sum(1 for target in all_targets if target["origin_node_id"] != node)
    planning_budget = _root_budget(consent, target_count=len(all_targets),
                                   remote_count=upper_remote, deadline=deadline,
                                   generation_ready=generation_ready)
    try:
        root_budget = routing.RootBudget(planning_budget, now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    peers = peer_directory(actor)
    delegated: str | None = None
    answer_outcomes: dict[str, str] = {}
    descriptor_notes: dict = {}
    # 探测可能回滚会话；先在普通值上固定住能力探测需要的全部输入
    # （root_task_id 是函数入参，本来就是普通值）。
    task_spec_digest = row.task_spec_digest
    scope_ref = row.scope_id
    try:
        # 摘要 -> 选目标 -> 探测：顺序不能反。摘要没取到就退化为确定性
        # local_first 排序，成员一个不少（穷查仍然全量）。
        descriptors, descriptor_notes = await _gather_descriptors(
            session, actor, node=node, all_targets=all_targets, manifest=manifest,
            consent=consent, budget=root_budget, peers=peers,
            valid_until=datetime.fromtimestamp(valid_until, timezone.utc))
        selected = _select_targets(all_targets, task_spec, node, descriptors=descriptors)
        remote_count = sum(1 for target in selected if target["origin_node_id"] != node)
        budget = _root_budget(consent, target_count=len(selected),
                              remote_count=remote_count, deadline=deadline,
                              generation_ready=generation_ready,
                              discovery_count=root_budget.used()["discovery"])
        probes, outcome, extra = await _probe_targets(
            session, actor, row, targets=selected, now=now, http=http, index=index,
            peers=peers, budget=root_budget, manifest=manifest,
            descriptors=_descriptor_index(descriptors))
        if task_spec.get("operation") == "rag.answer.cited" and not generation_ready:
            # 本地没有生成能力：在探索许可与根预算之内问候选执行节点
            # "你能不能生成带出处的答案"。没有任何 ready 节点就保持诚实的
            # 无答案结果（不伪造答案，也不再多发一个字节）。
            delegated, answer_outcomes = await _probe_answer_candidates(
                root_task_id=root_task_id, task_spec_digest=task_spec_digest,
                consent=consent, scope_ref=scope_ref, targets=selected,
                peers=peers, budget=root_budget)
    finally:
        await peers.aclose()
    # Probe 路径在碰撞时可能回滚过 SAVEPOINT，行会被 expire；按主键重读，后面
    # 的计划构造不能触发 async 懒加载（N8；与 `_execute_plan` 的同款防御一致）。
    row = await _load_request(session, actor, root_task_id)
    try:
        steps, edges = routing.plan_steps(
            targets=selected, probes=probes, local_node_id=node,
            coordinator_node_id=node, query=task_spec.get("query") or "",
            now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    steps, edges = _drop_answer_steps(steps, edges)
    if generation_ready:
        # 本地就绪：保持既有本地生成路径。
        steps = _append_local_answer_step(steps, coordinator=node)
    elif delegated is not None:
        # 远端就绪：计划先在审批之前落下 answer 步与类型化数据边；许可覆盖不到
        # 这条边，approve 会 egress_denied，证据一个字节都不会发。
        steps, edges = _append_delegated_answer_step(
            steps, edges, coordinator=node, executor=delegated)
        budget["max_hops"] = max(1, budget["max_hops"] + 1)
        budget["max_bytes"] = budget["max_bytes"] + EVIDENCE_BYTES_PER_TARGET
        budget["max_generation_tokens"] = GENERATION_TOKEN_BUDGET
    steps_by_target = _steps_by_target({"steps": steps}, selected)
    for target in _ordered_targets(selected):
        key = (target["origin_node_id"], target["collection_id"], target["operation"])
        step = steps_by_target.get(key)
        if step is None:
            continue
        fixed_inputs = ["query"]
        if target["operation"] == LOCATE_OPERATION:
            fixed_inputs.append(target["collection_id"])
        else:
            fixed_inputs.append(f"collection:{target['collection_id']}")
        step["fixed_inputs"] = fixed_inputs
        probe_id = extra["probe_ids"].get(key)
        step["probe_refs"] = [probe_id] if probe_id else []
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan",
        "plan_id": "plan-" + row.root_task_id,
        "revision": int(row.plan_revision or 0) + 1,
        "task_spec_digest": row.task_spec_digest,
        "root_coordinator_node_id": node,
        "planning_state": "ready",
        "steps": steps,
        "data_edges": edges,
        "budget": {"max_requests": budget["max_requests"], "max_bytes": budget["max_bytes"],
                   "max_hops": budget["max_hops"], "deadline": budget["deadline"],
                   "max_generation_tokens": budget["max_generation_tokens"]},
        "final_result_writer": node,
        "valid_until": deadline,
    }
    plan["plan_digest"] = plans.task_plan_digest(plan)
    try:
        plans.validate_plan(plan, task_spec, local_node_id=node, now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    row.plan_json = plan
    row.plan_revision = plan["revision"]
    row.plan_digest = plan["plan_digest"]
    row.planning_state = "ready"
    row.scope_id = manifest["scope_id"] if manifest is not None else row.scope_id
    row.updated_at = now
    await _append_event(session, row.root_task_id, _EVENT_PLAN_READY, {
        "plan_digest": plan["plan_digest"], "revision": plan["revision"],
        "targets": len(selected),
        "outcomes": {"/".join(key): value[0] for key, value in outcome.items()},
        # 复用与排序依据都要能在事件流里复核：复用的目标不占探测额度，
        # 也不该被读成"这一轮真的探测过"（search_profile 在账本上标记）。
        "reused_probes": {"/".join(key): probe_id
                          for key, probe_id in extra.get("reused", {}).items()},
        "descriptor_sources": descriptor_notes,
        "answer_executor": delegated,
        "answer_probes": answer_outcomes,
        "generation_ready": generation_ready,
    }, now=now)
    await _commit(session)
    return plan


# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------

async def approve(session: AsyncSession, actor: Actor, root_task_id: str, *,
                  plan_digest: str, execution_consent: dict, now: datetime) -> dict:
    """批准精确的计划修订与外发边界；摘要不符 409 plan_changed，边界不覆盖 403。"""
    # 同上：同 root 的审批与规划串行化，重放不产生第二次副作用。
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    plan = row.plan_json
    if not plan or row.planning_state not in ("ready", "approved"):
        raise APIError(409, "task has no plan revision to approve", "invalid_request_error",
                       "plan_changed")
    if (row.planning_state == "approved"
            and row.execution_consent_ref == (execution_consent or {}).get("consent_id")
            and row.execution_consent_json == execution_consent):
        return plan
    if plan_digest != row.plan_digest \
            or (execution_consent or {}).get("plan_digest") != plan_digest:
        raise APIError(409, "submitted revision does not match the stored plan revision",
                       "invalid_request_error", "plan_changed")
    consent = validate_execution_consent(execution_consent, now=now)
    recipients = set(consent["allowed_recipients"])
    endpoints = {step["executor_node_id"] for step in plan["steps"]}
    for edge in plan["data_edges"]:
        endpoints.update(edge.get("relay_via") or [])
        endpoints.add(edge["from_node_id"])
        endpoints.add(edge["to_node_id"])
    if endpoints - recipients:
        raise _egress_denied("execution consent does not cover every executor and data edge")
    planned_edges = {edge["edge_id"] for edge in plan["data_edges"]}
    if not planned_edges <= set(consent["allowed_edges"]):
        raise _egress_denied("execution consent does not approve every data edge")
    if any(edge["retention"] != consent["retention"] for edge in plan["data_edges"]):
        raise _egress_denied("execution consent retention does not match the plan edges")
    # 批准时把 execution 引用写回 TaskSpec。两个摘要都不覆盖 consent_refs 与
    # planning_state/execution_consent_ref，所以这是对同一修订的补充，不是新修订。
    task_spec = dict(row.task_spec_json)
    task_spec["consent_refs"] = {**task_spec["consent_refs"],
                                 "execution": consent["consent_id"]}
    try:
        plans.validate_spec(task_spec)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    if plans.task_spec_digest(task_spec) != row.task_spec_digest:
        raise APIError(409, "task spec changed during approval", "invalid_request_error",
                       "plan_changed")
    approved = dict(plan, planning_state="approved",
                    execution_consent_ref=consent["consent_id"])
    if plans.task_plan_digest(approved) != row.plan_digest:
        raise APIError(409, "plan digest changed during approval", "invalid_request_error",
                       "plan_changed")
    try:
        plans.validate_plan(approved, task_spec, local_node_id=federation.local_node_id(),
                            now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    row.task_spec_json = task_spec
    row.execution_consent_json = consent
    row.execution_consent_ref = consent["consent_id"]
    row.plan_json = approved
    row.planning_state = "approved"
    row.updated_at = now
    await _append_event(session, row.root_task_id, _EVENT_APPROVED, {
        "plan_digest": row.plan_digest, "execution_consent_ref": consent["consent_id"],
    }, now=now)
    await _commit(session)
    return approved


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _step_inputs(query: str) -> list[dict]:
    content = query.encode("utf-8")
    return [{"ref": "query", "digest": plans.content_digest(content),
             "size_bytes": len(content)}]


async def _fixed_inputs(session: AsyncSession, actor: Actor, task_spec: dict,
                        target: dict) -> list[dict]:
    """固定资源目标：把真实的 source_digest 带上，但**本切片执行者仍无法本地
    重算它**（`_local_digest` 只认问题文本），所以会如实落到 waiting_input，
    而不是被当成 content_verified。"""
    inputs = _step_inputs(task_spec.get("query") or "")
    if target["operation"] != LOCATE_OPERATION:
        return inputs
    resource = await policy.require_resource(session, actor, target["collection_id"])
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == resource.id, ResourceVersion.deleted_at.is_(None)
    ).order_by(ResourceVersion.version_no.desc()).limit(1))
    digest = ("sha256:" + version.source_digest
              if version is not None and len(version.source_digest or "") == 64
              else plans.content_digest(target["collection_id"].encode("utf-8")))
    inputs.append({"ref": target["collection_id"], "digest": digest,
                   "size_bytes": int(version.size_bytes) if version is not None else 0})
    return inputs


def _admission_body(*, root_task_id: str, plan: dict, task_spec: dict,
                    consent: dict, step: dict, inputs: list[dict],
                    generation: int, evidence: list[dict] | None = None) -> dict:
    # **收的是裸值而不是 ORM 行**：本地目标等终态时 `_local_execution_outcome`
    # 会 rollback 结束读事务，行对象随之过期；循环里再摸 `row.*` 会 MissingGreenlet。
    body = {
        "schema": "ddp-plan-admission/1#AdmissionRequest",
        # **业务幂等键，不含 delegation_generation**。把代次写进键里会让丢响应后的
        # 对账永远 404，于是 resume 把一次已受理的执行重做成第二次执行（T81/T82）。
        # 代次只进请求体：同键同体由执行者复用回执，同键异体当场 409。
        "idempotency_key": f"{root_task_id}:{step['step_id']}",
        "root_task_id": root_task_id,
        "step_id": step["step_id"],
        "delegation_generation": generation,
        "task_spec": task_spec,
        "plan": plan,
        "execution_consent": consent,
        "inputs": inputs,
    }
    if evidence:
        # 类型化数据边的载荷：有界、逐条带摘要。空就不带这个键，保持取数步骤的
        # 请求摘要口径不变。
        body["evidence"] = evidence
    return body


def _unknown_admission(exc: PeerUnavailable) -> bool:
    """这次受理的结果未知吗（可能已受理但回执丢了）。

    传输层失败（status=None）与 5xx 都算未知：前者可能发生在请求已送达之后，
    后者可能发生在执行者写库之后。未知就必须先对账，不许直接换键重做（T82）。
    4xx 是对方的明确答复，不是未知。
    """
    return exc.status is None or exc.status >= 500


async def _lookup_local_receipt(session: AsyncSession, actor: Actor,
                                key: str) -> dict | None:
    """本地对账：404 返回 None，其余错误原样抛出。

    `APIError` 是 `HTTPException` 的子类，404 在 `status_code` 上，不在
    `status` 上。写错字段时找不到行会变成 `AttributeError` -> 500，任务停在
    running（N3 复核）——所以这里只有一个字段可认。
    """
    try:
        return await federation.lookup_admission(session, actor, key)
    except APIError as exc:
        if exc.status_code == 404:
            return None
        raise


def _receipt_binding_error(receipt: dict | None, *, key: str, root_task_id: str,
                           step_id: str, plan_digest: str,
                           executor_node_id: str) -> str | None:
    """回执必须逐字段绑定本次 (root, step, 业务键, 当前计划修订, 执行者)。

    对账（lookup）拿回的回执可能来自另一个 root/step/计划 —— 对端键被复用、
    对方在乱回、或者本地镜像里混进了别人的受理行。**不绑定就采用**等于把一次
    外来执行、以及它的证据接进本任务。任何一项对不上都按"没有有效回执"处理，
    并把出错的字段写进可见的机器原因；绝不轮询或融合外来的 executor_task。
    """
    if not isinstance(receipt, dict):
        return "receipt_binding_mismatch:schema"
    for field, expected in (("root_task_id", root_task_id), ("step_id", step_id),
                            ("idempotency_key", key), ("plan_digest", plan_digest),
                            ("executor_node_id", executor_node_id)):
        if receipt.get(field) != expected:
            return f"receipt_binding_mismatch:{field}"
    return None


async def _lookup_remote_receipt(client, key: str) -> dict | None:
    """远端对账：404 返回 None，其余错误原样抛出（由调用方映射成可见状态）。"""
    try:
        return await client.lookup(key)
    except PeerUnavailable as exc:
        if exc.status == 404:
            return None
        raise


async def _run_local_step(session: AsyncSession, actor: Actor, *, root_task_id: str,
                          plan: dict, task_spec: dict, consent: dict, step: dict,
                          target: dict, generation: int, now: datetime, http,
                          index, reconcile: bool) -> tuple[str, str | None, list[dict],
                                                            str | None, list[str]]:
    """本地目标一律经 `federation.admit` 走同一条执行路径。

    `reconcile=True`（补做/重放）时先按**业务幂等键**对账：已经受理过的步骤
    直接复用执行行，绝不为同一个 (root, step) 造第二条执行记录。
    """
    inputs = await _fixed_inputs(session, actor, task_spec, target)
    body = _admission_body(root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                           consent=consent, step=step, inputs=inputs,
                           generation=generation)
    key = body["idempotency_key"]
    expected = {"key": key, "root_task_id": root_task_id,
                "step_id": step["step_id"], "plan_digest": plan["plan_digest"],
                "executor_node_id": federation.local_node_id()}
    if reconcile:
        receipt = await _lookup_local_receipt(session, actor, key)
        if receipt is not None:
            mismatch = _receipt_binding_error(receipt, **expected)
            if mismatch is not None:
                return "failed", mismatch, [], None, []
            return await _local_execution_outcome(
                session, actor, receipt, now=now, http=http, index=index,
                retry_expired=True)
    try:
        receipt, created = await federation.admit(session, actor, body, now=now,
                                                  http=http, index=index)
    except APIError:
        raise
    except Exception:                      # noqa: BLE001 —— 录取结果未知，先对账
        await session.rollback()
        # 对账本身也可能炸。四种形状都要有明确结局，绝不能带着异常逃逸成
        # 500 并把任务永远留在 running（N3）。
        try:
            receipt = await _lookup_local_receipt(session, actor, key)
        except APIError as exc:
            # 对账得到明确答复（非 404）：如实记失败，错误码就是答复。
            return "failed", exc.code, [], None, []
        except Exception:                  # noqa: BLE001 —— 连对账都不确定
            return "unreachable", "admission_lookup_failed", [], None, []
        if receipt is None:
            return "unreachable", "admission_unknown", [], None, []
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            return "failed", mismatch, [], None, []
        created = False                    # 这是对账捡回来的旧受理
    mismatch = _receipt_binding_error(receipt, **expected)
    if mismatch is not None:
        return "failed", mismatch, [], None, []
    # created=False（同键重放/对账）时连 `lease_expired` 一起补做：用户看到的
    # 是一次 resume，不能因为回执来自旧执行就把失败永久留下。
    return await _local_execution_outcome(
        session, actor, receipt, now=now, http=http, index=index,
        retry_expired=reconcile or not created)


async def _local_execution_outcome(session: AsyncSession, actor: Actor,
                                   receipt: dict, *, now: datetime, http, index,
                                   retry_expired: bool = False
                                   ) -> tuple[str, str | None, list[dict], str | None, list[str]]:
    """把一次本地受理回执翻成覆盖账本结局。

    - `queued`：受理时已经**持久化 + 排队**（执行行与队列任务同一个事务）。
      协调者自己就在 worker 进程里，直接调 `federation.execute` 把它跑完 ——
      不占第二个并发位，也不需要第二个会话（SQLite 单测只有一条连接）。
      队列里的 `federation_execute` 任务仍然在，作为进程崩溃的恢复路径：
      它被领取时 `execute` 看到终态会原样返回，不会重复执行。
    - `claimed/running`（别的 worker 已经领走）：等它落终态；有租约围栏，
      崩溃由回收清扫 + `execute` 的过期租约接管兜底。
    - `failed` 且原因是清扫留下的非业务事实（`lease_expired` / `queue_task_*`）
      且本次是补做（resume）：重排一次执行而不是把目标永久钉死。队列任务
      耗尽重试死掉的执行由 `reconcile` 对账落成 `failed/queue_task_failed`，
      没有这一步它会永远停在 queued。
    - `cancelled` 是终态：目标记 `not_attempted`，不重试、不伪造结果。
    """
    if receipt.get("state") != "accepted":
        return "not_attempted", str(receipt.get("state") or "not_accepted"), [], None, []
    executor_task_id = str(receipt.get("executor_task_id") or "")
    if not executor_task_id:
        return "failed", "invalid_admission_receipt", [], None, []
    execution = await federation.require_execution(session, actor, executor_task_id)
    status = federation.execution_status(execution)
    if retry_expired and status["state"] == "failed" \
            and status.get("error") in federation.RETRYABLE_EXECUTION_ERRORS:
        if await federation.retry_execution(session, actor, executor_task_id, now=now):
            execution = await federation.require_execution(session, actor, executor_task_id)
            status = federation.execution_status(execution)
    if status["state"] == "queued":
        await federation.execute(session, actor, execution, now=utcnow(), http=http,
                                 index=index, heartbeat=True)
        execution = await federation.require_execution(session, actor, executor_task_id)
        status = federation.execution_status(execution)
    deadline = time.monotonic() + LOCAL_POLL_DEADLINE_SECONDS
    while status["state"] in ("claimed", "running"):
        if time.monotonic() >= deadline:
            return "unreachable", "local_execution_timeout", [], None, []
        # 结束读事务：SQLite 的共享连接不结束就看不到另一个会话的提交
        # （PG 上多一次 rollback 只是结束只读事务）。
        if session.in_transaction():
            await session.rollback()
        await asyncio.sleep(LOCAL_POLL_INTERVAL_SECONDS)
        execution = await federation.require_execution(session, actor, executor_task_id)
        status = federation.execution_status(execution)
    if status["state"] == "cancelled":
        return "not_attempted", "cancelled", [], None, []
    if status["state"] != "succeeded":
        return "failed", str(status.get("error") or status["state"]), [], None, []
    stored = execution.result_json or {}
    evidence = list(stored.get("evidence") or [])
    index_revision = (stored.get("result") or {}).get("index_revision")
    return ("succeeded", None, evidence, index_revision,
            list(status.get("internal_limits") or []))


async def _poll_execution(client, executor_task_id: str) -> dict:
    """轮询到终态；超时返回 `peer_execution_timeout` 的显式状态。

    返回对端最后一次状态（或合成的超时状态）；调用方按 state/error 决定结局。
    **超时不取消对端执行**：它仍受对端自己的执行时限约束，而 resume 的对账
    会找回同一条执行继续等。取消它等于把可重做的目标永久钉死（对账只能
    拿回 cancelled，换代次重受理是同键异体 409）。
    """
    deadline = time.monotonic() + PEER_POLL_DEADLINE_SECONDS
    status = await client.execution(executor_task_id)
    while status.get("state") not in ("succeeded", "failed", "cancelled"):
        if time.monotonic() >= deadline:
            return {"state": "unreachable", "error": "peer_execution_timeout"}
        await asyncio.sleep(PEER_POLL_INTERVAL_SECONDS)
        status = await client.execution(executor_task_id)
    return status


async def _run_remote_step(peers: PeerDirectory, *, root_task_id: str, plan: dict,
                           task_spec: dict, consent: dict, step: dict, target: dict,
                           generation: int, reconcile: bool) -> tuple[str, str | None,
                                                                        list[dict],
                                                                        str | None,
                                                                        list[str]]:
    """远端执行者的 admission + 轮询。

    未知结果（传输失败/5xx）先按业务键 lookup 对账；只有 lookup 确认从未受理
    （404）才允许重试，重试仍用同一个业务键、只前进 delegation_generation。
    `reconcile=True` 的补做路径进入前也先对账，防止把丢失回执的成功执行重做。
    """
    node_id = target["origin_node_id"]
    try:
        client = peers.client(node_id)
        body = _admission_body(root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                               consent=consent, step=step,
                               inputs=_step_inputs(task_spec.get("query") or ""),
                               generation=generation)
        key = body["idempotency_key"]
        expected = {"key": key, "root_task_id": root_task_id, "step_id": step["step_id"],
                    "plan_digest": plan["plan_digest"], "executor_node_id": node_id}
        receipt = await _lookup_remote_receipt(client, key) if reconcile else None
        if receipt is None:
            try:
                receipt = await client.admit(body, idempotency_key=key)
            except PeerUnavailable as exc:
                if not _unknown_admission(exc):
                    raise
                receipt = await _lookup_remote_receipt(client, key)
                if receipt is None:
                    # 对账证明从未受理；这次的未知结果如实上报，补做时再来。
                    raise
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            # 对端把别的 root/step/计划修订的回执放在我们的键下（lookup 或受理
            # 响应都算）。采用它就等于轮询并融合一次外来执行；按"没有有效回执"
            # 处理并如实上报，绝不把外来 executor_task 接进本任务。
            return "failed", mismatch, [], None, []
        if receipt.get("state") != "accepted":
            return "not_attempted", str(receipt.get("state") or "not_accepted"), [], None, []
        executor_task_id = str(receipt.get("executor_task_id") or "")
        if not executor_task_id:
            return "failed", "invalid_admission_receipt", [], None, []
        status = await _poll_execution(client, executor_task_id)
        if status.get("state") == "unreachable":
            return "unreachable", str(status.get("error") or "peer_unreachable"), [], None, []
        if status.get("state") == "cancelled":
            # 对端把它显式取消了：这是终态，不是"没试过"，但对本任务的覆盖
            # 语义是 not_attempted（取消的目标不该被算成检索失败）。
            return "not_attempted", "cancelled", [], None, []
        if status.get("state") != "succeeded":
            return "failed", str(status.get("error") or status.get("state")), [], None, []
        evidence: list[dict] = []
        set_ref = status.get("evidence_set_ref")
        if set_ref:
            evidence = list((await client.evidence_set(str(set_ref))).get("items") or [])
        return ("succeeded", None, evidence, None,
                list(status.get("internal_limits") or []))
    except PeerUnavailable as exc:
        state = "unreachable" if exc.status is None else (
            "denied" if exc.status == 403 else "failed")
        return state, exc.code or "peer_unavailable", [], None, []


def _delegated_failure(reason: str) -> dict:
    """委托没成功时的答案字段：空答案 + 显式原因 + failed。"""
    return {**federation.unavailable_answer(reason), "validation_state": "failed"}


_REASON_DETAIL_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _reason_detail(value) -> str:
    """对端给的状态/错误码只当**细节**显示：限定字符集与长度。

    对端完全控制这些字符串；原样写进结果就等于让对端往用户界面里塞任意文本，
    也让答案原因变成一个无法枚举的开放集合。代码本身永远是本节点声明过的那几个。
    """
    text = _REASON_DETAIL_CHARS.sub("_", str(value or ""))[:64]
    return text or "unknown"


def _peer_failure(exc: PeerUnavailable) -> dict:
    """远端生成节点连不上或回错：代码固定，对端错误码/HTTP 状态进细节。"""
    detail = exc.code or (f"http_{exc.status}" if exc.status is not None else "transport")
    return _delegated_failure(f"peer_unavailable:{_reason_detail(detail)}")


def _remote_answer_reason(reason) -> str:
    """远端答案文档自报的 answer_reason。

    是本契约声明过的代码就照用（远端也跑同一份契约）；带细节时只保留允许带细节的
    代码并清洗细节。认不出来的一律归到 `delegated_answer_rejected:细节`，不透传。
    """
    text = str(reason or "")
    head, separator, detail = text.partition(":")
    if head in FEDERATED_ANSWER_REASON_VALUES:
        if separator and head in federation.ANSWER_REASONS_WITH_DETAIL and detail:
            return f"{head}:{_reason_detail(detail)}"
        return head
    if not text:
        return "delegated_answer_rejected"
    return f"delegated_answer_rejected:{_reason_detail(text)}"


def _validated_delegated_answer(document, *, evidence_ids: list[str]) -> dict:
    """校验远端回传的答案文档；任何越界/缺失引用都拒绝，不修补、不降级采用。

    - 引用绑定的 evidence id 必须是**本次发送证据 id 的子集** —— 对端报一个
      我们没发过的 id 就是伪造证据引用，整份答案作废；
    - `semantic_review` 一律重写成 `needs_review`：远端说 passed 不是人审；
    - 只复制已知字段，绝不把对端的任意 JSON 透传进任务结果。
    """
    if not isinstance(document, dict):
        return _delegated_failure("delegated_answer_missing")
    answer = document.get("answer")
    if document.get("validation_state") != "passed" or not isinstance(answer, str) \
            or not answer.strip():
        return _delegated_failure(_remote_answer_reason(document.get("answer_reason")))
    bindings = document.get("claim_evidence_bindings")
    if not isinstance(bindings, list) or not bindings:
        return _delegated_failure("delegated_bindings_missing")
    allowed = set(evidence_ids)
    validated = []
    for binding in bindings:
        if not isinstance(binding, dict):
            return _delegated_failure("delegated_binding_out_of_scope")
        refs = binding.get("evidence_refs")
        claim = binding.get("claim_text")
        if not isinstance(refs, list) or not refs or not set(refs) <= allowed \
                or not isinstance(claim, str) or not claim.strip() \
                or binding.get("structural_validation") != "passed":
            return _delegated_failure("delegated_binding_out_of_scope")
        validated.append({
            "claim_id": str(binding.get("claim_id") or f"claim-{len(validated) + 1}"),
            "claim_text": claim,
            "evidence_refs": [str(ref) for ref in refs],
            "structural_validation": "passed",
            "semantic_review": "needs_review",
        })
    # 对端标出的矛盾同样只收"引用落在所发证据里、至少两条不同证据"的；依据一律
    # 重写成 generation_reported —— 版本分歧由本协调者按规则自己算，不信对端自报。
    raw_conflicts = document.get("conflicts", [])
    if not isinstance(raw_conflicts, list):
        return _delegated_failure("delegated_conflict_out_of_scope")
    conflicts = []
    for item in raw_conflicts:
        refs = item.get("evidence_refs") if isinstance(item, dict) else None
        if (not isinstance(refs, list) or len(set(map(str, refs))) < 2
                or not {str(ref) for ref in refs} <= allowed):
            return _delegated_failure("delegated_conflict_out_of_scope")
        conflicts.append(coverage_kernel.conflict(
            "generation_reported", sorted({str(ref) for ref in refs})))
    provider = document.get("provider")
    provider_out = ({**provider, "location": "remote"} if isinstance(provider, dict) else None)
    return {
        "answer": answer.strip(),
        "answer_reason": None,
        "claim_evidence_bindings": validated,
        "conflicts": coverage_kernel.merge_conflicts(conflicts),
        "provider": provider_out,
        "disclosure": {"remote": True, "payload": ["question", "selected_evidence"]},
        "validation_state": "passed",
    }


async def _delegated_answer(row: FederationRequest, *, plan: dict, step: dict,
                            fused: list[dict], excerpts: dict[str, str],
                            actor: Actor) -> dict:
    """把整项答案委托给已就绪的远端执行者（计划里的 `answer-1`）。

    只发**有界且逐条带摘要**的证据摘录（`evidence_excerpts` 数据边），绝不发
    源文件；发之前逐条用 `excerpt_reason` 过一遍，任何缺失/越界都显式拒绝。
    受理与对账复用取数步骤那一套幂等语义；只采纳绑定落回所发证据 id 的结果。
    """
    executor = step["executor_node_id"]
    evidence: list[dict] = []
    for item in fused:
        evidence_id = str(item.get("evidence_id") or "")
        text = excerpts.get(evidence_id)
        reason = _excerpt_reason(text)
        if reason is not None:
            return _delegated_failure(reason)
        evidence.append({"evidence_id": evidence_id, "excerpt": text,
                         "digest": plans.content_digest(text.encode("utf-8"))})
    if not evidence:
        return _delegated_failure("insufficient_evidence")
    if len(evidence) > federation.ADMISSION_EVIDENCE_LIMIT:
        # 有界：不截断证据去凑数 —— 被截掉的引用会变成无根引用。
        return _delegated_failure("evidence_delegation_over_limit")
    body = _admission_body(
        root_task_id=row.root_task_id, plan=plan, task_spec=row.task_spec_json,
        consent=row.execution_consent_json, step=step,
        inputs=_step_inputs(row.task_spec_json.get("query") or ""),
        generation=int(row.delegation_generation or 0), evidence=evidence)
    key = body["idempotency_key"]
    expected = {"key": key, "root_task_id": row.root_task_id, "step_id": step["step_id"],
                "plan_digest": plan["plan_digest"], "executor_node_id": executor}
    peers = peer_directory(actor)
    try:
        client = peers.client(executor)
        # 先对账再受理：resume/重放不得为同一个 (root, step) 触发第二次生成。
        try:
            receipt = await _lookup_remote_receipt(client, key)
            if receipt is None:
                receipt = await client.admit(body, idempotency_key=key)
        except PeerUnavailable as exc:
            receipt = None
            if _unknown_admission(exc):
                try:
                    receipt = await _lookup_remote_receipt(client, key)
                except PeerUnavailable:
                    receipt = None
            if receipt is None:
                return _peer_failure(exc)
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            return _delegated_failure(mismatch)
        if receipt.get("state") != "accepted":
            return _delegated_failure(
                f"delegated_admission_not_accepted:{_reason_detail(receipt.get('state'))}")
        executor_task_id = str(receipt.get("executor_task_id") or "")
        if not executor_task_id:
            return _delegated_failure("invalid_admission_receipt")
        status = await _poll_execution(client, executor_task_id)
        if status.get("state") != "succeeded":
            # 超时（本节点合成的 peer_execution_timeout）、对端 failed/cancelled：
            # 代码固定，具体是哪一种进细节 —— 界面能分清，又不会冒出未声明的代码。
            return _delegated_failure(f"delegated_execution_failed:"
                                      f"{_reason_detail(status.get('error') or status.get('state'))}")
        return _validated_delegated_answer(
            status.get("answer"), evidence_ids=[item["evidence_id"] for item in evidence])
    except PeerUnavailable as exc:
        return _peer_failure(exc)
    finally:
        await peers.aclose()


def _evidence_key(item: dict) -> tuple:
    return (item.get("origin_node_id"), item.get("resource_id"),
            item.get("source_version_id"), item.get("evidence_id"))


def _public_item(item: dict) -> dict:
    """HTTP 出口只出契约字段。

    `_excerpt`/`_score` 是审计用内部字段；`excerpt` 是证据集读取专供生成用的
    有界正文，也不进任务结果 —— 正文只在生成时进 prompt，不随状态响应扩散。
    """
    return {key: value for key, value in item.items()
            if not str(key).startswith("_") and key != "excerpt"}


async def _mark_failed(session: AsyncSession, root_task_id: str, *, now: datetime,
                       error: str) -> bool:
    """落协调任务失败；**终态守卫是条件 UPDATE，不是"先读后写"**。

    取消可能恰好发生在业务错误抛出的前后；先读后写时，读到的"running"在
    写入的那一刻可能已经是别人提交的 cancelled —— 一条迟到的 `_mark_failed`
    会把用户显式取消的任务改写回 failed（语义从"用户不要了"变成"系统做砸了"）。
    这里让数据库替我们仲裁：`UPDATE ... WHERE status='running'`，命中 0 行
    表示终态已经由别人落定，本次一律不写，连事件也不追加。

    返回 True = 本次真的把 running 落成了 failed；False = 别人拥有这个结局。
    """
    await session.rollback()
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == root_task_id,
            FederationRequest.status == "running").values(
            status="failed", error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        await session.rollback()
        return False
    await _append_event(session, root_task_id, _EVENT_FAILED, {"error": error}, now=now)
    await session.commit()
    return True


def _entry_row(root_task_id: str, entry: dict) -> CoverageEntry:
    return CoverageEntry(
        root_task_id=root_task_id, target_digest=_target_digest(entry["target_key"]),
        target_key_json=entry["target_key"], query_digest=entry["query_or_subquery_digest"],
        state=entry["state"], probe_refs_json=list(entry.get("probe_receipts") or []),
        actual_index_revision=entry.get("actual_index_revision"),
        search_profile=entry.get("search_profile"), attempts=int(entry.get("attempts") or 0),
        last_error=entry.get("last_error"), evidence_refs_json=list(entry.get("evidence_refs") or []),
        used_budget_json=dict(entry.get("used_budget") or {"requests": 0, "bytes": 0}),
        exclusion_basis=entry.get("exclusion_basis"))


def _entry_from_row(row: CoverageEntry, scope_ref: str) -> dict:
    return {
        "target_key": row.target_key_json, "scope_ref": scope_ref,
        "query_or_subquery_digest": row.query_digest, "state": row.state,
        "probe_receipts": list(row.probe_refs_json or []),
        "actual_index_revision": row.actual_index_revision,
        "search_profile": row.search_profile, "attempts": int(row.attempts or 0),
        "last_error": row.last_error, "evidence_refs": list(row.evidence_refs_json or []),
        "used_budget": dict(row.used_budget_json or {"requests": 0, "bytes": 0}),
        "exclusion_basis": row.exclusion_basis,
    }


async def _load_probe(session: AsyncSession, actor: Actor, step: dict, *,
                      request_created_at: datetime | None = None
                      ) -> tuple[dict | None, bool]:
    """读回步骤绑定的探测回执，并如实区分"本次计划新探测的"与"复用的"。

    复用判据是持久行早于本任务受理（`create_plan` 的新探测都以计划时刻写入，
    晚于受理）。标记只影响 `search_profile` 的可读性，不改变覆盖计数。
    """
    for probe_id in step.get("probe_refs") or []:
        row = await session.get(FederationProbe, probe_id)
        if row is not None and row.organization_id == actor.organization_id:
            reused = (request_created_at is not None
                      and as_aware(row.created_at) < as_aware(request_created_at))
            return (row.result_json or {}).get("result"), reused
    return None, False


def _enumeration_state(row: FederationRequest) -> str:
    manifest = row.scope_manifest_json
    if manifest is not None:
        return str(manifest.get("enumeration_state") or "partial")
    return "sealed"   # fixed_resources：固定列表就是完整分母


#: 协调者结果里的内部字段：上一轮可归属（自报来源 = 返回目标节点）的证据键，供
#: resume 时让复原条目参与版本分歧比较。以 `_` 开头，不进交付文档、不出 HTTP。
_ATTRIBUTED_FIELD = "_attributed_evidence"


def _public_result(result: dict | None) -> dict | None:
    """状态出口只出结果文档字段；内部簿记（`_` 开头）不外泄。"""
    if not result:
        return None
    return {key: value for key, value in result.items() if not str(key).startswith("_")}


def _recorded_conflicts(row: FederationRequest) -> list[dict]:
    """结果文档里持久化的矛盾记录（协调者写入时已校验）；读路径据此复原冲突轴。

    覆盖读取是从逐目标记录重算的；矛盾不在逐目标记录里，不带上它，GET coverage
    会把刚写成 conflicting 的任务重新算回 sufficient_by_policy。
    """
    return coverage_kernel.merge_conflicts((row.result_json or {}).get("conflicts") or [])


def _first_error(entries: list[dict]) -> str | None:
    for entry in entries:
        if entry.get("last_error"):
            return str(entry["last_error"])
    return None


def _bounded_delivery_document(document: dict) -> dict | None:
    """把可交付文档限进字节上限；超限返回 None（**不截断**）。

    截断文档再报一个覆盖截断后字节的摘要，等于让本地"校验通过"的是被改过的
    内容 —— 客户端会据此确认一份与中心实际结果不同的交付。超限就如实不交付。
    """
    try:
        size = len(plans.canonical_bytes(document))
    except ApplicationError:
        return None
    if size > DELIVERY_RESULT_MAX_BYTES:
        return None
    return document


async def _deliver_result(session: AsyncSession, row: FederationRequest, *,
                          document: dict, digest: str, now: datetime) -> FederationDelivery:
    """任务产生结果时建一条 delivery=pending，并持久化有界的交付文档。

    `document` 是结果的规范文档（**剔除摘要字段本身**）：读取端点把它作为
    `result` 返回，客户端用 `content_digest(canonical result)` 与
    `result_manifest_digest` 对账。retention 永远不是 persistent。
    """
    delivery = await session.get(FederationDelivery, row.delivery_id) if row.delivery_id else None
    if delivery is None or delivery.state in ("expired", "confirmed"):
        delivery_id = new_id()
        delivery = FederationDelivery(
            delivery_id=delivery_id, root_task_id=row.root_task_id, state="pending",
            result_manifest_digest=digest, retention="temporary",
            expires_at=now + timedelta(seconds=DELIVERY_TTL_SECONDS), receipt_json={},
            created_at=now, updated_at=now)
        session.add(delivery)
        row.delivery_id = delivery_id
    else:
        delivery.result_manifest_digest = digest
        delivery.expires_at = now + timedelta(seconds=DELIVERY_TTL_SECONDS)
        delivery.updated_at = now
    delivery.result_json = _bounded_delivery_document(document)
    delivery.verified_at = None
    row.delivery_state = "pending"
    return delivery


def _answer_skeleton() -> dict:
    """答案字段的公共骨架（与远端执行者共用；见 `federation.answer_skeleton`）。"""
    return federation.answer_skeleton()


def _unavailable_answer(reason: str) -> dict:
    """生成没发生/没得用的显式原因。`local_model_missing` 与
    `insufficient_evidence` 也要走这里，不能是沉默的空值。"""
    return federation.unavailable_answer(reason)


async def _load_excerpts(session: AsyncSession, actor: Actor,
                         plan: dict) -> dict[str, str]:
    """从已落库的探测回执里取回每条融合证据的原文片段。

    执行时新检索到的证据在手上就带 `_excerpt`；这里补的是 resume 路径上被
    跳过的、上一轮已成功的目标 —— 结果里存的是公开信封（没有正文），而生成
    必须真的看到正文，否则 [n] 只是一串空编号。
    """
    excerpts: dict[str, str] = {}
    node = federation.local_node_id()
    for step in plan["steps"]:
        if step["operation"] != "retrieve":
            continue
        for probe_id in step.get("probe_refs") or []:
            probe = await session.get(FederationProbe, probe_id)
            if probe is None or probe.organization_id != actor.organization_id:
                continue
            # 来源按**协调者自己生成的计划**判：retrieve 步的执行者就是目标 origin。
            # 不用探测行的 `target_node_id` —— 远端行的这一列取自对端回执，坏对端
            # 报成本节点就能让自己的超长 `_excerpt` 被当成本地正文静默截断，
            # 绕过 N6 的显式拒绝。条目自报的 origin 同样不信。
            local_source = step["executor_node_id"] == node
            for item in (probe.result_json or {}).get("evidence") or []:
                evidence_id = str(item.get("evidence_id") or "")
                excerpt = _generation_excerpt(item, local_source=local_source)
                if evidence_id and excerpt is not None:
                    excerpts.setdefault(evidence_id, excerpt)
    return excerpts


def _generation_excerpt(item: dict, *, local_source: bool) -> str | None:
    """一条证据可以进生成的正文；没有就 None（生成端据此显式拒绝）。

    - **本节点自己的证据**取内部 `_excerpt`，按证据集出口同一把尺子截到契约
      上限（`federation.bounded_excerpt`）：本节点就是这段正文的权威，对外本来
      就只给有界片段。不截的话，一个没被切分的长表格/代码块会让整份带引用
      答案落 `excerpt_over_contract_bound`，而远端协调者读同一条证据却能成功。
    - **对端给的** `excerpt` 原样返回：越界交给 `excerpt_reason` 显式拒绝
      （N6，不静默改写别人的证据正文）。对端条目里的 `_excerpt` 不采信。
    - 空白不是正文（N5）。
    """
    if local_source:
        bounded = federation.bounded_excerpt(item.get("_excerpt"))
        if bounded is not None:
            return bounded
    excerpt = item.get("excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        return excerpt
    return None


def _excerpt_reason(text) -> str | None:
    """这条正文能不能进生成：不能就返回机器原因，能返回 None。

    实现只有一份（`federation.excerpt_reason`）：远端执行者接收证据时与本地
    生成时用的是同一把尺子，两边不会对"多长算越界"给出不同答案。
    """
    return federation.excerpt_reason(text)


async def _grounded_answer(http, *, query: str, fused: list[dict],
                           excerpts: dict[str, str], max_generation_tokens: int) -> dict:
    """本地带引用生成（委托 `federation.grounded_answer`，与远端执行者同一实现）。

    本地与远端只有在 provider 归属上不同：本地生成的 `provider.location` 是
    `local`。引用结构验收、越界/空白正文拒绝、超预算拒绝与绑定形状全部由共享
    实现决定，这里不再复制一份。
    """
    return await federation.grounded_answer(
        http, query=query,
        evidence_ids=[str(item.get("evidence_id") or "") for item in fused],
        excerpts=excerpts, max_generation_tokens=max_generation_tokens,
        provider_model=settings.chat_model or "unknown",
        provider_endpoint=settings.chat_endpoint, location="local")


async def _answer_result(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                         plan: dict, fused: list[dict], live_excerpts: dict[str, str],
                         sufficiency: str, http) -> dict:
    """执行阶段的答案决定：不生成 / 证据不足 / 本地生成 / 委托生成，都可见。"""
    node = federation.local_node_id()
    answer_step = next((step for step in plan["steps"]
                        if step["operation"] == "answer"), None)
    if answer_step is None:
        # 规划时本地与远端都没有可用的生成能力：明说没有模型，不伪造答案。
        return _unavailable_answer("local_model_missing")
    if sufficiency == "insufficient" or not fused:
        # 没有可引用的证据就不给模型留"凭常识补一句"的机会；契约也要求
        # insufficient 时绑定必须为空（ddp-evidence/v1 FederatedAnswer 的 allOf）。
        # 远端委托同理：没有证据就不发数据边。
        return _unavailable_answer("insufficient_evidence")
    excerpts = await _load_excerpts(session, actor, plan)
    excerpts.update({key: value for key, value in live_excerpts.items()
                     if isinstance(value, str) and value.strip()})
    if answer_step["executor_node_id"] != node:
        return await _delegated_answer(row, plan=plan, step=answer_step, fused=fused,
                                       excerpts=excerpts, actor=actor)
    cap = int((plan.get("budget") or {}).get("max_generation_tokens") or 0)
    if cap <= 0:
        return _unavailable_answer("local_model_missing")
    return await _grounded_answer(http, query=row.task_spec_json.get("query") or "",
                                  fused=fused, excerpts=excerpts,
                                  max_generation_tokens=cap)


async def _execute_plan(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                        now: datetime, http, index, retry_only: bool) -> dict:
    task_spec = row.task_spec_json
    plan = row.plan_json
    consent = row.execution_consent_json
    manifest = row.scope_manifest_json
    node = federation.local_node_id()
    query_digest = plans.content_digest((task_spec.get("query") or "").encode("utf-8"))
    all_targets = _all_targets(task_spec, manifest, node)
    # 计划是目标选择的权威：执行阶段没有目录摘要可重排，重排会错位挂回执。
    candidates = _plan_selected_targets(plan, all_targets)
    steps_by_target = _steps_by_target(plan, candidates)
    existing = {r.target_digest: r for r in await session.scalars(
        select(CoverageEntry).where(CoverageEntry.root_task_id == row.root_task_id))}
    entries = {digest: _entry_from_row(item, row.scope_id) for digest, item in existing.items()}
    evidence: dict[tuple, dict] = {}
    previous = row.result_json or {}
    # §7.6 规则一路的输入：只收"自报来源 = 返回它的那个目标的节点"的条目。条目
    # 字段是对端自报的，不这样筛，坏对端就能伪造一条"本节点同资源同定位"的条目
    # 把本地证据标成矛盾。
    #
    # **归属要跨轮次保留**：resume 跳过上一轮已成功的目标，它们的条目只能从结果
    # 里复原。复原的条目若不参与分组，同一处的两个版本分两轮到达时就永远不会被
    # 比较，账本照报 sufficient_by_policy（第六次验收复现）。所以上一轮把可归属
    # 条目的键记进内部字段 `_attributed_evidence`（不进交付文档、不出 HTTP），
    # 这里据此把复原条目放回规则一路的输入。
    attributed_keys = {tuple(key) for key in previous.get(_ATTRIBUTED_FIELD) or []
                       if isinstance(key, list)}
    attributed: list[dict] = []
    for item in previous.get("evidence") or []:
        evidence[_evidence_key(item)] = item
        if _evidence_key(item) in attributed_keys:
            attributed.append(item)
    # 没有 `_attributed_evidence` 的旧结果（本字段上线前完成的任务）：复原的条目
    # 没有归属信息，至少把它记下的规则矛盾原样带回；生成标注的矛盾不复原（本轮
    # 会重新生成）。
    restored_divergences = [item for item in _recorded_conflicts(row)
                            if item["basis"] == "version_divergence"]
    # 本轮真正看到的正文片段（公开结果只出契约字段，`_excerpt` 不进 HTTP 出口）。
    live_excerpts: dict[str, str] = {}
    generation = int(row.delegation_generation or 0)
    root_task_id = row.root_task_id
    # resume 前移过代次：上一轮可能已受理了某个目标却在落账前死掉（没有
    # coverage 行）。不先对账就按新代次重新受理，同一个业务键的请求摘要变了，
    # 执行者只能 409 idempotency_conflict —— 所以补做一律先对账。
    # 循环里不能再读 `row.*`：本地目标等待终态时 `_local_execution_outcome`
    # 会 rollback 结束读事务，行对象随之过期；之后在同步上下文里摸属性就是
    # MissingGreenlet。需要的列在这里一次取完。
    scope_id = row.scope_id
    exploration_consent = row.exploration_consent_json
    request_created_at = as_aware(row.created_at)
    peers = peer_directory(actor)
    try:
        for target in candidates:
            key = (target["origin_node_id"], target["collection_id"], target["operation"])
            digest = _target_digest(target)
            current = entries.get(digest)
            if current is not None and (
                    current["state"] not in _RETRYABLE_STATES
                    or current.get("last_error") == "search_mode_fast"):
                continue
            step = steps_by_target.get(key)
            if step is None:
                continue
            entry = coverage_kernel.new_entry(_target_key(target), scope_id, query_digest)
            probe, probe_reused = await _load_probe(
                session, actor, step, request_created_at=request_created_at)
            probe_limits: list[str] = []
            if probe is not None:
                # 规划期探测在这里只提供**回执绑定**（probe_receipts + 实际索引
                # 修订），成败由下面的执行决定，所以按 in_flight 记。让内核从
                # 探测派生 succeeded 会触发"过期探测不许记成新成功"：计划有效
                # 900s、探测只有 300s，用户审批慢一点，成功执行就被降成
                # partial/probe_receipt_missing 并丢掉证据。
                try:
                    entry = coverage_kernel.record(entry, probe, state="in_flight",
                                                   now=_ts(now))
                    probe_limits = list(
                        (probe.get("retrieval") or {}).get("internal_limits") or [])
                except ApplicationError:
                    entry = coverage_kernel.new_entry(_target_key(target), scope_id,
                                                      query_digest)
            if probe_reused:
                # 这一行的探测回执来自更早的任务：账本照实带缓存标记，
                # 不把它读成"这一轮真的重新探测过"。
                entry["search_profile"] = CACHED_PROBE_PROFILE
            # bytes 记账留 0：精确外发字节需要数据面测量（socket/网关侧计数器），
            # 本切片只能诚实地记"次数已发生、字节未测"，不编一个看起来精确的数。
            entry["used_budget"] = {"requests": 1, "bytes": 0}
            if target["origin_node_id"] != node:
                # 探索许可门在执行阶段仍然生效：探都不许探的目标，admission 更
                # 不许发。local_only / 未列入接收方 / 载荷不许的目标保持 denied。
                denial = _peer_probe_denial(exploration_consent,
                                            target["origin_node_id"])
                if denial is not None:
                    entries[digest] = coverage_kernel.record(
                        entry, None, state="denied", error=denial, now=_ts(now))
                    continue
            internal_limits: list[str] = []
            try:
                if target["origin_node_id"] == node:
                    state, error, items, revision, internal_limits = await _run_local_step(
                        session, actor, root_task_id=root_task_id, plan=plan,
                        task_spec=task_spec,
                        consent=consent, step=step, target=target, generation=generation,
                        now=now, http=http, index=index,
                        reconcile=retry_only or current is not None)
                else:
                    state, error, items, revision, internal_limits = await _run_remote_step(
                        peers, root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                        consent=consent,
                        step=step, target=target, generation=generation,
                        reconcile=retry_only or current is not None)
            except APIError as exc:
                state, error, items, revision, internal_limits = "failed", exc.code, [], None, []
            if state == "succeeded" and revision:
                # 执行报了自己检索的索引修订就以它为准：规划期探测的修订最多可能
                # 是计划有效期（900s）之前的，覆盖账本要记"实际检索的是哪一版"。
                entry["actual_index_revision"] = revision
            if state == "succeeded":
                if not entry.get("probe_receipts") or not entry.get("actual_index_revision"):
                    # §7.4：没有回执与实际索引修订的"成功"不许进成功数。
                    state, error = "partial", "probe_receipt_missing"
                else:
                    local_source = target["origin_node_id"] == node
                    for item in items:
                        evidence[_evidence_key(item)] = _public_item(item)
                        if item.get("origin_node_id") == target["origin_node_id"]:
                            attributed.append(_public_item(item))
                        excerpt = _generation_excerpt(item, local_source=local_source)
                        if excerpt is not None:
                            live_excerpts[str(item.get("evidence_id") or "")] = excerpt
            # 执行/probe 自报的内部限制一并进账本：非空必须落 partial，
            # 绝不允许由一条 truncated_by_limit 的执行推出 complete（T85）。
            entry = coverage_kernel.record(
                entry, None, state=state, error=error, now=_ts(now),
                limits=probe_limits + list(internal_limits))
            entries[digest] = entry
        # 分母补齐在 resume 上同样要做：上一轮若在落账前死掉，库里没有任何
        # coverage 行，fast 模式里没被选中的目标也就没有 entry，下面按全量
        # 目标取 entries 会 KeyError（既不是 APIError 也不是 ApplicationError，
        # 队列里反复重试，行一直停在 running）。已有的行原样保留。
        for target in all_targets:
            digest = _target_digest(target)
            if digest in entries:
                continue
            entry = coverage_kernel.new_entry(_target_key(target), scope_id, query_digest)
            if target not in candidates:
                entry = coverage_kernel.record(entry, None, state="not_attempted",
                                               error="search_mode_fast", now=_ts(now))
            entries[digest] = entry
    finally:
        await peers.aclose()
    # 本地执行失败时 `federation.execute` 会 rollback 整个 session（那是对的：
    # 失败要落库），副作用是协调者行被 expire。做账前按主键重新加载一次，
    # 不让"某个目标失败"把后面所有属性读都变成 MissingGreenlet。
    #
    # **先结束读事务再重读**：取消可能来自另一个会话；SQLite（以及 PG 的非
    # READ COMMITTED 快照）里不结束旧事务就看不到那个提交，围栏会拿着
    # "running" 的旧快照把一次迟到成功写进去。到这一步协调者 session 里
    # 没有未提交的业务写入（执行/受理都在内部 commit 过），rollback 是安全的。
    if session.in_transaction():
        await session.rollback()
    row = await session.get(FederationRequest, root_task_id,
                            populate_existing=True)
    if row.status != "running":
        # 取消（cancelled）、回收清扫（failed）或别的终态在本次执行期间落了库：
        # 迟到的成功/失败结果一律不许覆盖，覆盖账本也不许重写 —— cancel 已经把
        # 未完成目标记成 not_attempted，这里再写一遍会把那份账目改掉。
        await session.rollback()
        return _status_output(await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    ordered = [entries[_target_digest(target)] for target in _ordered_targets(all_targets)]
    fused = list(evidence.values())
    # §7.6 规则一路：同一来源不同版本在同一定位上的正文分歧，生成之前就能算。
    # 它只会把"充分"压成"矛盾"：没有绑定时账本仍是 insufficient，下面的生成闸
    # 照样拦住（内核 `sufficiency` 的优先级钉着这件事）。
    divergences = coverage_kernel.merge_conflicts(
        restored_divergences, coverage_kernel.version_conflicts(attributed))
    ledger = coverage_kernel.ledger(
        root_task_id=row.root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
        enumeration_state=_enumeration_state(row), entries=ordered, conflicts=divergences)
    coverage_kernel.validate_ledger(ledger)
    succeeded = ledger["counts"]["succeeded"]
    # 状态轴必须与证据轴一致（N1）：有真实证据就不是失败 —— 即使所有目标都
    # 因内部限额只落 partial（counts.succeeded 可以是 0）。覆盖账本照实带
    # partial/incomplete，`unretrieved_targets` 列出没查全的目标；failed 只
    # 留给"没有任何目标产出证据"（全部 denied/unreachable/failed/unsupported）。
    status = "succeeded" if succeeded > 0 or fused \
        or ledger["retrieval_completeness"] == "complete" else "failed"
    error = None if status == "succeeded" else (_first_error(ordered) or "no_retrievable_target")
    unretrieved = [{"target_key": entry["target_key"], "state": entry["state"],
                    "last_error": entry.get("last_error")}
                   for entry in ordered if entry["state"] != "succeeded"]
    answer = await _answer_result(
        session, actor, row, plan=plan, fused=fused, live_excerpts=live_excerpts,
        sufficiency=ledger["evidence_sufficiency"], http=http)
    if answer.get("conflicts"):
        # 生成一路标出的矛盾（已按本次证据编号域校验）并入账本：只会把充分性压成
        # conflicting，不会把 insufficient 抬高 —— 没有证据就根本不会走到生成。
        ledger = coverage_kernel.ledger(
            root_task_id=row.root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
            enumeration_state=_enumeration_state(row), entries=ordered,
            conflicts=coverage_kernel.merge_conflicts(divergences, answer["conflicts"]))
        coverage_kernel.validate_ledger(ledger)
    # 结果文档 = 交付字节的规范原文（**不含摘要字段本身**）。摘要是对这份文档的
    # content_digest，客户端下载后重算它才允许 ack；文档有界且不含正文摘录。
    document = {
        **answer,
        "operation": task_spec.get("operation"),
        "search_mode": row.search_mode,
        "retrieval_completeness": ledger["retrieval_completeness"],
        "evidence_sufficiency": ledger["evidence_sufficiency"],
        "counts": ledger["counts"], "coverage_ref": row.root_task_id,
        "conflicts": ledger.get("conflicts", []),
        "evidence": fused, "unretrieved_targets": unretrieved,
    }
    result = {**document, "result_manifest_digest": plans.digest(document),
              _ATTRIBUTED_FIELD: sorted({_evidence_key(item) for item in attributed},
                                        key=lambda key: tuple(str(part) for part in key))}
    await session.execute(delete(CoverageEntry).where(
        CoverageEntry.root_task_id == row.root_task_id))
    ledger_row = await session.get(CoverageLedger, row.root_task_id)
    if ledger_row is None:
        ledger_row = CoverageLedger(root_task_id=row.root_task_id, created_at=now)
        session.add(ledger_row)
    ledger_row.scope_ref = row.scope_id
    ledger_row.search_mode = row.search_mode
    ledger_row.enumeration_state = ledger["enumeration_state"]
    ledger_row.retrieval_completeness = ledger["retrieval_completeness"]
    ledger_row.evidence_sufficiency = ledger["evidence_sufficiency"]
    ledger_row.counts_json = ledger["counts"]
    ledger_row.manifest_digest = row.scope_digest or plans.digest(row.scope_id)
    ledger_row.updated_at = now
    await session.flush()   # entries 有指向 ledger 的外键，父行先落
    session.add_all([_entry_row(row.root_task_id, entry) for entry in ordered])
    # **终态围栏放在这里，而且是条件 UPDATE**：`_answer_result` 可能跑了很久
    # （远端生成），取消/清扫可能在这期间落库；只按内存里的 `row` 写终态就是
    # 用旧快照覆盖别人的终态。条件 UPDATE 命中 0 行 = 有人在执行期间改了终态，
    # 整笔（覆盖账本、交付、事件）rollback 丢弃，返回那个真实终态。
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == row.root_task_id,
            FederationRequest.status == "running").values(
            coverage_ref=row.root_task_id, status=status,
            retrieval_completeness=ledger["retrieval_completeness"],
            evidence_sufficiency=ledger["evidence_sufficiency"],
            result_json=result, error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        await session.rollback()
        return _status_output(await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    await session.refresh(row)
    if status == "succeeded":
        await _deliver_result(session, row, document=document,
                              digest=result["result_manifest_digest"], now=now)
    await _append_event(session, row.root_task_id,
                        _EVENT_COMPLETED if status == "succeeded" else _EVENT_FAILED, {
                            "retrieval_completeness": ledger["retrieval_completeness"],
                            "evidence_sufficiency": ledger["evidence_sufficiency"],
                            "counts": ledger["counts"], "delivery_id": row.delivery_id,
                            "error": error}, now=now)
    if row.delivery_id:
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_PENDING, {
            "delivery_id": row.delivery_id,
            "result_manifest_digest": result["result_manifest_digest"]}, now=now)
    await session.commit()
    return _status_output(row)


async def execute_task(session: AsyncSession, actor: Actor, root_task_id: str, *,
                       plan_digest: str, idempotency_key: str, now: datetime,
                       http, index) -> tuple[dict, bool]:
    """以幂等键受理已批准计划；同键重放不再发任何请求。

    返回 `(status, created)`。**队列模式（默认）**：created=True 时受理与
    `federation_plan` 队列任务在同一个事务里提交，HTTP 端点据此回 202，
    真正的执行由 corpus-worker 推进 —— 受理进程重启不会留下永远 running
    的任务（企业边界 7）。created=False 是同键重放，回 200 + 权威状态。
    已取消的任务 409 `task_cancelled`（终态不许被新幂等键复活）。

    `FEDERATION_EXECUTION_INLINE=true` 时保持旧的请求内执行行为（回 200）。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status == "cancelled":
        # 取消是终态：一个还没提交过的已批准计划也不许凭新的幂等键"复活"。
        # 与 resume 同一判据 —— 重跑要另起一条新任务。
        raise APIError(409, "task was cancelled; create a new task instead of submitting",
                       "invalid_request_error", "task_cancelled")
    if row.plan_digest != plan_digest:
        raise APIError(409, "submitted digest does not match the stored plan revision",
                       "invalid_request_error", "plan_changed")
    if row.planning_state != "approved" or not row.execution_consent_json:
        raise _egress_denied("task has no approved execution consent")
    if row.idempotency_key:
        if row.idempotency_key != idempotency_key:
            raise APIError(409, "root task already accepted under another idempotency key",
                           "invalid_request_error", "idempotency_conflict")
        return _status_output(row), False
    validate_execution_consent(row.execution_consent_json, now=now)
    try:
        plans.validate_plan(row.plan_json, row.task_spec_json,
                            local_node_id=federation.local_node_id(), now=_ts(now))
    except ApplicationError as exc:
        if exc.code == "consent_expired":
            raise _egress_denied("plan or budget has expired; re-plan instead") from None
        raise federation.api_error(exc) from None
    row.idempotency_key = idempotency_key
    row.delegation_generation = int(row.delegation_generation or 0) + 1
    row.status = "running"
    row.updated_at = now
    try:
        # 事件里那次 `SELECT max(seq)` 会触发 autoflush；必须放在 try 内，
        # 否则同键异任务时唯一约束在事件追加处就炸成 500，409 分支永远到不了。
        await _append_event(session, row.root_task_id, _EVENT_STARTED, {
            "plan_digest": plan_digest, "generation": row.delegation_generation}, now=now)
        if not settings.federation_execution_inline:
            # **状态行与队列任务同一个事务**：两个半截状态（running 没任务、
            # 任务没 running）都不可能出现。dedupe 键取 root —— 同一 root
            # 的重复受理会先在上面按 idempotency_key 幂等返回。
            await queue.enqueue(
                session, kind="federation_plan",
                payload={"root_task_id": root_task_id, "retry_only": False,
                         "actor": federation.actor_binding(actor)},
                organization_id=actor.organization_id,
                dedupe_key=f"federation-request:{root_task_id}")
        await session.commit()
    except IntegrityError:
        # 同一个幂等键已经用去受理了另一个 root task；唯一约束替我们仲裁。
        await session.rollback()
        existing = await session.scalar(select(FederationRequest).where(
            FederationRequest.organization_id == actor.organization_id,
            FederationRequest.idempotency_key == idempotency_key))
        if existing is not None and existing.root_task_id != root_task_id:
            raise APIError(409, "idempotency key already accepted for another task",
                           "invalid_request_error", "idempotency_conflict") from None
        raise
    if not settings.federation_execution_inline:
        return _status_output(row), True
    try:
        result = await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                     retry_only=False)
        return result, True
    except APIError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        raise
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        raise federation.api_error(exc) from None


async def run_queued(session: AsyncSession, actor: Actor, root_task_id: str, *,
                     retry_only: bool, now: datetime, http, index) -> dict:
    """worker 入口：推进一个已受理的协调任务（kind=`federation_plan`）。

    执行权仍来自持久行：状态不是 running 就直接返回当前状态，绝不把
    已取消/已终态的任务再跑一遍。已知业务错误（APIError/ApplicationError）
    落成 `failed` 并返回；未知异常交给 runner 重试（那才是队列的用武之地）。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status != "running":
        return _status_output(row)
    try:
        return await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                   retry_only=retry_only)
    except APIError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        return _status_output(await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        return _status_output(await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))


async def resume(session: AsyncSession, actor: Actor, root_task_id: str, *, now: datetime,
                 http, index) -> dict:
    """重新判权后补做未完成目标。旧授权过期就 403/410，不隐式扩权。

    **队列模式（默认）**：状态改 running + 排 `federation_plan`（retry_only=True）
    后返回，端点回 202；执行由 worker 推进。同 root 已有的未完成任务靠 dedupe
    键挡住第二次排队 —— 那次运行会读到最新的行状态并只补未完成目标。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status == "cancelled":
        # 取消是显式终态：resume 不得把它"复活"回 running。任何重跑都必须
        # 走一条新的任务（新授权、新覆盖分母），而不是拿旧计划接着跑。
        raise APIError(409, "task was cancelled; create a new task instead of resuming",
                       "invalid_request_error", "task_cancelled")
    if row.planning_state != "approved" or not row.plan_json:
        raise _egress_denied("task has no approved plan to resume")
    manifest = row.scope_manifest_json
    if manifest is not None and plans.instant(manifest["valid_until"]) <= _ts(now):
        raise APIError(410, "scope has expired; re-enumerate instead of resuming",
                       "invalid_request_error", "scope_expired")
    validate_execution_consent(row.execution_consent_json, now=now)
    try:
        plans.validate_plan(row.plan_json, row.task_spec_json,
                            local_node_id=federation.local_node_id(), now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    row.delegation_generation = int(row.delegation_generation or 0) + 1
    row.status = "running"
    row.updated_at = now
    await _append_event(session, row.root_task_id, _EVENT_RESUMED, {
        "generation": row.delegation_generation}, now=now)
    if not settings.federation_execution_inline:
        await queue.enqueue(
            session, kind="federation_plan",
            payload={"root_task_id": root_task_id, "retry_only": True,
                     "actor": federation.actor_binding(actor)},
            organization_id=actor.organization_id,
            dedupe_key=f"federation-request:{root_task_id}")
    await _commit(session)
    if not settings.federation_execution_inline:
        return _status_output(row)
    try:
        return await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                   retry_only=True)
    except APIError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        raise
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        raise federation.api_error(exc) from None


async def cancel(session: AsyncSession, actor: Actor, root_task_id: str, *,
                 now: datetime) -> dict:
    """显式、幂等取消：终态不被改写；未完成目标留在分母里记 not_attempted。

    落 `status="cancelled"`（契约 task_status 的终态）。**同时取消队列里的
    `federation_plan` 任务**：不然 worker 领取后只会看到终态空转，而且它
    占着并发位；已开跑的那次会因 generation 前移而写不进结果。

    - `succeeded` / `failed` / `cancelled` 都是已落定的结局，原样返回。
      把 failed 改成 cancelled 会把"系统做砸了"改写成"用户不要了"（契约要求
      两者分开），还会抹掉逐目标的 denied/unreachable 原因、顺手封死 resume。
    - 只改**可重做**的目标（`_RETRYABLE_STATES`）；succeeded / unsupported /
      denied / revoked 是本任务无法靠重做改变的结论，照实保留。
    - 覆盖账本行跟着逐目标记录重算，不留一份过期的 counts。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status in ("succeeded", "failed", "cancelled"):
        return _status_output(row)
    entries = list(await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == root_task_id)))
    if not entries:
        # 还没跑过：先把计划分母落成 entries，再逐条标 not_attempted。
        # coverage_entries 有指向 ledger 的外键，父行必须先落。
        node = federation.local_node_id()
        manifest = row.scope_manifest_json
        query_digest = plans.content_digest((row.task_spec_json.get("query") or "").encode())
        ledger_row = CoverageLedger(
            root_task_id=root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
            enumeration_state=_enumeration_state(row), retrieval_completeness="partial",
            evidence_sufficiency="insufficient", counts_json={}, manifest_digest="",
            created_at=now, updated_at=now)
        session.add(ledger_row)
        await session.flush()
        for target in _all_targets(row.task_spec_json, manifest, node):
            session.add(_entry_row(root_task_id, coverage_kernel.new_entry(
                _target_key(target), row.scope_id, query_digest)))
        await session.flush()
        entries = list(await session.scalars(select(CoverageEntry).where(
            CoverageEntry.root_task_id == root_task_id)))
    marked = 0
    for entry in entries:
        if entry.state not in _RETRYABLE_STATES:
            continue
        entry.state = "not_attempted"
        entry.last_error = "cancelled"
        marked += 1
    ledger = coverage_kernel.ledger(
        root_task_id=root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
        enumeration_state=_enumeration_state(row),
        entries=[_entry_from_row(entry, row.scope_id) for entry in entries],
        conflicts=_recorded_conflicts(row))
    ledger_row = await session.get(CoverageLedger, root_task_id)
    if ledger_row is not None:
        ledger_row.retrieval_completeness = ledger["retrieval_completeness"]
        ledger_row.evidence_sufficiency = ledger["evidence_sufficiency"]
        ledger_row.counts_json = ledger["counts"]
        ledger_row.updated_at = now
    row.status = "cancelled"
    row.error = "cancelled"
    row.updated_at = now
    await _append_event(session, root_task_id, _EVENT_CANCELLED, {"marked": marked}, now=now)
    await _commit(session)
    if not settings.federation_execution_inline:
        await queue.cancel_by_dedupe(session, kind="federation_plan",
                                     dedupe_key=f"federation-request:{root_task_id}")
    return _status_output(row)


async def mark_stalled(session: AsyncSession, root_task_id: str, *, now: datetime,
                       error: str = "coordinator_stalled") -> bool:
    """回收清扫用：把卡在 running 且超过 deadline 的协调任务落成显式失败。

    **状态守卫必须落在 UPDATE 的 WHERE 里**：候选查询与函数各持一道闸，但
    写入本身也必须是条件 UPDATE。SQLite 单连接、PG 的 READ COMMITTED 快照
    都会让"先读后写"拿着旧快照覆盖并发提交的终态 —— 取消发生在读与写之间时，
    一次超时清扫会把用户显式取消的任务改写回 failed。命中 0 行 = 别人已落终态，
    本行不写、事件不追加。

    覆盖账本原样保留（分母不因清扫消失）；真的赢了才追加可见原因的事件。
    """
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == root_task_id,
            FederationRequest.status == "running").values(
            status="failed", error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        return False
    await _append_event(session, root_task_id, _EVENT_FAILED, {"error": error}, now=now)
    return True


async def heartbeat_request(session: AsyncSession, root_task_id: str) -> bool:
    """续协调任务的活性戳（`updated_at`）。返回 False = 已不在 running。

    清扫按 `updated_at` 判"卡死"，而一次 exhaustive 计划可能合法地跑十几分钟；
    `federation_plan` handler 在 `run_queued` 旁边按 `task_heartbeat_seconds`
    调这里，避免把正在推进的任务误杀。
    """
    done = await session.execute(update(FederationRequest).where(
        FederationRequest.root_task_id == root_task_id,
        FederationRequest.status == "running").values(updated_at=utcnow()))
    await session.commit()
    return done.rowcount == 1


# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------

async def read_task(session: AsyncSession, actor: Actor, root_task_id: str) -> dict:
    return _status_output(await _load_request(session, actor, root_task_id))


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


# ---------------------------------------------------------------------------
# 交付回执
# ---------------------------------------------------------------------------

def _delivery_receipt(delivery: FederationDelivery, *, idempotency_key: str) -> dict:
    return {
        "schema": "ddp-evidence/1#DeliveryReceipt",
        "delivery_id": delivery.delivery_id,
        "root_task_id": delivery.root_task_id,
        "step_id": "fuse-1",
        "state": delivery.state,
        "result_manifest_digest": delivery.result_manifest_digest,
        "verified_at": _instant(delivery.verified_at) if delivery.verified_at else None,
        "idempotency_key": idempotency_key,
        "retention": delivery.retention,
    }


async def ack_delivery(session: AsyncSession, actor: Actor, delivery_id: str, *,
                       result_manifest_digest: str, idempotency_key: str,
                       now: datetime) -> dict:
    """本地校验后的幂等确认。

    只有**摘要与已交付结果一致**才可能 confirmed；TTL 到期一律 expired，
    过期件永远不可确认（§8.3）。重复确认返回同一回执。
    每 delivery 的咨询锁把并发确认串行化：两个不同键的确认不许各写一份回执。
    """
    await catalog.lock_key(session, "federation-delivery:" + delivery_id)
    delivery = await session.get(FederationDelivery, delivery_id)
    row = await session.get(FederationRequest, delivery.root_task_id) if delivery else None
    # 与 read_delivery / _load_request 同一可见性判据：同组织的其他成员（非
    # 管理员）拿到 delivery_id 也不许替别人确认 —— 确认会把交付钉成
    # confirmed、绕过 TTL 过期。不可见与不存在同形 404。
    if delivery is None or row is None or row.organization_id != actor.organization_id \
            or (row.actor_id != federation.acting_actor(actor) and not actor.can_manage):
        raise APIError(404, "delivery not found", "invalid_request_error", "delivery_not_found")
    if delivery.state == "confirmed":
        return delivery.receipt_json
    expired = delivery.state == "expired" or (
        delivery.expires_at is not None and as_aware(delivery.expires_at) <= now)
    key = str((delivery.receipt_json or {}).get("idempotency_key") or idempotency_key)
    if expired:
        delivery.state = "expired"
        delivery.updated_at = now
        row.delivery_state = "expired"
        row.updated_at = now
        delivery.receipt_json = _delivery_receipt(delivery, idempotency_key=key)
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_EXPIRED, {
            "delivery_id": delivery.delivery_id}, now=now)
        await _commit(session)
        return delivery.receipt_json
    if delivery.result_json is None:
        # 没有可校验的字节（超界未持久化、或历史交付）就不许确认：客户端
        # 下载不到 result，拿什么"本地校验过"都不成立。
        raise APIError(409, "delivery has no stored result bytes to verify",
                       "invalid_request_error", "input_not_verified")
    if result_manifest_digest != delivery.result_manifest_digest:
        raise APIError(409, "submitted result manifest digest does not match the delivered result",
                       "invalid_request_error", "input_not_verified")
    delivery.state = "confirmed"
    delivery.verified_at = now
    delivery.updated_at = now
    row.delivery_state = "confirmed"
    row.updated_at = now
    delivery.receipt_json = _delivery_receipt(delivery, idempotency_key=key)
    await _append_event(session, row.root_task_id, _EVENT_DELIVERY_CONFIRMED, {
        "delivery_id": delivery.delivery_id,
        "result_manifest_digest": delivery.result_manifest_digest}, now=now)
    await _commit(session)
    return delivery.receipt_json


async def read_delivery(session: AsyncSession, actor: Actor, delivery_id: str, *,
                        now: datetime) -> dict:
    """读取交付字节（有界 JSON）。**读取不是确认**。

    - 未知/不可见同形 404（不给出存在性探测口）；
    - 未确认且过 TTL：先把交付置 expired 并写事件，再回 410
      `delivery_expired` —— 本地据此把状态标成 expired，绝不显示"已保存"；
    - confirmed 是终态，不再受 TTL 影响（客户端已经校验并持有）；
    - `result` 是规范文档，`content_digest(canonical result)` 必须等于
      `result_manifest_digest`；超界未持久化时是 null，客户端必须拒绝确认。
    """
    delivery = await session.get(FederationDelivery, delivery_id)
    row = await session.get(FederationRequest, delivery.root_task_id) if delivery else None
    if delivery is None or row is None or row.organization_id != actor.organization_id \
            or (row.actor_id != federation.acting_actor(actor) and not actor.can_manage):
        raise APIError(404, "delivery not found", "invalid_request_error", "delivery_not_found")
    if delivery.state not in ("confirmed", "expired") \
            and delivery.expires_at is not None and as_aware(delivery.expires_at) <= now:
        delivery.state = "expired"
        delivery.updated_at = now
        row.delivery_state = "expired"
        row.updated_at = now
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_EXPIRED, {
            "delivery_id": delivery.delivery_id}, now=now)
        await _commit(session)
    if delivery.state == "expired":
        raise APIError(410, "delivery has expired and was never confirmed locally",
                       "invalid_request_error", "delivery_expired")
    return {
        "delivery_id": delivery.delivery_id,
        "root_task_id": delivery.root_task_id,
        "state": delivery.state,
        "result_manifest_digest": delivery.result_manifest_digest,
        "result": delivery.result_json,
        "expires_at": _instant(delivery.expires_at) if delivery.expires_at else None,
    }
