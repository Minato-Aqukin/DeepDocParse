"""Child report validation and durable root accounting through public adapters."""
from copy import deepcopy
from datetime import timedelta

import pytest

from ddp_core.application import coverage
from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import digest
from ddp_corpus import federation_budget, federation_tasks
from ddp_corpus.federation_models import FederationRootLedger
from ddp_corpus.models import new_id, utcnow
from test_federation_tasks import peer_evidence


QUERY_DIGEST = "sha256:" + "c" * 64
SCOPE = "scope-recursive"


def report_case():
    key = coverage.target_key("node-r", "papers", "corpus.retrieve")
    share = {"max_requests": 4, "max_bytes": 1024, "max_hops": 3,
             "max_probes": 2, "deadline": "2030-01-01T00:00:00Z"}
    step = {"step_id": "delegate-1", "operation": "delegate", "executor_node_id": "node-p",
            "depends_on": [], "fixed_inputs": ["query"], "budget_share": share,
            "delegated_targets": [{"target_key": key, "via_node_ids": []}]}
    evidence = peer_evidence()
    evidence.update(origin_node_id="node-r", authority_node_id="node-r", excerpt="Leaf fact",
                    excerpt_digest=digest(b"Leaf fact"))
    entry = coverage.new_entry(key, SCOPE, QUERY_DIGEST)
    entry.update(state="succeeded", attempts=1, probe_receipts=["receipt-r"],
                 actual_index_revision="index-r", evidence_refs=[evidence["evidence_id"]])
    report = {"entries": [entry], "evidence": [evidence],
              "consumption": {"requests": 4, "bytes": 1024, "hops": 3, "probes": 2}}
    return step, report


def validate(report, step):
    return federation_tasks.validate_delegation_report(
        report, step=step, scope_ref=SCOPE, query_digest=QUERY_DIGEST)


def test_valid_report_at_share_boundary_is_accepted():
    step, report = report_case()
    report["entries"][0]["scope_ref"] = "scope-child"
    report["entries"][0]["query_or_subquery_digest"] = "sha256:" + "d" * 64
    accepted = validate(report, step)
    assert accepted["entries"][0]["scope_ref"] == SCOPE
    assert accepted["entries"][0]["query_or_subquery_digest"] == QUERY_DIGEST
    assert accepted["entries"][0]["reported_by"] == "node-p"


@pytest.mark.parametrize("counter", ["requests", "bytes", "hops", "probes"])
def test_report_exceeding_any_reserved_counter_is_rejected(counter):
    step, report = report_case()
    report["consumption"][counter] += 1
    with pytest.raises(ApplicationError) as caught:
        validate(report, step)
    assert caught.value.code == "budget_exceeded"


@pytest.mark.parametrize("mutation", [
    "foreign-target", "foreign-collection", "foreign-evidence", "duplicate-target",
    "forged-excerpt", "negative-consumption", "boolean-consumption",
])
def test_malformed_or_out_of_assignment_report_is_rejected(mutation):
    step, report = report_case()
    if mutation == "foreign-target":
        report["entries"][0]["target_key"]["origin_node_id"] = "node-unassigned"
    elif mutation == "foreign-collection":
        report["entries"][0]["target_key"]["collection_id"] = "unassigned-papers"
    elif mutation == "foreign-evidence":
        report["evidence"][0].update(origin_node_id="node-unassigned", authority_node_id="node-unassigned")
    elif mutation == "duplicate-target":
        report["entries"].append(deepcopy(report["entries"][0]))
    elif mutation == "forged-excerpt":
        report["evidence"][0]["excerpt"] = "Changed fact"
    elif mutation == "negative-consumption":
        report["consumption"]["requests"] = -1
    else:
        report["consumption"]["requests"] = True
    with pytest.raises(ApplicationError):
        validate(report, step)


def test_dangling_evidence_refs_rejected_fail_closed():
    """子报告引用了自己没带的证据：整个报告 fail-closed 拒绝。

    变异确认：把 `validate_delegation_report` 的 references ⊆ items 检查去掉，
    本用例变红（报告会被接受，父节点记 succeeded 却 fused 为空）。
    """
    from ddp_core.application.ports import ApplicationError as _ApplicationError
    step, report = report_case()
    report["evidence"] = []
    with pytest.raises(_ApplicationError) as caught:
        validate(report, step)
    assert caught.value.code == "protocol_incompatible"


def test_partial_subquery_evidence_leaves_target_incomplete():
    """单个子查询的证据不得让整个目标提前 complete。"""
    from ddp_core.application import plans as plans_kernel
    key = coverage.target_key("node-r", "papers", "corpus.retrieve")
    digests = [plans_kernel.content_digest(f"subquery {index}".encode())
               for index in (1, 2)]
    first = coverage.new_entry(key, SCOPE, digests[0])
    first.update(state="succeeded", attempts=1, probe_receipts=["receipt-r"],
                 actual_index_revision="index-r", evidence_refs=["ev-1"])
    second = coverage.new_entry(key, SCOPE, digests[1])
    assert coverage.completeness("sealed", "exhaustive_scope", [first, second]) == "partial"
    assert coverage.completeness(
        "sealed", "exhaustive_scope",
        [{**second, "state": "succeeded", "attempts": 1,
          "probe_receipts": ["receipt-r"], "actual_index_revision": "index-r",
          "evidence_refs": ["ev-2"]},
         first]) == "complete"


@pytest.fixture
async def share_root(session):
    root, org, now = new_id(), new_id(), utcnow()
    caps = {"max_requests": 10, "max_bytes": 4096, "max_hops": 8,
            "max_generation_tokens": 0, "max_probe_requests": 4,
            "max_discovery_requests": 0, "max_egress_bytes": 4096,
            "deadline": (now + timedelta(minutes=5)).isoformat()}
    row = await federation_budget.ensure_ledger(
        session, root_task_id=root, organization_id=org, caller_budget=None,
        server_caps=caps, now=now)
    await session.commit()
    budget = federation_budget.rebuild_from_ledger(row, None, now=now.timestamp())
    share = {"max_requests": 4, "max_bytes": 1024, "max_hops": 3,
             "max_probes": 2, "deadline": caps["deadline"]}
    return root, org, budget, share


async def test_relay_not_attempted_rows_match_coordinator_shape(session, monkeypatch):
    """F8 中继腿与协调者同形：未命中的子查询行 refs 为空、尝试与回执保留。

    变异确认：把 relay.py 的 `leaf["evidence_refs"] = []` 改回"所有行都挂全量
    items"，或把 `attempts = 1` 删掉（回退到 record(not_attempted) 的 0），
    这里的断言变红。
    """
    from ddp_corpus import federation as _federation
    from ddp_corpus.deps import Actor as _Actor
    from ddp_corpus.federation_tasks import relay as _relay
    from ddp_core.application import plans as _plans
    from ddp_core.bundle import digest as _digest
    from test_federation_tasks import task_spec as _task_spec

    node = "node-relay-shape"
    monkeypatch.setattr(_federation, "local_node_id", lambda: node)
    monkeypatch.setattr(_relay.federation, "local_node_id", lambda: node)
    target = coverage.target_key(node, "papers", "corpus.retrieve")
    task_spec = _task_spec(scope="site_public", mode="fast", query="retrieval target")
    task_spec["requirements"] = {"query_plan": {"subqueries": [
        "retrieval target", "distant nebula cartography"]}}
    spec = {
        "request": {
            "task_spec": task_spec,
            "step": {"step_id": "delegate-1"},
            "plan": {"final_result_writer": node, "data_edges": []},
            "execution_consent": {"consent_id": "execute-1", "retention": "temporary"},
            "delegation_path": ["node-root"],
        },
        "step": {
            "step_id": "delegate-1", "operation": "delegate",
            "executor_node_id": node,
            "budget_share": {"max_requests": 32, "max_bytes": 1 << 20,
                             "max_hops": 4, "max_probes": 4,
                             "deadline": "2030-01-01T00:00:00Z"},
            "delegated_targets": [{"target_key": target, "via_node_ids": []}],
        },
    }
    item = peer_evidence(evidence_id="relay-evidence-1")
    item.update(origin_node_id=node, authority_node_id=node,
                excerpt="retrieval target text alpha",
                excerpt_digest=_digest(b"retrieval target text alpha"))

    async def _local_step(*args, **kwargs):
        return ("succeeded", None, [item], "index-relay", [])

    async def _no_receipt(*args, **kwargs):
        return {"admission_id": "admission-relay"}

    monkeypatch.setattr(_relay, "_run_local_step", _local_step)
    monkeypatch.setattr(_relay, "_lookup_local_receipt", _no_receipt)
    actor = _Actor(id="actor-alice", kind="user", organization_id="org-test",
                   role="contributor")
    report = await _relay.execute_delegation(
        session, actor, spec, execution_id="execution-relay-shape",
        now=utcnow(), http=None, index=None)
    assert len(report["entries"]) == 2, report["entries"]
    by_digest = {entry["query_or_subquery_digest"]: entry
                 for entry in report["entries"]}
    matched = _plans.content_digest(b"retrieval target")
    pending_digest = _plans.content_digest(b"distant nebula cartography")
    assert set(by_digest) == {matched, pending_digest}
    assert by_digest[matched]["state"] == "succeeded"
    assert by_digest[matched]["evidence_refs"] == ["relay-evidence-1"]
    pending = by_digest[pending_digest]
    assert pending["state"] == "not_attempted"
    assert pending["last_error"] == "subquery_evidence_missing"
    assert pending["evidence_refs"] == [], \
        "an unmatched relay row must not claim the matched row's evidence"
    assert pending["attempts"] == by_digest[matched]["attempts"] == 1
    assert pending["probe_receipts"] == by_digest[matched]["probe_receipts"]
    assert pending["actual_index_revision"] == by_digest[matched]["actual_index_revision"]


async def test_resume_and_retry_reserve_share_once_but_charge_every_physical_attempt(session, share_root):
    root, org, budget, share = share_root
    await federation_budget.reserve_share(root_task_id=root, organization_id=org,
        step_id="delegate-1", share=share, budget=budget)
    await federation_budget.spend(root_task_id=root, organization_id=org,
        kind="request", budget=budget)
    row = await session.get(FederationRootLedger, root, populate_existing=True)
    resumed = federation_budget.rebuild_from_ledger(row, None, now=utcnow().timestamp())
    await federation_budget.reserve_share(root_task_id=root, organization_id=org,
        step_id="delegate-1", share=share, budget=resumed)
    await federation_budget.spend(root_task_id=root, organization_id=org,
        kind="request", budget=resumed)
    used = await federation_budget.ledger_used(session, root_task_id=root, organization_id=org)
    assert {key: used[key] for key in ("requests", "bytes", "hops", "probes")} == {
        "requests": 6, "bytes": 1024, "hops": 3, "probes": 2}
    assert resumed.used()["requests"] == 6
    # Actual usage is informational: prepaid child allowance is never refunded.
    reported = {"requests": 1, "bytes": 50, "hops": 1, "probes": 0}
    await federation_budget.record_share_report(root_task_id=root, organization_id=org,
        step_id="delegate-1", reported=reported)
    await federation_budget.record_share_report(root_task_id=root, organization_id=org,
        step_id="delegate-1", reported=reported)
    assert await federation_budget.ledger_used(session, root_task_id=root, organization_id=org) == used


async def test_overlapping_child_shares_cannot_amplify_parent_budget(session, share_root):
    root, org, budget, share = share_root
    await federation_budget.reserve_share(root_task_id=root, organization_id=org,
        step_id="delegate-1", share=share, budget=budget)
    await federation_budget.reserve_share(root_task_id=root, organization_id=org,
        step_id="delegate-2", share=share, budget=budget)
    before = await federation_budget.ledger_used(session, root_task_id=root, organization_id=org)
    with pytest.raises(ApplicationError) as caught:
        await federation_budget.reserve_share(root_task_id=root, organization_id=org,
            step_id="delegate-3", share=share, budget=budget)
    assert caught.value.code == "budget_exhausted"
    assert await federation_budget.ledger_used(session, root_task_id=root, organization_id=org) == before


async def test_retry_cannot_replace_reserved_share_with_larger_allowance(session, share_root):
    root, org, budget, share = share_root
    await federation_budget.reserve_share(root_task_id=root, organization_id=org,
        step_id="delegate-1", share=share, budget=budget)
    before = await federation_budget.ledger_used(session, root_task_id=root, organization_id=org)
    with pytest.raises(ApplicationError) as caught:
        await federation_budget.reserve_share(root_task_id=root, organization_id=org,
            step_id="delegate-1", share={**share, "max_requests": 5}, budget=budget)
    assert caught.value.code == "plan_changed"
    assert await federation_budget.ledger_used(session, root_task_id=root, organization_id=org) == before
