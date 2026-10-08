"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_models import (
    CoverageEntry,
    FederationRequest,
    FederationTaskEvent,
)
from sqlalchemy.exc import IntegrityError
from ddp_contracts.enums import TASK_EVENT_TYPE_VALUES
from ddp_corpus.models import as_aware, new_id
from ddp_core.application import coverage as coverage_kernel, plans
from ddp_corpus import federation, federation_budget
from sqlalchemy import func, select
import hashlib
import json
import re

#: 取数目标统一用这个 operation 进覆盖账本；与节点能力清单的 operation 同名。
RETRIEVAL_OPERATION = "corpus.retrieve"

#: 固定资源目标的 operation：本节点只对指定版本做授权定位，不把它当集合。
LOCATE_OPERATION = "corpus.locate"

#: fast 的有界候选数（§7.2）。exhaustive 不受它限制。
FAST_CANDIDATE_LIMIT = 8

MAX_ANSWER_CANDIDATES = 8

#: 规划时因来源集合的转交策略（T83）排除了远端生成候选、又没有别的可用生成节点：
#: 内部字段记下原因，执行时答案原因写 `source_policy_denied` 而不是"没有模型"。
GENERATION_WITHHELD_FIELD = "_generation_withheld"

SOURCE_POLICY_DENIED = "source_policy_denied"

#: 命中探测复用（P6 缓存回执）时覆盖账本 search_profile 上的可见标记。
#: 它不声称"本次真的探测过" —— 回执本身的 observed_at 才是新鲜度事实。
CACHED_PROBE_PROFILE = "cached_probe_receipt"

#: 本地枚举的 scope 有效期上限：比探索许可短，到期重新枚举。
SCOPE_TTL_SECONDS = 900

#: 交付暂存期。TTL 到期未确认 -> expired，不得再显示"已保存本地"。
DELIVERY_TTL_SECONDS = 86400

#: Every remote execution uses bounded exponential status polling. The approved
#: deadline wins over the next delay; timeout preserves the peer's execution so
#: resume reconciles the same business key instead of replaying an uncertain write.
#: Polls remain prepaid HTTP requests, but minutes-long CPU generation must not
#: consume the root allowance through one-second busy waiting.
PEER_POLL_INTERVAL_SECONDS = 1.0

PEER_POLL_MAX_INTERVAL_SECONDS = 15.0

#: 本地执行的等待上限。本地目标现在也排在持久队列里（`federation_execute`），
#: 协调者在拿到回执后等它落终态；超时记 `local_execution_timeout` 并保留
#: 覆盖缺口，绝不挂住协调者任务。本地轮询是本库读，不占根预算；间隔同样 ≥1s
#: 给真实 CPU 任务让路，不空转。
LOCAL_POLL_DEADLINE_SECONDS = 20.0

LOCAL_POLL_INTERVAL_SECONDS = 1.0

#: 每个目标一份检索结果的字节预算（证据信封 + locator，够宽但有限）。
EVIDENCE_BYTES_PER_TARGET = 64 * 1024

#: 答案生成的 token 上限（`RootBudget.max_generation_tokens`）。本地生成或
#: 远端 answer 委托就绪时进根预算；0 表示这份计划不生成。没有上游 tokenizer，
#: 计数用本仓共享的 `ddp_core.tokenize.tokens`（确定性、可复核）；它是**上限
#: 口径**，不是模型侧的真实 token 数。
GENERATION_TOKEN_BUDGET = 1024

#: 会带生成步骤的协调者 operation（契约 `federation_task_operation` 的生成子集）。
ANSWER_OPERATION = "rag.answer.cited"

#: 按固定原始证据生成版本化 Wiki 的协调者 operation（`requirements.wiki` 严格
#: 校验已在 `plans.validate_spec` 落地；这里只复用它的形状，不另起契约）。
WIKI_OPERATION = "wiki.pages"

#: 任一需要生成 token 预算的 operation（answer 与 wiki_pages 都要调模型）。
GENERATION_OPERATIONS = frozenset({ANSWER_OPERATION, WIKI_OPERATION})

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
    })
    if manifest.get("node_routes"):
        body["node_routes"] = [
            {"node_id": item["node_id"], "via_node_ids": item["via_node_ids"]}
            for item in manifest["node_routes"]]
    body.update({
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
    # 事件类型进契约 `task_event_type`：界面按它显示文案。未声明的类型是编程错误。
    if type_ not in TASK_EVENT_TYPE_VALUES:
        raise ValueError(f"undeclared task event type: {type_!r}")
    current = await session.scalar(select(func.max(FederationTaskEvent.seq)).where(
        FederationTaskEvent.root_task_id == root_task_id))
    session.add(FederationTaskEvent(id=new_id(), root_task_id=root_task_id,
                                    seq=int(current or 0) + 1, type=type_,
                                    payload=payload, created_at=now))

def _concurrent_write() -> APIError:
    return APIError(409, "concurrent write for the same task", "invalid_request_error",
                    "idempotency_conflict")

def _is_unique_violation(exc: IntegrityError) -> bool:
    """True only for concurrent-write unique violations (PG 23505 / SQLite UNIQUE).

    PG 报告 `duplicate key value violates unique constraint ...`（SQLSTATE
    23505），SQLite 报告 `UNIQUE constraint failed: ...`。外键（23503）、
    非空（23502）、CHECK（23514）是程序或数据错误，必须继续以 5xx 可见 ——
    把它们翻译成 409 会让调用方原样重试一个永远过不了的写。
    """
    orig = exc.orig
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if pgcode is not None:
        return str(pgcode) == "23505"
    text = str(orig) if orig is not None else str(exc)
    lowered = text.lower()
    return "unique constraint failed" in lowered or "duplicate key" in lowered

async def _commit(session: AsyncSession) -> None:
    """提交协调者写路径；并发写的唯一约束冲突映射成 409，而不是裸 500。

    事件流的 `(root_task_id, seq)` 唯一约束是并发兜底。正常路径由每 root 的
    咨询锁串行化，真撞上时答案是"同一任务已有并发写"，不是服务端错误。
    只有唯一约束冲突才翻译：外键/非空/CHECK 失败原样重抛（5xx）。
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not _is_unique_violation(exc):
            raise
        raise _concurrent_write() from None

async def _status_output(session: AsyncSession, row: FederationRequest) -> dict:
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
        "used_budget": await federation_budget.ledger_used(
            session, root_task_id=row.root_task_id, organization_id=row.organization_id),
        "result": _public_result(row.result_json),
        "error": row.error,
        "scope_ref": row.scope_id or None,
        "created_at": _instant(row.created_at),
        "updated_at": _instant(row.updated_at),
    }

def _target_digest(target: dict) -> str:
    return hashlib.sha256(plans.canonical_bytes(target)).hexdigest()


def _entry_key(entry: dict) -> tuple[str, str]:
    """In-memory key for one (target, subquery_digest) ledger entry."""
    return (_target_digest(entry["target_key"]), entry["query_or_subquery_digest"])


def _coverage_key(target: dict, digest: str) -> tuple[str, str]:
    """In-memory key for one (target, subquery_digest) ledger slot."""
    return (_target_digest(target), digest)

def _target_key(target: dict) -> dict:
    return {"origin_node_id": target["origin_node_id"],
            "collection_id": target["collection_id"], "operation": target["operation"]}

def _target_identity(target: dict) -> tuple[str, str, str]:
    return (target["origin_node_id"], target["collection_id"], target["operation"])

#: A continuation revision renames every step (`r2-retrieve-1`) and re-indexes retrieves.
_REVISION_PREFIX = re.compile(r"^r\d+-")

def _reservation_step(step: dict) -> str:
    """The logical plan step a once-only allowance (hops, generation tokens) belongs to.

    Fast continuations stage new plan revisions of the same root: step ids gain an
    `r{n}-` prefix and retrieve indexes shift as targets are added. A retrieve step's
    allowance belongs to its target (executor + collection), any other step's to its id
    without the revision prefix; otherwise every continuation re-reserves the root's whole
    generation cap and can never answer with the evidence it fetched (F14).
    """
    if step["operation"] == "retrieve":
        target = next((ref for ref in step.get("fixed_inputs") or [] if ref != "query"), "")
        return f"retrieve:{step['executor_node_id']}:{target}"
    return _REVISION_PREFIX.sub("", step["step_id"])

def _entry_row(root_task_id: str, entry: dict) -> CoverageEntry:
    return CoverageEntry(
        root_task_id=root_task_id, target_digest=_target_digest(entry["target_key"]),
        target_key_json=entry["target_key"], query_digest=entry["query_or_subquery_digest"],
        state=entry["state"], probe_refs_json=list(entry.get("probe_receipts") or []),
        actual_index_revision=entry.get("actual_index_revision"),
        search_profile=entry.get("search_profile"), attempts=int(entry.get("attempts") or 0),
        last_error=entry.get("last_error"), evidence_refs_json=list(entry.get("evidence_refs") or []),
        used_budget_json=dict(entry.get("used_budget") or {"requests": 0, "bytes": 0}),
        exclusion_basis=entry.get("exclusion_basis"), reported_by=entry.get("reported_by"))

def _entry_from_row(row: CoverageEntry, scope_ref: str) -> dict:
    entry = {
        "target_key": row.target_key_json, "scope_ref": scope_ref,
        "query_or_subquery_digest": row.query_digest, "state": row.state,
        "probe_receipts": list(row.probe_refs_json or []),
        "actual_index_revision": row.actual_index_revision,
        "search_profile": row.search_profile, "attempts": int(row.attempts or 0),
        "last_error": row.last_error, "evidence_refs": list(row.evidence_refs_json or []),
        "used_budget": dict(row.used_budget_json or {"requests": 0, "bytes": 0}),
        "exclusion_basis": row.exclusion_basis,
    }
    if row.reported_by is not None:
        entry["reported_by"] = row.reported_by
    return entry

def _enumeration_state(row: FederationRequest) -> str:
    manifest = row.scope_manifest_json
    if manifest is not None:
        return str(manifest.get("enumeration_state") or "partial")
    return "sealed"   # fixed_resources：固定列表就是完整分母

def _public_result(result: dict | None) -> dict | None:
    """状态出口只出结果文档字段；内部簿记（`_` 开头）不外泄，无公开字段时为 null。"""
    if not result:
        return None
    public = {key: value for key, value in result.items() if not str(key).startswith("_")}
    return public or None

def _recorded_conflicts(row: FederationRequest) -> list[dict]:
    """结果文档里持久化的矛盾记录（协调者写入时已校验）；读路径据此复原冲突轴。

    覆盖读取是从逐目标记录重算的；矛盾不在逐目标记录里，不带上它，GET coverage
    会把刚写成 conflicting 的任务重新算回 sufficient_by_policy。
    """
    return coverage_kernel.merge_conflicts((row.result_json or {}).get("conflicts") or [])

def _answer_skeleton() -> dict:
    """答案字段的公共骨架（与远端执行者共用；见 `federation.answer_skeleton`）。"""
    return federation.answer_skeleton()

def _unavailable_answer(reason: str) -> dict:
    """生成没发生/没得用的显式原因。`local_model_missing` 与
    `insufficient_evidence` 也要走这里，不能是沉默的空值。"""
    return federation.unavailable_answer(reason)

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

# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------


async def _revocation_sweep(session: AsyncSession, actor: Actor, *,
                            fused: list[dict], now: datetime) -> str | None:
    """读路径实时复查：本地证据逐条 resolve，任一条撤销即返回 machine code。

    远端证据按存它的组织/调用者绑定信任，不用本地 ACL 复核；本地证据 404 视为
    不可见（同形，不泄露存在性），`source_revoked`/withdrawn（410）则 fail-closed。
    返回 None 表示无已知撤销，否则返回 `source_revoked`（调用方显式失败）。
    """
    node = federation.local_node_id()
    for item in fused:
        if item.get("origin_node_id") != node:
            continue
        try:
            await federation.resolve_evidence(
                session, actor, evidence_ref=str(item.get("evidence_id") or ""),
                now=now)
        except APIError as exc:
            if exc.code == "source_revoked":
                return "source_revoked"
    return None
