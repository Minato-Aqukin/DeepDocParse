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
    # 13 hops: 1 used, direct target 2, two admissions 4, P (two leaves) needs 4,
    # Q needs 2 — exactly enough, so the hop rule does not refuse the plan.
    steps, _ = routing.plan_steps(
        targets=members, probes=[], local_node_id="node-a", coordinator_node_id="node-a",
        query="facts", now=0, node_routes=routes,
        budget={"max_requests": 100, "max_bytes": 200000, "max_hops": 13,
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
    assert sum(share["max_hops"] for share in shares.values()) <= 13 - 1 - 2 * 2 - 2


@pytest.mark.parametrize("members,routes,need", [
    # One relay, leaf served directly by it: admission 2 + share 2 (depth gate 1 < 2).
    ([("node-s", ())], [("node-s", ("node-p",))], 4),
    # Direct remote sibling 2 + admission 2 + share 5 (P admits R: 2 + R's share 3,
    # R's gate is len([A, P]) = 2 < 3).
    ([("node-b", ()), ("node-s", ())], [("node-s", ("node-p", "node-r"))], 9),
    # Three relays: Q needs 4 (gate 3 < 4), R 2 + 4 = 6, P 2 + 6 = 8, plus admission 2.
    ([("node-s", ())], [("node-s", ("node-p", "node-r", "node-q"))], 10),
    # Leaves sharing a relay: the plan validator reserves both edges of every leaf
    # with its relay hop, 2 * (1 + 1) each, which outgrows admission 2 + the relay's
    # own retrievals: 2 leaves -> 8, 3 leaves -> 12.
    ([("node-s", ()), ("node-t", ())],
     [("node-s", ("node-p",)), ("node-t", ("node-p",))], 8),
    ([("node-s", ()), ("node-t", ()), ("node-u", ())],
     [("node-s", ("node-p",)), ("node-t", ("node-p",)), ("node-u", ("node-p",))], 12),
    # Two leaves behind [P, R]: 2 * 2 * (1 + 2) = 12 transmitted hops; P's share must
    # also hold its own sub-plan's 2 * 2 * (1 + 1) = 8.
    ([("node-s", ()), ("node-t", ())],
     [("node-s", ("node-p", "node-r")), ("node-t", ("node-p", "node-r"))], 12),
], ids=["one-relay", "sibling-plus-two-relays", "three-relays", "two-leaves-one-relay",
        "three-leaves-one-relay", "two-leaves-two-relays"])
def test_hop_need_covers_every_admission_relay_depth_gate_and_transmission(members, routes, need):
    assert routing.hop_need(
        targets=[target(node) for node, _ in members], coordinator_node_id="node-a",
        node_routes=[{"node_id": node, "via_node_ids": list(via)} for node, via in routes]) == need


def test_plan_refuses_a_hop_share_shorter_than_its_route_depth():
    """8 hops for B direct plus S via [P, R] would carve P a 4-hop share; R would
    then refuse the admission at execution (len([A, P]) = 2 is not < 2). The
    plan is refused up front instead, where the user still sees the budget."""
    members = [target("node-b"), target("node-s")]
    routes = [{"node_id": "node-s", "via_node_ids": ["node-p", "node-r"]}]
    with pytest.raises(ApplicationError) as caught:
        plan(members, routes, max_hops=8)
    assert caught.value.code == "budget_exceeded"
    steps, _ = plan(members, routes, max_hops=9)
    share = next(step["budget_share"] for step in steps if step["operation"] == "delegate")
    assert share["max_hops"] == 5


def test_hop_shares_follow_route_depth_before_splitting_the_rest():
    """A deep route gets the depth it needs even beside a shallow one; only the
    remainder is split evenly. 11 hops: two admissions 4, then P needs 5 for
    [P, R] and Q needs 2 for [Q] — an even split (4/3) would starve P."""
    members = [target("node-s"), target("node-t")]
    routes = [{"node_id": "node-s", "via_node_ids": ["node-p", "node-r"]},
              {"node_id": "node-t", "via_node_ids": ["node-q"]}]
    steps, _ = plan(members, routes, max_hops=11)
    shares = {step["executor_node_id"]: step["budget_share"]["max_hops"]
              for step in steps if step["operation"] == "delegate"}
    assert shares == {"node-p": 5, "node-q": 2}
    steps, _ = plan(members, routes, max_hops=14)
    shares = {step["executor_node_id"]: step["budget_share"]["max_hops"]
              for step in steps if step["operation"] == "delegate"}
    assert shares == {"node-p": 7, "node-q": 3}


def test_relay_carves_sub_shares_for_its_own_delegation_depth():
    """P, admitted with path [A], plans S via [R]: R is admitted with [A, P]
    and needs a share of 3, so P's 4-hop share (2 for R's admission) is short."""
    members = [target("node-s")]
    routes = [{"node_id": "node-s", "via_node_ids": ["node-r"]}]
    def relay_plan(max_hops):
        return routing.plan_steps(
            targets=members, probes=[], local_node_id="node-p", coordinator_node_id="node-p",
            query="facts", now=0, node_routes=routes, delegation_depth=1,
            budget={"max_requests": 100, "max_bytes": 200000, "max_hops": max_hops,
                    "max_probe_requests": 9, "deadline": DEADLINE})
    with pytest.raises(ApplicationError) as caught:
        relay_plan(4)
    assert caught.value.code == "budget_exceeded"
    steps, _ = relay_plan(5)
    share = next(step["budget_share"] for step in steps if step["operation"] == "delegate")
    assert share["max_hops"] == 3
    admission.validate_delegation_path(["node-a", "node-p"], receiver_node_id="node-r",
                                       issuer_node_id="node-p", max_hops=share["max_hops"])


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
