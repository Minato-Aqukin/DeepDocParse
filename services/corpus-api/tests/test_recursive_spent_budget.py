"""Shares reserve against the REMAINING budget after planning spend (red-first)."""
import pytest

from ddp_core.application import plans
from ddp_corpus.main import app  # noqa: F401 — keeps the root app importable in this module
from federation_recursive_node import NODE_A, NODE_P, NODE_R, NODE_S, RecursiveFixture
from test_federation_two_node import (
    EXPIRY, approve_task, coverage_of, entry_for, member, plan_task,
    scope_manifest, submit_task, task_spec, exploration,
)
from test_federation_recursive import assert_adjacent_traffic, recursive_manifest


async def spent_budget_plan(client, fixture, session):
    """Two targets: one local leaf plus one delegated S leaf, tight probe cap.

    Planning probes/discovery/bytes burn part of the ledger before shares are
    carved; every share must still reserve and both leaves must complete.
    The local leaf keeps the topology within the hop budget while still
    spending planning budget before carving.
    """
    from test_federation_probes import indexed_source, publish_collection

    _, version, _, _, _ = await indexed_source(session, texts=("federation keyword fact",))
    local = await publish_collection(client, version, key="spent-budget-local")
    local_target = member(local["collection_id"], NODE_A)
    s_target = member(fixture.nodes[NODE_S].seed.collection_id, NODE_S)
    manifest = scope_manifest([local_target, s_target])
    manifest["scope_id"] = "scope-recursive"
    manifest["registry_revision_vector"] = [
        {"node_id": node, "registry_revision": 1, "fetched_at": "2026-01-01T00:00:00Z"}
        for node in (NODE_A, NODE_P, NODE_R, NODE_S)]
    manifest["node_routes"] = [{"node_id": NODE_S, "via_node_ids": [NODE_P, NODE_R]}]
    manifest["manifest_digest"] = plans.digest({
        key: value for key, value in manifest.items() if key != "manifest_digest"})
    spec = task_spec(coordinator=NODE_A)
    spec["operation"] = "corpus.retrieve"
    spec["resource_scope"]["scope_ref"] = "scope-recursive"
    consent = exploration(recipients=(NODE_P, NODE_R, NODE_S),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    body = {"task_spec": spec, "exploration_consent": consent,
            "scope_manifest": manifest,
            "budget": {"max_requests": 160, "max_bytes": 16 << 20,
                       "max_hops": 16, "deadline": EXPIRY}}
    response = await client.post("/api/v1/task-intents", json=body,
                                 headers={"Idempotency-Key": plans.digest(body)})
    assert response.status_code == 201, response.text
    root = response.json()["root_task_id"]
    return root, await plan_task(client, root)


async def test_partially_spent_budget_still_reserves_every_share(actor_client, tmp_path,
                                                                 monkeypatch, app_state, session):
    from federation_recursive_node import RecursiveFixture as _Fixture

    fixture = await _Fixture.create(tmp_path)
    try:
        fixture.install_root(monkeypatch, app)
        root, plan = await spent_budget_plan(actor_client, fixture, session)
        delegates = [step for step in plan["steps"] if step["operation"] == "delegate"]
        assert len(delegates) == 1 and delegates[0]["executor_node_id"] == NODE_P, plan
        direct = [step for step in plan["steps"] if step["operation"] == "retrieve"
                  and step["executor_node_id"] == NODE_A]
        assert len(direct) == 1, plan
        share = delegates[0]["budget_share"]
        # The share fits the probes/bytes planning already spent: it reserves.
        assert share["max_probes"] <= 4
        await approve_task(actor_client, root, plan)
        response = await submit_task(actor_client, root, plan["plan_digest"],
                                     "recursive-spent-budget")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["status"] == "succeeded", status
        evidence = status["result"]["evidence"]
        origins = {item["origin_node_id"] for item in evidence}
        assert {NODE_A, NODE_S} <= origins, status
        coverage = await coverage_of(actor_client, root)
        for node in (NODE_A, NODE_S):
            entry = entry_for(coverage, node)
            assert entry["state"] == "succeeded", entry
        assert entry_for(coverage, NODE_S)["reported_by"] == NODE_P
        assert_adjacent_traffic(fixture)
    finally:
        await fixture.stop()
