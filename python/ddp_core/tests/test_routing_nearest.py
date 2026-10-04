"""T36: 距离只排序合格目标，不能扩张冻结范围或删掉穷查成员。"""
import pytest

from ddp_core.application.routing import candidates, plan_steps, targets
from test_routing import NOW, descriptor, manifest, probe, target


def test_nearest_node_without_the_collection_never_enters_candidates():
    far = target("node-z", "required-collection")
    frozen = targets(manifest([far]))
    ranked = candidates(
        frozen, [descriptor("node-a", "irrelevant", topics=["required"])],
        query="required", limit=1, local_node_id="node-a")
    assert [item["target_key"] for item in ranked] == [far]


@pytest.mark.parametrize("ordering", ["local_first", "cost_first", "freshness_first"])
def test_fewer_hops_rank_before_a_better_descriptor(ordering):
    direct = target("node-z", "direct")
    delegated = target("node-a", "delegated")
    deep = target("node-b", "deep")
    ranked = candidates(
        [deep, delegated, direct],
        [descriptor("node-a", "delegated", topics=["robotics"], to="2026-01-01T00:00:00Z")],
        query="robotics", limit=3, ordering=ordering, local_node_id="node-local",
        node_routes=[{"node_id": "node-a", "via_node_ids": ["node-p"]},
                     {"node_id": "node-b", "via_node_ids": ["node-p", "node-q"]}])
    assert [item["target_key"] for item in ranked] == [direct, delegated, deep]


def test_negative_cache_ranks_after_reachable_at_same_distance():
    unreachable = target("node-a", "strong-summary")
    reachable = target("node-z", "weak-summary")
    ranked = candidates(
        [unreachable, reachable], [descriptor("node-a", "strong-summary", topics=["robotics"])],
        query="robotics", limit=2, unreachable_node_ids={"node-a"})
    assert [item["target_key"] for item in ranked] == [reachable, unreachable]


def test_routing_metadata_never_removes_exhaustive_members():
    members = [target("node-a", "local"), target("node-b", "direct"), target("node-c", "far")]
    ranked = candidates(
        targets(manifest(members)), [], query="anything", limit=len(members), local_node_id="node-a",
        node_routes=[{"node_id": "node-c", "via_node_ids": ["node-b"]}],
        unreachable_node_ids={"node-b", "node-c"})
    assert [item["target_key"] for item in ranked] == members


def test_nearest_node_without_generation_capacity_never_gets_answer():
    steps, _ = plan_steps(
        targets=[target("node-a", "local"), target("node-z", "required")],
        probes=[probe("node-a", can_generate=False), probe("node-z", can_generate=True)],
        local_node_id="node-a", coordinator_node_id="node-a", query="required", now=NOW)
    assert [step["executor_node_id"] for step in steps if step["operation"] == "answer"] == ["node-z"]
