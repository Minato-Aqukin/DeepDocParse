"""T64 探针证据留存：pin = "还能 resume 或还能 continuation"，二者任一成立即保留。

Main 第二轮复核的结论：fast continuation 不需要旧 plan / 旧 execution
consent —— `_resume_continuation_gate` 调 `create_plan` 建新计划（新有效期、
新审批），新计划的门是 scope manifest + exploration consent（`create_plan`
依次调 `validate_exploration_consent` 与 `_validate_manifest`，任一过期即拒）。
所以 plan 过期但 manifest + exploration 仍有效的 succeeded/failed fast 任务
仍可续批，settled 目标的 carried probe_refs 还要读正文。只看旧 plan 有效期
的规则会在这个方向 under-pin。

- (a) plain resume：非 cancelled + min(plan.valid_until, plan.budget.deadline,
  execution_consent.valid_until) 还没过截止线 —— 旧计划直接重跑/补做；
- (b) continuation：非 cancelled + 非 fixed_resources + task_spec
  search_policy.mode == "fast" + min(scope_manifest.valid_until,
  exploration_consent.valid_until) 还没过截止线 —— 续批建新计划。
  manifest 缺失（固定资源任务）时 (b)恒假。

任一成立即 pin 该任务 plan `steps[].probe_refs` 与覆盖账本 `probe_refs_json`
引用的行。截止线两侧全过 + cancelled → 剥离 `evidence`，行/状态/摘要保留。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application.plans import instant
from ddp_core.application.ports import ApplicationError
from ddp_corpus.config import settings
from ddp_corpus.federation_models import CoverageEntry, FederationProbe, FederationRequest
from ddp_corpus.models import as_aware


def _plan_probe_refs(plan: dict | None) -> set[str]:
    """从一份持久化计划里取出全部 `probe_refs` 引用的本地行 id。"""
    refs: set[str] = set()
    if not isinstance(plan, dict):
        return refs
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return refs
    for step in steps:
        if not isinstance(step, dict):
            continue
        for probe_id in step.get("probe_refs") or []:
            if isinstance(probe_id, str) and probe_id:
                refs.add(probe_id)
    return refs


def _earliest(*values: str | None) -> float | None:
    """若干 RFC3339 instant 取最早（epoch 秒）；缺失/解析失败即 None。"""
    stamps = []
    for value in values:
        if not value:
            return None
    try:
        stamps = [instant(value) for value in values]
    except (ApplicationError, AttributeError, TypeError, ValueError):
        return None
    return min(stamps)


def _resume_floor(row) -> float | None:
    """(a) plain resume（旧计划直接重跑）的有效期下限：三份有效期取最早。

    对应 `resume` 在 continuation gate 之前的拒绝链：execution consent 过期 →
    403 `egress_denied`；plan 或 budget 过期 → `consent_expired`。注意 gate
    在这些检查**之后**才跑：旧 plan 过期时 resume 在 gate 前就被拒绝，
    continuation 够不着 —— 所以 plan/budget/execution 任一过期即 (a) 死，
    (b) 的判断完全独立（见 `_continuation_floor`），二者是或关系。
    scope 也在 resume 链里，但 scope 过期同时杀死 (b)；scope 单独放 (b) 里，
    这里不重复收它（或关系下结果相同）。
    """
    plan = getattr(row, "plan_json", None) or {}
    if not isinstance(plan, dict):
        return None
    budget = plan.get("budget") or {}
    consent = getattr(row, "execution_consent_json", None) or {}
    if not isinstance(consent, dict):
        return None
    return _earliest(plan.get("valid_until"),
                     budget.get("deadline") if isinstance(budget, dict) else None,
                     consent.get("valid_until"))

def _continuation_floor(row) -> float | None:
    """(b) fast continuation 的有效期下限：建新计划的两份有效期取最早。

    对应 `create_plan` 的门：`validate_exploration_consent`（过期 403
    `egress_denied`）+ `_validate_manifest`（过期 410 `scope_expired`）。
    只有 fast + 非固定资源任务能进 `_resume_continuation_gate`，其余恒 None。
    manifest 缺失（固定资源任务、本地枚举无 manifest 的形态）→ None。
    """
    spec = getattr(row, "task_spec_json", None) or {}
    if not isinstance(spec, dict):
        return None
    policy = spec.get("search_policy") or {}
    scope = spec.get("resource_scope") or {}
    if not isinstance(policy, dict) or not isinstance(scope, dict):
        return None
    if policy.get("mode") != "fast" or scope.get("kind") == "fixed_resources":
        return None
    manifest = getattr(row, "scope_manifest_json", None) or {}
    consent = getattr(row, "exploration_consent_json", None) or {}
    if not isinstance(manifest, dict) or not isinstance(consent, dict):
        return None
    return _earliest(manifest.get("valid_until"), consent.get("valid_until"))


class _RowShim:
    """有效期判定只读 JSON 列；用轻量替身避免整行加载。"""

    def __init__(self, plan_json, scope_manifest_json, exploration_consent_json,
                 execution_consent_json, task_spec_json):
        self.plan_json = plan_json
        self.scope_manifest_json = scope_manifest_json
        self.exploration_consent_json = exploration_consent_json
        self.execution_consent_json = execution_consent_json
        self.task_spec_json = task_spec_json


async def _pinned_probe_ids(session: AsyncSession, *, cutoff: datetime) -> set[tuple[str, str]]:
    """截止线之前仍可 resume 或 continuation 的任务引用的探针行 `(组织, 行 id)`。

    有界扫描：只看组织列、probe 引用列与有效期列，不碰 evidence 载荷；
    `cancelled` 直接排除（resume 明确拒绝复活它）。
    pin 以 `(organization_id, probe_id)` 配对：任务只能 pin 住自己组织的
    行，跨组织同名 id 只是字符串相同（不变量 8：每次查询都有组织边界）。
    """
    cutoff_ts = as_aware(cutoff).timestamp()
    rows = (await session.execute(select(
        FederationRequest.root_task_id,
        FederationRequest.organization_id,
        FederationRequest.plan_json,
        FederationRequest.scope_manifest_json,
        FederationRequest.exploration_consent_json,
        FederationRequest.execution_consent_json,
        FederationRequest.task_spec_json,
    ).where(FederationRequest.status != "cancelled"))).all()
    resumable_org: dict[str, str] = {}
    pinned: set[tuple[str, str]] = set()
    for root_task_id, organization_id, plan_json, manifest_json, exploration_json, execution_json, spec_json in rows:
        shim = _RowShim(plan_json, manifest_json, exploration_json, execution_json, spec_json)
        resume_floor = _resume_floor(shim)
        continuation_floor = _continuation_floor(shim)
        floors = [floor for floor in (resume_floor, continuation_floor) if floor is not None]
        # (a)/(b) 是或关系：任一门还开着就 pin。用 max —— min 会要求两门同时开。
        if not floors or max(floors) < cutoff_ts:
            continue
        resumable_org[root_task_id] = organization_id
        pinned |= {(organization_id, ref) for ref in _plan_probe_refs(plan_json)}
    if resumable_org:
        # 覆盖账本是第二引用源：续批 settled 目标的 carried receipts 落在这里。
        # 只收仍走得动的任务的行，不把已死任务的引用算成 pin。账本行没有
        # 组织列，引用归属其 root 任务行的组织（账本随任务建）。
        covered = (await session.execute(select(
            CoverageEntry.root_task_id, CoverageEntry.probe_refs_json).where(
            CoverageEntry.root_task_id.in_(list(resumable_org))))).all()
        for root_task_id, refs in covered:
            if isinstance(refs, list):
                org = resumable_org[root_task_id]
                pinned.update((org, ref) for ref in refs
                              if isinstance(ref, str) and ref)
    return pinned


async def sweep_probe_evidence(session: AsyncSession, *, now: datetime,
                               limit: int = 500) -> int:
    """剥离"过期且其任务再也 resume/continuation 不了"的探针证据载荷。

    条件三选一缺一不可：`expires_at` 已过留存窗口（`now - retention`）、行里
    真的有 `evidence` 载荷、没有任何截止线前仍走得动的本组织非 cancelled 任务在
    plan `probe_refs` 或覆盖账本 `probe_refs_json` 里引用它。行主键/状态/
    摘要/幂等键与回执元数据保留 —— 覆盖账本引的是行 id，删行等于造空洞。
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    retention = settings.federation_probe_evidence_retention_seconds
    cutoff = now - timedelta(seconds=retention)
    pinned = await _pinned_probe_ids(session, cutoff=cutoff)
    expired = (await session.execute(select(FederationProbe).where(
        FederationProbe.expires_at < cutoff
    ).order_by(FederationProbe.expires_at, FederationProbe.probe_id
               ).limit(limit))).scalars().all()
    stripped = 0
    for row in expired:
        if (row.organization_id, row.probe_id) in pinned:
            continue
        stored = row.result_json or {}
        if not stored.get("evidence"):
            continue
        row.result_json = {**stored, "evidence": []}
        stripped += 1
    await session.commit()
    return stripped
