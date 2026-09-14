import json

import pytest

from ddp_core.application import plans
from ddp_core.application.plans import task_plan_digest, task_spec_digest, utc_instant
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import build_probe
from ddp_core.application.routing import RootBudget, candidates, plan_steps, targets
from ddp_paths import CONTRACTS
from plan_samples import EXPIRY, NOW

DIGEST = "sha256:" + "a" * 64
OBSERVED = utc_instant(NOW)


def target(origin, collection, operation="corpus.retrieve"):
    return {"origin_node_id": origin, "collection_id": collection, "operation": operation}


def manifest(members, state="sealed", unexpanded=()):
    return {"schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": "scope-17",
            "caller_scope_hash": DIGEST, "created_at": OBSERVED, "valid_until": EXPIRY,
            "registry_revision_vector": [{"node_id": "node-b", "registry_revision": 3, "fetched_at": OBSERVED}],
            "expanded_members": list(members), "unexpanded_subtrees": list(unexpanded),
            "enumeration_state": state, "manifest_digest": DIGEST}


def descriptor(origin, collection, topics=(), languages=(), to=None):
    value = {"schema": "ddp-discovery/1#CollectionDescriptor", "origin_node_id": origin,
             "collection_id": collection, "index_revision": "index-42", "revision": 1,
             "valid_until": EXPIRY}
    if topics:
        value["topics"] = list(topics)
    if languages:
        value["languages"] = list(languages)
    if to:
        value["time_range"] = {"from": to, "to": to}
    return value


def probe(node, observed=None, can_generate=False):
    return build_probe(
        probe_id=f"probe-{node}", target_node_id=node, task_spec_digest=DIGEST, consent_ref="consent-1",
        probe_kind="evidence_retrieval",
        capability_check={"operation": "corpus.retrieve", "readiness": "ready"},
        retrieval={"status": "succeeded", "collection_ref": f"{node}:robotics", "index_revision": "index-42"},
        can_generate=can_generate, observed_at=observed or OBSERVED)


def budget(**overrides):
    value = {"max_requests": 6, "max_bytes": 200, "max_hops": 1, "max_generation_tokens": 1,
             "deadline": EXPIRY}
    value.update(overrides)
    return RootBudget(value, now=NOW)


def task_spec(coordinator="node-a"):
    return {"schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1", "operation": "rag.answer.cited",
            "workspace_ref": "workspace-a", "query": "which page has the evidence?",
            "resource_scope": {"kind": "federation_public", "scope_ref": "scope-17"},
            "search_policy": {"mode": "exhaustive_scope", "ordering": "local_first"},
            "execution_policy": {"mode": "trusted_federation", "coordinator_ref": coordinator},
            "consent_refs": {"exploration": "consent-probe-4", "execution": None},
            "budget_ref": "budget-9"}


def assemble(steps, edges, spec):
    plan = {"schema": "ddp-plan-admission/1#TaskPlan", "plan_id": "plan-1", "revision": 1,
            "plan_digest": "sha256:" + "0" * 64, "task_spec_digest": task_spec_digest(spec),
            "root_coordinator_node_id": "node-a", "planning_state": "ready", "steps": steps,
            "data_edges": edges,
            "budget": {"max_requests": 10, "max_bytes": 100000, "max_generation_tokens": 500,
                       "max_hops": max(1, len(edges)), "deadline": EXPIRY},
            "final_result_writer": "node-a", "valid_until": EXPIRY, "execution_consent_ref": None}
    plan["plan_digest"] = task_plan_digest(plan)
    return plan


def test_targets_dedup_and_stable_order():
    members = [target("node-c", "c:robotics"), target("node-b", "b:robotics"),
               target("node-b", "b:robotics"), target("node-b", "b:legal")]
    ordered = targets(manifest(members))
    assert ordered == [target("node-b", "b:legal"), target("node-b", "b:robotics"), target("node-c", "c:robotics")]
    sealed = json.loads((CONTRACTS / "fixtures" / "valid" / "scope-manifest-sealed.json").read_text())
    deduped = targets(sealed)
    assert deduped == sorted(deduped, key=lambda key: (key["origin_node_id"], key["collection_id"], key["operation"]))
    assert len(deduped) == len(sealed["expanded_members"])


def test_targets_keep_members_when_enumeration_is_partial_but_reject_broken_seals():
    members = [target("node-b", "b:robotics")]
    partial = manifest(members, state="partial", unexpanded=[{"node_id": "node-x", "reason": "timeout"}])
    assert targets(partial) == members
    with pytest.raises(ApplicationError, match="unexpanded"):
        targets(manifest(members, state="sealed", unexpanded=[{"node_id": "node-x", "reason": "timeout"}]))


def test_candidates_deterministic_bounded_and_never_drop_members():
    members = [target("node-a", "a:robotics"), target("node-b", "b:legal"), target("node-c", "c:robotics")]
    descriptors = [descriptor("node-c", "c:robotics", topics=["robotics"], languages=["en"])]
    first = candidates(members, descriptors, query="robotics review", limit=2, local_node_id="node-a")
    second = candidates(members, descriptors, query="robotics review", limit=2, local_node_id="node-a")
    assert first == second
    assert len(first) == 2
    assert first[0]["target_key"] == target("node-c", "c:robotics")  # 主题命中压过本地加分
    assert first[0]["score"] == 3 and "topic_match:1" in first[0]["reason"]
    every = candidates(members, descriptors, query="anything", limit=10, local_node_id="node-b")
    assert {row["target_key"]["origin_node_id"] for row in every} == {"node-a", "node-b", "node-c"}


def test_candidates_local_first_prefers_but_does_not_exclude_remote():
    members = [target("node-b", "b:robotics"), target("node-a", "a:robotics")]
    ranked = candidates(members, [], query="anything", limit=2, local_node_id="node-a")
    assert ranked[0]["target_key"]["origin_node_id"] == "node-a"
    assert "local" in ranked[0]["reason"]
    assert {row["target_key"]["origin_node_id"] for row in ranked} == {"node-a", "node-b"}


def test_candidates_freshness_first_orders_by_time_range():
    members = [target("node-b", "b:old"), target("node-c", "c:new")]
    descriptors = [descriptor("node-b", "b:old", to="2020-01-01T00:00:00Z"),
                   descriptor("node-c", "c:new", to="2026-01-01T00:00:00Z")]
    ranked = candidates(members, descriptors, query="anything", limit=2, ordering="freshness_first")
    assert [row["target_key"]["collection_id"] for row in ranked] == ["c:new", "b:old"]
    with pytest.raises(ApplicationError):
        candidates(members, descriptors, query="anything", limit=0)
    with pytest.raises(ApplicationError):
        candidates(members, descriptors, query="anything", limit=1, ordering="cheapest")


def test_root_budget_shared_caps_and_sub_caps():
    root = budget(max_requests=4, max_probe_requests=1, max_discovery_requests=1,
                  max_egress_bytes=50, max_generation_tokens=1)
    root.reserve("probe")
    with pytest.raises(ApplicationError) as probe_error:
        root.reserve("probe")
    assert probe_error.value.code == "budget_exhausted"
    root.reserve("retrieve")
    root.reserve("discovery")
    with pytest.raises(ApplicationError):
        root.reserve("discovery")
    root.reserve("bytes", 60)
    root.reserve("egress_bytes", 50)
    with pytest.raises(ApplicationError):
        root.reserve("egress_bytes", 1)
    root.reserve("hops")
    with pytest.raises(ApplicationError):
        root.reserve("hops")
    root.reserve("generation_tokens")
    with pytest.raises(ApplicationError):
        root.reserve("generation_tokens")
    assert root.used() == {"requests": 3, "bytes": 110, "generation_tokens": 1, "hops": 1, "discovery": 1}
    with pytest.raises(ApplicationError):
        root.reserve("mystery")
    with pytest.raises(ApplicationError):
        root.reserve("probe", -1)


def test_root_budget_never_resets_or_backfills():
    root = budget(max_requests=1)
    root.reserve("request")
    snapshot = root.used()
    for _ in range(3):
        with pytest.raises(ApplicationError) as exc:
            root.reserve("request")
        assert exc.value.code == "budget_exhausted"
        assert root.used() == snapshot
    expired = budget(deadline=utc_instant(NOW - 1))
    with pytest.raises(ApplicationError) as exc:
        expired.reserve("request")
    assert exc.value.code == "budget_exhausted"
    with pytest.raises(ApplicationError):
        RootBudget({"max_requests": 1, "max_bytes": 1}, now=NOW)


def test_plan_steps_build_a_plan_that_passes_validate_plan():
    steps, edges = plan_steps(
        targets=[target("node-b", "b:robotics"), target("node-c", "c:robotics")],
        probes=[probe("node-b"), probe("node-c", can_generate=True)],
        local_node_id="node-a", coordinator_node_id="node-a", query="which page has the evidence?", now=NOW)
    assert [(step["step_id"], step["operation"], step["executor_node_id"]) for step in steps] == [
        ("retrieve-1", "retrieve", "node-b"),
        ("retrieve-2", "retrieve", "node-c"),
        ("fuse-1", "fuse", "node-a"),
        ("answer-1", "answer", "node-c"),
    ]
    assert all(edge["authorised_by"] for edge in edges)
    spec = task_spec()
    plan = assemble(steps, edges, spec)
    plans.validate_plan(plan, spec, local_node_id="node-a", now=NOW)
    # 本地产生成能力时证据不出本地，answer 不再需要数据边。
    local_steps, local_edges = plan_steps(
        targets=[target("node-b", "b:robotics")], probes=[probe("node-b"), probe("node-a", can_generate=True)],
        local_node_id="node-a", coordinator_node_id="node-a", query="q", now=NOW)
    assert local_steps[-1] == {"step_id": "answer-1", "operation": "answer",
                               "executor_node_id": "node-a", "depends_on": ["fuse-1"]}
    assert len(local_edges) == 2


def test_plan_steps_without_generation_omits_answer():
    steps, edges = plan_steps(
        targets=[target("node-b", "b:robotics"), target("node-c", "c:robotics")],
        probes=[probe("node-b")], local_node_id="node-a", coordinator_node_id="node-a", query="q", now=NOW)
    assert [step["operation"] for step in steps] == ["retrieve", "retrieve", "fuse"]
    assert len(edges) == 4
    assert not any(step["operation"] == "answer" for step in steps)


def test_plan_steps_references_only_fresh_probe_receipts():
    fresh = probe("node-b")
    stale = probe("node-b", observed=utc_instant(NOW - 3600))
    stale["probe_id"] = "probe-stale"
    stale_capability = probe("node-c", observed=utc_instant(NOW - 3600), can_generate=True)
    steps, _ = plan_steps(targets=[target("node-b", "b:robotics")], probes=[fresh, stale, stale_capability],
                          local_node_id="node-a", coordinator_node_id="node-a", query="q", now=NOW)
    assert steps[0]["probe_refs"] == ["probe-node-b"]
    assert not any(step["operation"] == "answer" for step in steps)
    with pytest.raises(ApplicationError):
        plan_steps(targets=[], probes=[], local_node_id="node-a", coordinator_node_id="node-a", query="q", now=NOW)
