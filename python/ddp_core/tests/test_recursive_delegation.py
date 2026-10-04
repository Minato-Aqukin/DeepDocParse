from ddp_core.application import coverage, routing


def test_recursive_route_builds_one_delegation_and_preserves_leaf_edges():
    target = {"origin_node_id": "node-r", "collection_id": "papers", "operation": "corpus.retrieve"}
    steps, edges = routing.plan_steps(
        targets=[target, target], probes=[], local_node_id="node-a",
        coordinator_node_id="node-a", query="facts", now=0,
        node_routes=[{"node_id": "node-r", "via_node_ids": ["node-p"]}],
        budget={"max_requests": 100, "max_bytes": 100000, "max_hops": 8,
                "max_probe_requests": 10, "deadline": "2030-01-01T00:00:00Z"})
    delegates = [step for step in steps if step["operation"] == "delegate"]
    assert len(delegates) == 1
    assert delegates[0]["executor_node_id"] == "node-p"
    assert delegates[0]["delegated_targets"] == [{"target_key": target, "via_node_ids": []}]
    assert delegates[0]["budget_share"]["max_requests"] < 100
    leaf_edge = next(edge for edge in edges if edge["payload_kind"] == "evidence_excerpts")
    assert (leaf_edge["from_node_id"], leaf_edge["to_node_id"], leaf_edge["relay_via"]) == (
        "node-r", "node-a", ["node-p"])


def test_delegated_success_cannot_claim_complete():
    entry = coverage.new_entry(
        coverage.target_key("node-r", "papers", "corpus.retrieve"), "scope", "sha256:" + "a" * 64)
    entry.update(state="succeeded", attempts=1, probe_receipts=["receipt"],
                 actual_index_revision="index-1", reported_by="node-p")
    coverage.validate_entry(entry)
    assert coverage.completeness("sealed", "exhaustive_scope", [entry]) == "partial"
