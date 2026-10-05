"""Recursive retrieval crosses real HTTP only along approved adjacent trust edges."""
from urllib.parse import urlsplit

import pytest

from ddp_core.application import plans
from ddp_core.bundle import digest as bundle_digest
from ddp_corpus.main import app
from federation_recursive_node import NODE_A, NODE_P, NODE_R, NODE_S, RecursiveFixture
from test_federation_two_node import (
    EXPIRY, approve_task, coverage_of, entry_for, execution_consent, execution_recipients,
    exploration, member, plan_task, scope_manifest, submit_task, task_spec,
)


@pytest.fixture
async def recursive_nodes(tmp_path, monkeypatch, app_state, request):
    fixture = await RecursiveFixture.create(tmp_path, policies=getattr(request, "param", None))
    try:
        fixture.install_root(monkeypatch, app)
        yield fixture
    finally:
        await fixture.stop()


def recursive_manifest(fixture, leaf, *, direct_relay=False):
    targets = [member(fixture.nodes[leaf].seed.collection_id, leaf)]
    if direct_relay:
        # P's own collection as a direct target next to the routed leaf.
        targets.insert(0, member(fixture.nodes[NODE_P].seed.collection_id, NODE_P))
    manifest = scope_manifest(targets)
    manifest["scope_id"] = "scope-recursive"
    manifest["registry_revision_vector"] = [
        {"node_id": node, "registry_revision": 1, "fetched_at": "2026-01-01T00:00:00Z"}
        for node in (NODE_A, NODE_P, NODE_R, NODE_S)]
    manifest["node_routes"] = [{"node_id": leaf, "via_node_ids": (
        [NODE_P] if leaf == NODE_R else [NODE_P, NODE_R])}]
    manifest["manifest_digest"] = plans.digest({
        key: value for key, value in manifest.items() if key != "manifest_digest"})
    return manifest


async def recursive_plan(client, fixture, *, leaf=NODE_S, recipients=None,
                         direct_relay=False, max_hops=8, max_requests=96):
    spec = task_spec(coordinator=NODE_A)
    spec["operation"] = "corpus.retrieve"
    spec["resource_scope"]["scope_ref"] = "scope-recursive"
    consent = exploration(recipients=(NODE_P, NODE_R, NODE_S) if recipients is None else recipients,
                          budget={"max_probe_requests": 16, "max_egress_bytes": 16 << 20})
    body = {"task_spec": spec, "exploration_consent": consent,
            "scope_manifest": recursive_manifest(fixture, leaf, direct_relay=direct_relay),
            "budget": {"max_requests": max_requests, "max_bytes": 16 << 20,
                       "max_hops": max_hops, "deadline": EXPIRY}}
    response = await client.post("/api/v1/task-intents", json=body,
                                 headers={"Idempotency-Key": plans.digest(body)})
    assert response.status_code == 201, response.text
    root = response.json()["root_task_id"]
    return root, await plan_task(client, root)


def assert_adjacent_traffic(fixture):
    """Check socket destinations, not just untrusted target-node headers."""
    endpoint_ports = {node: urlsplit(peer.endpoint).port for node, peer in fixture.nodes.items()}
    assert fixture.outbound, "root must actually contact P"
    assert {urlsplit(call["url"]).port for call in fixture.outbound} == {endpoint_ports[NODE_P]}
    for node, permitted in ((NODE_P, {NODE_R}), (NODE_R, {NODE_S}), (NODE_S, set())):
        contacted = {urlsplit(call["url"]).port for call in fixture.nodes[node].outbound()}
        assert contacted <= {endpoint_ports[peer] for peer in permitted}


@pytest.mark.parametrize("leaf", [NODE_R, NODE_S], ids=["A-P-R", "A-P-R-S"])
async def test_recursive_retrieval_preserves_leaf_origin_over_real_http(
        actor_client, recursive_nodes, leaf):
    root, plan = await recursive_plan(actor_client, recursive_nodes, leaf=leaf)
    delegates = [step for step in plan["steps"] if step["operation"] == "delegate"]
    assert len(delegates) == 1, plan
    step = delegates[0]
    assert step["executor_node_id"] == NODE_P
    assert step["delegated_targets"] == [{
        "target_key": member(recursive_nodes.nodes[leaf].seed.collection_id, leaf),
        "via_node_ids": [] if leaf == NODE_R else [NODE_R]}]
    assert step["budget_share"]["max_hops"] >= 3
    assert not any(call["path"].endswith("/probes") for call in recursive_nodes.outbound), \
        "routed retrieval must not directly probe leaves during planning"
    route = [NODE_P] if leaf == NODE_R else [NODE_P, NODE_R]
    query_edges = [edge for edge in plan["data_edges"]
                   if edge["from_node_id"] == NODE_A and edge["to_node_id"] == leaf]
    evidence_edges = [edge for edge in plan["data_edges"]
                      if edge["from_node_id"] == leaf and edge["to_node_id"] == NODE_A]
    assert query_edges and all(edge["relay_via"] == route for edge in query_edges)
    assert evidence_edges and all(edge["relay_via"] == list(reversed(route)) for edge in evidence_edges)

    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "recursive-retrieve")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["status"] == "succeeded", status
    assert status["retrieval_completeness"] == "partial", status
    evidence = status["result"]["evidence"]
    assert len(evidence) == 1, status
    item, seed = evidence[0], recursive_nodes.nodes[leaf].seed
    assert item["origin_node_id"] == leaf
    assert item["authority_node_id"] == leaf
    assert item["resource_id"] == seed.resource_id
    assert item["source_version_id"] == seed.version_id
    assert item["evidence_id"] == seed.evidence_id
    assert item["excerpt_digest"] == bundle_digest(seed.text.encode())
    assert item["relay_via"] == list(reversed(route))
    coverage = await coverage_of(actor_client, root)
    assert coverage["counts"]["total_targets"] == 1
    assert coverage["retrieval_completeness"] == "partial"
    entry = entry_for(coverage, leaf)
    assert entry["state"] == "succeeded", entry
    assert entry["reported_by"] == NODE_P
    assert entry["actual_index_revision"] == seed.index_revision
    assert len(recursive_nodes.nodes[leaf].calls("/admissions")) == 1
    assert_adjacent_traffic(recursive_nodes)
    root_admission = next(call["body"] for call in recursive_nodes.nodes[NODE_P].calls("/admissions"))
    assert root_admission["delegation_path"] == [NODE_A]
    if leaf == NODE_S:
        relay_admission = next(call["body"] for call in recursive_nodes.nodes[NODE_R].calls("/admissions"))
        assert relay_admission["delegation_path"] == [NODE_A, NODE_P]


async def test_direct_relay_target_plus_two_relay_leaf_reaches_the_leaf_over_real_http(
        actor_client, recursive_nodes):
    """A retrieves P's own collection directly and S via [P, R] in one plan.

    Hop need (hand-derived): direct P 2 + P's admission 2 + P's share 5, where
    P's share is R's admission 2 + R's share 3 and R's share must beat its
    strict depth gate len([A, P]) = 2. The root budget must be 9 and P's share
    at least 5; with 8 the real R refuses P's sub-delegation (budget_exceeded).
    The caller's request/byte caps are generous so the server caps decide.
    """
    root, plan = await recursive_plan(actor_client, recursive_nodes,
                                      direct_relay=True, max_hops=16, max_requests=4096)
    assert plan["budget"]["max_hops"] == 9, plan["budget"]
    delegate = next(step for step in plan["steps"] if step["operation"] == "delegate")
    assert delegate["executor_node_id"] == NODE_P
    assert delegate["budget_share"]["max_hops"] >= 5, delegate
    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "recursive-direct-plus-deep")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["status"] == "succeeded", status
    coverage = await coverage_of(actor_client, root)
    assert entry_for(coverage, NODE_P)["state"] == "succeeded", coverage
    leaf = entry_for(coverage, NODE_S)
    assert leaf["state"] == "succeeded", (leaf.get("last_error"), leaf)
    assert leaf["reported_by"] == NODE_P
    origins = {item["origin_node_id"] for item in status["result"]["evidence"]}
    assert origins == {NODE_P, NODE_S}, status
    relay_admission = next(call["body"] for call in recursive_nodes.nodes[NODE_R].calls("/admissions"))
    assert relay_admission["delegation_path"] == [NODE_A, NODE_P]
    assert_adjacent_traffic(recursive_nodes)


async def test_unavailable_parent_keeps_recursive_leaf_unreachable(
        actor_client, recursive_nodes):
    root, plan = await recursive_plan(actor_client, recursive_nodes)
    await approve_task(actor_client, root, plan)
    await recursive_nodes.nodes[NODE_P].stop()
    response = await submit_task(actor_client, root, plan["plan_digest"], "recursive-parent-down")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    coverage = await coverage_of(actor_client, root)
    assert coverage["counts"]["total_targets"] == 1
    assert coverage["counts"]["incomplete"] == 1
    entry = entry_for(coverage, NODE_S)
    assert entry["state"] == "unreachable", entry
    assert entry["last_error"] == "peer_unavailable", entry
    assert recursive_nodes.nodes[NODE_R].calls() == []
    assert recursive_nodes.nodes[NODE_S].calls() == []
    assert_adjacent_traffic(recursive_nodes)


@pytest.mark.parametrize("missing", [NODE_P, NODE_R, NODE_S], ids=["first-relay", "relay", "leaf"])
async def test_missing_route_exploration_consent_sends_zero_bytes(
        actor_client, recursive_nodes, missing):
    recipients = [node for node in (NODE_P, NODE_R, NODE_S) if node != missing]
    root, plan = await recursive_plan(actor_client, recursive_nodes, recipients=recipients)
    assert recursive_nodes.outbound == []
    assert all(peer.calls() == [] and peer.outbound() == [] for peer in recursive_nodes.nodes.values())
    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "recursive-no-explore")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    entry = entry_for(await coverage_of(actor_client, root), NODE_S)
    assert entry["state"] == "denied", entry
    assert entry["last_error"] == "egress_denied", entry
    assert recursive_nodes.outbound == []
    assert all(peer.calls() == [] and peer.outbound() == [] for peer in recursive_nodes.nodes.values())


@pytest.mark.parametrize("missing", [NODE_P, NODE_R, NODE_S], ids=["first-relay", "relay", "leaf"])
async def test_missing_route_execution_consent_sends_zero_bytes(
        actor_client, recursive_nodes, missing):
    root, plan = await recursive_plan(actor_client, recursive_nodes)
    assert recursive_nodes.outbound == []
    consent = execution_consent(plan["plan_digest"],
        recipients=[node for node in execution_recipients(plan) if node != missing],
        edges=[edge["edge_id"] for edge in plan["data_edges"]])
    response = await actor_client.post(f"/api/v1/task-plans/{root}/approve", json={
        "plan_digest": plan["plan_digest"], "execution_consent": consent})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "egress_denied"
    assert recursive_nodes.outbound == []
    assert all(peer.calls() == [] and peer.outbound() == [] for peer in recursive_nodes.nodes.values())


@pytest.mark.parametrize("recursive_nodes", [{NODE_S: [NODE_R, NODE_P]}], indirect=True,
                         ids=["source-forbids-final-root"])
async def test_leaf_source_policy_checks_final_recipient_not_only_adjacent_relay(
        actor_client, recursive_nodes):
    """S permits both relays but not A; a recursive plan must carry A to S's gate."""
    root, plan = await recursive_plan(actor_client, recursive_nodes)
    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "recursive-source-denied")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    entry = entry_for(await coverage_of(actor_client, root), NODE_S)
    assert entry["state"] == "denied", entry
    assert entry["last_error"] == "egress_denied", entry
    calls = recursive_nodes.nodes[NODE_S].calls("/admissions")
    assert len(calls) == 1 and calls[0]["status"] == 403, calls
    assert not any("/evidence-sets/" in call["path"] for call in recursive_nodes.nodes[NODE_S].calls())

