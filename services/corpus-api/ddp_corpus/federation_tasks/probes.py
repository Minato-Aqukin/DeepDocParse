"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ddp_corpus.federation_models import FederationRequest
    from ddp_corpus.federation_peers import PeerDirectory

from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import routing
from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_models import FederationProbe
from sqlalchemy.exc import IntegrityError
from ddp_corpus.federation_peers import PeerUnavailable
from ddp_corpus import cache, federation
import hashlib
from ddp_core.application import plans
from sqlalchemy import select
from datetime import datetime, timedelta

from ddp_corpus.federation_tasks.common import (LOCATE_OPERATION, _target_key)
from ddp_corpus.federation_tasks.targets import (
    _peer_probe_denial,
    _probe_policy_revision,
    _registry_revisions,
)

# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _probe_key(root_task_id: str, target: dict, revision: int = 1) -> str:
    parts = [root_task_id, target["origin_node_id"], target["collection_id"],
             target["operation"]]
    if revision > 1:
        parts.append(revision)
    return "plan:" + hashlib.sha256(plans.canonical_bytes(parts)).hexdigest()

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
    # 枚举参数决定（"search"、"corpus.retrieve" 都可能），这里一律按集合检索
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
                                request_digest: str, now: datetime,
                                policy_revision: str | None = None) -> str:
    """把远端回执存成**本节点**的一行探测（org 作用域），GET/覆盖路径才统一。

    行 id 由本节点从 (actor, 幂等键) 确定性推导；`result` 里的 probe_id 仍是
    对端的真实回执 id —— 不篡改来源身份。`policy_revision`（记录侧）
    存进落库信封顶层 `result_json["policy_revision"]`（与
    `cache._recorded_policy_revision` 的 stored 来源一致），缺省不存（旧行保持
    legacy 形状）；它绝不进入任何摘要比对。
    """
    probe_row_id = "probe-" + hashlib.sha256(plans.canonical_bytes(
        [actor.organization_id, actor.kind, actor.id, actor.principal_id, key])
    ).hexdigest()[:26]
    existing = await session.get(FederationProbe, probe_row_id)
    if existing is not None:
        return probe_row_id
    retrieval = probe.get("retrieval") or {}
    envelope: dict = {"kind": probe["probe_kind"], "result": probe,
                      "evidence": evidence, "request_digest": request_digest}
    if policy_revision is not None:
        envelope["policy_revision"] = policy_revision
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
                result_json=envelope,
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
                               task_spec_digest: str | None = None,
                               now: datetime) -> tuple[dict | None, str | None, str | None]:
    """冻结的缓存复用 API + **调用者绑定复核**；返回 (回执, 本地行 id, 跳过原因).

    `cache.find_reusable_probe` 的作用域是 organization（冻结的缓存语义）；
    同一组织里不同调用者仍是可见性边界，命中后要把回执归回它那一行的 actor：
    远端回执的行 id 与回执内 `probe_id` 不同，不能按 id 取，因此按同一
    (org, target, collection, kind) 的至多 64 行做内容比对，取到后再核 actor。
    第二个返回值必须是**本地行 id**：计划的 probe_refs 与执行读回都用它，
    用对端回执里的 `probe_id` 会指不到行（覆盖条目会丢回执）。

    第三个返回值是观测性跳过原因（None = 命中）：`cache_miss`（内核无复用行：
    含任务摘要/索引修订/修订/TTL 任一对不上）或 `actor_mismatch`（命中但非
    同一调用者）或 `row_not_found`（命中但本地行比对不上）。

    `task_spec_digest`：透传给 `cache.find_reusable_probe`，
    由 `probe.reusable` 做任务摘要比对（主判据）；不给则回退 `query_digest`
    兼容位（旧行仍可复用）。这里再保留一次调用方侧复核：cache 命中只保证行级
    可复用，归回本地行时的 actor 绑定仍在此处判定。
    """
    found = await cache.find_reusable_probe(
        session, actor, target_key=_target_key(target), query_digest=query_digest,
        index_revision=index_revision,
        policy_revision=_probe_policy_revision(manifest, target["origin_node_id"]),
        task_spec_digest=task_spec_digest, now=now)
    if found is not None and task_spec_digest is not None \
            and found.get("task_spec_digest") != task_spec_digest:
        found = None
        return None, None, "task_spec_digest_mismatch"
    if found is None:
        return None, None, "cache_miss"
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
                return None, None, "actor_mismatch"
            return found, row.probe_id, None
    return None, None, "row_not_found"

async def _probe_targets(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                         targets: list[dict], now: datetime, http, index,
                         peers: PeerDirectory, budget: routing.RootBudget,
                         manifest: dict | None = None,
                         descriptors: dict[tuple[str, str], dict] | None = None,
                         spend=None, unreachable_nodes: dict[str, str] | None = None,
                         ) -> tuple[list[dict], dict, dict]:
    """执行探索许可允许的 Probe。返回 (probes, target_outcome, extra)。

    顺序对每个目标固定：**先看能不能复用已有回执（零外发、零预算）**，再看
    负面缓存（零外发），最后才发真请求。复用只在前端拿到"当前索引修订"时
    才可能发生（描述符给的口径），所以不会拿旧修订的回执冒充当前检索。
    `unreachable_nodes` 是调用点已读出的负面理由（同一修订不再读第二次）。
    """
    task_spec = row.task_spec_json
    consent = row.exploration_consent_json
    scope_ref = row.scope_id
    revision = int(row.plan_revision or 0) + 1
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
    reuse_skipped: dict[str, str] = {}
    for target in targets:
        key = (target["origin_node_id"], target["collection_id"], target["operation"])
        origin = target["origin_node_id"]
        descriptor = (descriptors or {}).get((origin, target["collection_id"]))
        index_revision = descriptor.get("index_revision") if isinstance(descriptor, dict) else None
        # Reuse-gate observability: record WHY each target did or did not
        # take the reuse path. `reuse_skipped` carries one machine reason
        # per non-reused target — no behavior change.
        reuse_skip: str | None = None
        if target["operation"] == LOCATE_OPERATION:
            reuse_skip = "locate_never_reused"
        elif not (isinstance(index_revision, str) and index_revision):
            reuse_skip = "no_descriptor_index_revision"
        if reuse_skip is None:
            found, row_id, inner_skip = await _find_reusable_probe(
                session, actor, target=target, query_digest=query_digest,
                index_revision=index_revision, manifest=manifest,
                task_spec_digest=row.task_spec_digest, now=now)
            if found is not None and row_id is not None:
                # 复用不是新观测：不加 attempts（由执行阶段的账本照实记），
                # 不占探索预算，也绝不落一条冒充本次探测的新行。probe_refs
                # 引用的必须是本地行 id，执行读回才找得到这一行。
                probes.append(found)
                probe_ids[key] = row_id
                reused[key] = row_id
                continue
            reuse_skip = inner_skip or "no_reusable_receipt"
        reuse_skipped[key] = reuse_skip
        request = _probe_request(task_spec=task_spec, consent_ref=consent["consent_id"],
                                 scope_ref=scope_ref, target=target, query=query)
        if origin == node:
            probe_key = _probe_key(row.root_task_id, target, revision)
            try:
                # commit=False：规划持有每 root 的事务级咨询锁，探测在规划
                # 事务里落行、随计划一起提交；中途提交会把锁提前放掉。
                # policy_revision 是观测侧元数据（记录侧）：经
                # run_probe 落库信封顶层（不进 request digest，同键重放幂等）。
                probe = await federation.run_probe(
                    session, actor, request, now=now, http=http, index=index,
                    idempotency_key=probe_key, commit=False,
                    policy_revision=_probe_policy_revision(manifest, origin))
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
        reason = (unreachable_nodes or {}).get(origin)
        if reason is None and node_revision:
            # SCOPED-ONLY read：该任务摘要 + 该目标集合修订双绑定。描述符缺失
            # 时跳过 scoped 读（fail-closed：无修订不断言命中），legacy 三元组
            # 键不再被命中 —— 复用 `_find_reusable_probe` 的 fail-closed 语义。
            descriptor_revision = descriptor.get("index_revision") \
                if isinstance(descriptor, dict) else None
            if isinstance(descriptor_revision, str) and descriptor_revision:
                seen = await cache.get_negative(
                    session, scope_key=negative_scope, node_id=origin,
                    node_revision=node_revision, now=now, query_digest=query_digest,
                    collection_revision=descriptor_revision)
                if seen is not None:
                    reason = str(seen.get("reason") or "unreachable")
        if reason is not None:
            # 命中负面条目：不发一个字节、不占探测预算，也不续命
            # （get_negative 只读，expires_at 原样）。
            outcome[key] = (_negative_state(reason), reason)
            continue
        try:
            # 单次预扣在 spend 内部（独立提交 + 内存 reserve 二合一）：
            # 调用点不再另做 reserve，否则内存扣 2 次、持久只 1 次。
            await spend(kind="probe", amount=1)
            await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(request)))
        except ApplicationError as exc:
            outcome[key] = ("not_attempted", exc.code)
            continue
        try:
            remote = await peers.client(origin).probe(
                request, idempotency_key=_probe_key(row.root_task_id, target, revision))
        except PeerUnavailable as exc:
            if node_revision and (exc.status is None or exc.status == 403):
                # DUAL-WRITE：legacy 三元组键（`_ranking_unreachable_nodes` 仍读
                # 它）+ scoped 键（任务摘要 + 集合修订双绑定）。scoped 那条与
                # 上面的 scoped-only get_negative 同一口径；描述符缺失时只写
                # legacy（fail-closed：无修订不断言作用域命中）。
                descriptor_revision = descriptor.get("index_revision") \
                    if isinstance(descriptor, dict) else None
                await cache.record_negative(session, scope_key=negative_scope,
                                            node_id=origin, node_revision=node_revision,
                                            reason=_negative_reason(exc), now=now)
                if isinstance(descriptor_revision, str) and descriptor_revision:
                    await cache.record_negative(
                        session, scope_key=negative_scope, node_id=origin,
                        node_revision=node_revision, reason=_negative_reason(exc), now=now,
                        query_digest=query_digest,
                        collection_revision=descriptor_revision)
            outcome[key] = ("unreachable" if exc.status is None else "failed",
                            exc.code or "peer_unavailable")
            continue
        items: list[dict] = []
        set_ref = (remote.get("retrieval") or {}).get("evidence_set_ref")
        if set_ref:
            try:
                # 证据集读取本身先预扣 1 request（独立提交），再读；字节按
                # 实际信封规范 JSON 计，同样先预扣后用，不做内存 double-reserve。
                if spend is not None:
                    await spend(kind="request", amount=1)
                fetched = await peers.client(origin).evidence_set(str(set_ref))
                items = list(fetched.get("items") or [])
                if spend is not None:
                    await spend(kind="bytes",
                                amount=len(plans.canonical_bytes({"items": items})))
            except ApplicationError as exc:
                outcome[key] = ("not_attempted", exc.code)
                continue
            except PeerUnavailable as exc:
                outcome[key] = ("unreachable" if exc.status is None else "failed",
                                exc.code or "peer_unavailable")
                continue
        probes.append(remote)
        probe_ids[key] = await _persist_remote_probe(
            session, actor, remote, items,
            key=_probe_key(row.root_task_id, target, revision),
            task_spec_digest=row.task_spec_digest, consent_ref=consent["consent_id"],
            query_digest=query_digest,
            request_digest=plans.content_digest(plans.canonical_bytes(request)),
            policy_revision=_probe_policy_revision(manifest, origin), now=now)
    return probes, outcome, {"evidence": evidence, "probe_ids": probe_ids, "reused": reused,
                             "reuse_skipped": reuse_skipped}
