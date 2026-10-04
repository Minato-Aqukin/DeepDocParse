"""Recursive planning and admission limits at the public pure seams."""
import pytest

from ddp_core.application import admission, coverage, routing
from ddp_core.application.ports import ApplicationError


DEADLINE = "2030-01-01T00:00:00Z"


def target(node, collection="papers"):
    return coverage.target_key(node, collection, "corpus.retrieve")


def plan(members, routes, **limits):
    return routing.plan_steps(
        targets=members, probes=[], local_node_id="node-a", coordinator_node_id="node-a",
        query="facts", now=0, node_routes=routes,
        budget={"max_requests": 100, "max_bytes": 200000, "max_hops": 12,
                "max_probe_requests": 9, "deadline": DEADLINE, **limits})


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_leaf_routes_choose_shortest_then_lexical_once(reverse):
    leaf = target("node-s")
    routes = [
        {"node_id": "node-s", "via_node_ids": ["node-p", "node-r"]},
        {"node_id": "node-s", "via_node_ids": ["node-q"]},
        {"node_id": "node-s", "via_node_ids": ["node-b"]},
    ]
    steps, edges = plan([leaf, leaf, leaf], list(reversed(routes)) if reverse else routes)
    work = [step for step in steps if step["operation"] in ("retrieve", "delegate")]
    assert len(work) == 1
    assert work[0]["operation"] == "delegate"
    assert work[0]["executor_node_id"] == "node-b"
    assert work[0]["delegated_targets"] == [{"target_key": leaf, "via_node_ids": []}]
    assert len(edges) == 2
    assert all(edge["relay_via"] == ["node-b"] for edge in edges)


@pytest.mark.parametrize("path,issuer", [
    (["node-a", "node-p"], "node-p"),
    (["node-a", "node-r", "node-a"], "node-a"),
], ids=["receiver-in-path", "repeated-ancestor"])
def test_admission_rejects_receiver_and_cycles_as_delegation_loop(path, issuer):
    with pytest.raises(ApplicationError) as caught:
        admission.validate_delegation_path(
            path, receiver_node_id="node-p", issuer_node_id=issuer, max_hops=8)
    assert caught.value.code == "delegation_loop"


def test_admission_depth_boundary_accepts_remaining_hop_only():
    path = ["node-a", "node-p"]
    admission.validate_delegation_path(
        path, receiver_node_id="node-r", issuer_node_id="node-p", max_hops=3)
    with pytest.raises(ApplicationError) as caught:
        admission.validate_delegation_path(
            path, receiver_node_id="node-r", issuer_node_id="node-p", max_hops=2)
    assert caught.value.code == "budget_exceeded"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_child_shares_leave_parent_request_byte_and_hop_allowance(count):
    members = [target(f"node-leaf-{index}") for index in range(count)]
    routes = [{"node_id": member["origin_node_id"], "via_node_ids": [f"node-relay-{index}"]}
              for index, member in enumerate(members)]
    steps, _ = plan(members, routes)
    shares = [step["budget_share"] for step in steps if step["operation"] == "delegate"]
    assert len(shares) == count
    # Parent needs at least one admission attempt, payload byte and hop per child.
    assert sum(share["max_requests"] for share in shares) + count <= 100
    assert sum(share["max_bytes"] for share in shares) + count <= 200000
    assert sum(share["max_hops"] for share in shares) + count <= 12
    assert sum(share["max_probes"] for share in shares) <= 9
    assert all(share["deadline"] == DEADLINE for share in shares)


def test_plan_rejects_child_when_no_depth_share_remains():
    with pytest.raises(ApplicationError) as caught:
        plan([target("node-r")], [{"node_id": "node-r", "via_node_ids": ["node-p"]}],
             max_hops=2)
    assert caught.value.code == "budget_exceeded"


def test_shares_carve_from_remaining_budget_proportionally_to_leaves():
    """Used budget is not carved twice; each group's share follows its leaves."""
    members = [target("node-direct", "papers"), target("node-far-1", "papers"),
               target("node-far-2", "papers"), target("node-far-3", "papers")]
    routes = [{"node_id": "node-far-1", "via_node_ids": ["node-p"]},
              {"node_id": "node-far-2", "via_node_ids": ["node-p"]},
              {"node_id": "node-far-3", "via_node_ids": ["node-q"]}]
    steps, _ = routing.plan_steps(
        targets=members, probes=[], local_node_id="node-a", coordinator_node_id="node-a",
        query="facts", now=0, node_routes=routes,
        budget={"max_requests": 100, "max_bytes": 200000, "max_hops": 12,
                "max_probe_requests": 9, "deadline": DEADLINE,
                "used_requests": 10, "used_bytes": 20000, "used_probes": 2, "used_hops": 1})
    shares = {step["executor_node_id"]: step["budget_share"] for step in steps
              if step["operation"] == "delegate"}
    assert set(shares) == {"node-p", "node-q"}
    # Two leaves vs one leaf: the bigger group gets the bigger share.
    assert shares["node-p"]["max_requests"] > shares["node-q"]["max_requests"]
    assert shares["node-p"]["max_bytes"] > shares["node-q"]["max_bytes"]
    assert shares["node-p"]["max_probes"] >= shares["node-q"]["max_probes"]
    # Nothing carved twice: shares + own needs fit inside the remaining caps.
    remaining_requests = 100 - 10
    remaining_bytes = 200000 - 20000
    remaining_probes = 9 - 2
    assert sum(share["max_requests"] for share in shares.values()) <= remaining_requests - 8 * 2 - 68
    assert sum(share["max_bytes"] for share in shares.values()) <= remaining_bytes - 32768 * 2 - 4096
    assert sum(share["max_probes"] for share in shares.values()) <= remaining_probes
    assert sum(share["max_hops"] for share in shares.values()) <= 12 - 1 - 2 * 2 - 2


@pytest.mark.parametrize("state", ["succeeded", "unsupported"])
def test_reported_leaf_never_upgrades_exhaustive_coverage_to_complete(state):
    entry = coverage.new_entry(target("node-r"), "scope-1", "sha256:" + "b" * 64)
    entry.update(state=state, attempts=1, probe_receipts=["receipt-r"],
                 actual_index_revision="index-r")
    if state == "unsupported":
        entry["exclusion_basis"] = "collection cannot retrieve evidence"
    coverage.validate_entry(entry)
    assert coverage.completeness("sealed", "exhaustive_scope", [entry]) == "complete"
    entry["reported_by"] = "node-p"
    coverage.validate_entry(entry)
    assert coverage.completeness("sealed", "exhaustive_scope", [entry]) == "partial"
