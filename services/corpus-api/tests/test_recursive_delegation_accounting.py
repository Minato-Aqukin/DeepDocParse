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
