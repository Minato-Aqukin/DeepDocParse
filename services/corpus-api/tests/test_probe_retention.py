"""T64 探针证据留存：pin = "还能 resume 或还能 continuation"，二者任一即保留。

- (a) plain resume：非 cancelled + min(plan.valid_until, plan.budget.deadline,
  execution_consent.valid_until) 还没过截止线；
- (b) continuation：非 cancelled + fast + 非固定资源 + min(scope_manifest
  .valid_until, exploration_consent.valid_until) 还没过截止线；
  plan 过期但 manifest + exploration 有效的 fast 任务仍可续批，必须 pin。
- 截止线两侧全过、或 cancelled：剥离 `evidence`，行/状态/摘要保留；
- 没过留存窗口的行不动；limit 非正拒绝。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ACTOR, ORG
from ddp_corpus import probe_retention
from ddp_corpus.deps import Actor
from ddp_corpus.federation_models import CoverageEntry, CoverageLedger, FederationProbe, FederationRequest
from ddp_corpus.models import new_id, utcnow

CALLER = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")


def _probe(probe_id: str, *, expired: bool = True, with_evidence: bool = True) -> FederationProbe:
    now = utcnow()
    evidence = [{"evidence_id": "e-1", "origin_node_id": "node-x",
                 "excerpt": "可重建的证据摘录", "_excerpt": "内部全文拷贝"}] if with_evidence else []
    return FederationProbe(
        probe_id=probe_id, organization_id=ORG, actor_id=ACTOR,
        target_node_id="node-x", task_spec_digest="sha256:" + "1" * 64,
        consent_ref="consent-1", probe_kind="evidence_retrieval",
        collection_id="col-1", query_digest="sha256:" + "2" * 64,
        state="succeeded",
        result_json={"kind": "evidence_retrieval",
                     "result": {"probe_id": probe_id, "probe_kind": "evidence_retrieval"},
                     "evidence": evidence, "request_digest": "digest-1"},
        expires_at=(now - timedelta(seconds=86400 + 10) if expired
                    else now + timedelta(seconds=3600)),
        created_at=now - timedelta(minutes=5))


def _request(root_task_id: str, probe_id: str | None, *, status: str,
             plan_valid_until: str = "2030-01-01T00:00:00Z",
             manifest_valid_until: str = "2030-01-01T00:00:00Z",
             exploration_valid_until: str = "2030-01-01T00:00:00Z",
             execution_valid_until: str = "2030-01-01T00:00:00Z",
             mode: str = "fast", scope_kind: str = "federation_public") -> FederationRequest:
    plan = None
    if probe_id is not None:
        plan = {"steps": [{"step_id": "retrieve-1", "operation": "retrieve",
                           "probe_refs": [probe_id]}],
                "valid_until": plan_valid_until,
                "budget": {"deadline": plan_valid_until}}
    now = utcnow()
    return FederationRequest(
        root_task_id=root_task_id, organization_id=ORG, actor_id=ACTOR,
        task_spec_digest="sha256:" + "3" * 64, scope_id="scope-1",
        planning_state="approved", plan_revision=1, plan_digest="sha256:" + "4" * 64,
        status=status,
        task_spec_json={"search_policy": {"mode": mode},
                        "resource_scope": {"kind": scope_kind}},
        exploration_consent_json={"valid_until": exploration_valid_until},
        scope_manifest_json={"valid_until": manifest_valid_until},
        execution_consent_json={"valid_until": execution_valid_until},
        plan_json=plan, created_at=now, updated_at=now)


async def test_expired_unpinned_probe_is_stripped_but_row_survives(session):
    session.add(_probe("probe-old"))
    await session.commit()
    stripped = await probe_retention.sweep_probe_evidence(session, now=utcnow())
    assert stripped == 1
    row = await session.get(FederationProbe, "probe-old")
    assert row is not None, "回执行必须保留：覆盖账本引的是行 id"
    assert (row.result_json or {}).get("evidence") == []
    assert row.state == "succeeded" and row.task_spec_digest == "sha256:" + "1" * 64
    assert (row.result_json or {})["request_digest"] == "digest-1"


async def test_running_task_pins_its_probe(session):
    session.add(_probe("probe-pinned"))
    session.add(_request("root-running", "probe-pinned", status="running"))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 0
    row = await session.get(FederationProbe, "probe-pinned")
    assert len((row.result_json or {})["evidence"]) == 1


async def test_resumable_succeeded_task_pins_its_probe(session):
    # Blocker 回归：succeeded 仍可 resume，旧实现按终态剥离会打断 continuation。
    session.add(_probe("probe-live-task"))
    session.add(_request("root-succeeded", "probe-live-task", status="succeeded"))
    session.add(CoverageLedger(
        root_task_id="root-succeeded", scope_ref="scope-1", search_mode="fast",
        enumeration_state="sealed", retrieval_completeness="partial",
        evidence_sufficiency="insufficient", counts_json={},
        manifest_digest="sha256:" + "5" * 64))
    await session.flush()
    session.add(CoverageEntry(
        root_task_id="root-succeeded", target_digest="d" * 64,
        target_key_json={"origin_node_id": "node-x", "collection_id": "col-1",
                         "operation": "corpus.retrieve"},
        query_digest="sha256:" + "2" * 64, state="succeeded",
        probe_refs_json=["probe-live-task"], evidence_refs_json=["e-1"],
        used_budget_json={}))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 0
    row = await session.get(FederationProbe, "probe-live-task")
    assert len((row.result_json or {})["evidence"]) == 1


async def test_plan_expired_but_manifest_and_exploration_valid_pins(session):
    # Main 第二轮复核的精确场景：plan（含 budget deadline 与 execution consent）
    # 全过期，但 scope manifest + exploration consent 仍有效 → fast continuation
    # 建新计划照样放行，settled carried refs 还要读正文，必须 pin。
    session.add(_probe("probe-continuable"))
    session.add(_request("root-continuable", "probe-continuable", status="succeeded",
                         plan_valid_until="2020-01-01T00:00:00Z",
                         manifest_valid_until="2030-01-01T00:00:00Z",
                         exploration_valid_until="2030-01-01T00:00:00Z",
                         execution_valid_until="2020-01-01T00:00:00Z"))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 0
    row = await session.get(FederationProbe, "probe-continuable")
    assert len((row.result_json or {})["evidence"]) == 1


async def test_non_fast_task_with_expired_plan_does_not_pin(session):
    # 穷查任务没有 continuation 门：plan 链过期即不可走，manifest 有效也不 pin。
    session.add(_probe("probe-exhaustive"))
    session.add(_request("root-exhaustive", "probe-exhaustive", status="succeeded",
                         plan_valid_until="2020-01-01T00:00:00Z",
                         manifest_valid_until="2030-01-01T00:00:00Z",
                         exploration_valid_until="2030-01-01T00:00:00Z",
                         execution_valid_until="2020-01-01T00:00:00Z",
                         mode="exhaustive"))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 1


async def test_fully_expired_succeeded_task_is_stripped(session):
    # 四份有效期全过：resume 本来就会被可见的过期理由拒绝，剥离安全。
    session.add(_probe("probe-done"))
    session.add(_request("root-done", "probe-done", status="succeeded",
                         plan_valid_until="2020-01-01T00:00:00Z",
                         manifest_valid_until="2020-01-01T00:00:00Z",
                         exploration_valid_until="2020-01-01T00:00:00Z",
                         execution_valid_until="2020-01-01T00:00:00Z"))
    session.add(CoverageLedger(
        root_task_id="root-done", scope_ref="scope-1", search_mode="fast",
        enumeration_state="sealed", retrieval_completeness="complete",
        evidence_sufficiency="sufficient_by_policy", counts_json={},
        manifest_digest="sha256:" + "5" * 64))
    await session.flush()
    session.add(CoverageEntry(
        root_task_id="root-done", target_digest="d" * 64,
        target_key_json={"origin_node_id": "node-x", "collection_id": "col-1",
                         "operation": "corpus.retrieve"},
        query_digest="sha256:" + "2" * 64, state="succeeded",
        probe_refs_json=["probe-done"], evidence_refs_json=["e-1"],
        used_budget_json={}))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 1
    assert await session.get(FederationProbe, "probe-done") is not None
    entry = await session.get(CoverageEntry, ("root-done", "d" * 64))
    assert entry.probe_refs_json == ["probe-done"], "覆盖引用的行 id 必须还在"


async def test_cancelled_task_never_pins(session):
    session.add(_probe("probe-cancelled"))
    session.add(_request("root-cancelled", "probe-cancelled", status="cancelled"))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 1


async def test_live_probe_is_never_touched(session):
    session.add(_probe("probe-live", expired=False))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 0
    row = await session.get(FederationProbe, "probe-live")
    assert len((row.result_json or {})["evidence"]) == 1


async def test_already_stripped_row_is_not_recounted(session):
    session.add(_probe("probe-bare", with_evidence=False))
    await session.commit()
    assert await probe_retention.sweep_probe_evidence(session, now=utcnow()) == 0


async def test_invalid_limit_is_rejected(session):
    with pytest.raises(ValueError):
        await probe_retention.sweep_probe_evidence(session, now=utcnow(), limit=0)
