"""T36: 最近路径和组织负面观测只影响排序，能力/许可仍是硬门。"""
import json
from datetime import timedelta

import httpx
import pytest

from conftest import ACTOR, ORG
from ddp_core.application import routing
from ddp_corpus import cache, federation_tasks
from ddp_corpus.deps import Actor
from ddp_corpus.federation_peers import Delegation, PeerDirectory, parse_peers
from ddp_corpus.models import utcnow
from node_credentials_fixture import LocalControlSigner
from test_federation_probes import configure_federation
from test_federation_tasks import EXPIRY, exploration, member, peer_capability_probe, task_spec

LOCAL = "node-" + "0" * 48
DIRECT = "node-" + "f" * 48
FAR = "node-" + "a" * 48


def route_manifest():
    return {"node_routes": [{"node_id": FAR, "via_node_ids": [DIRECT]}]}


def test_fast_equal_score_prefers_direct_holder_over_delegated_holder():
    far_holders = [member(f"far-{index}", FAR) for index in range(8)]
    selected = federation_tasks._select_targets(
        [*far_holders, member("direct", DIRECT)], task_spec(), LOCAL, manifest=route_manifest())
    assert selected[0] == member("direct", DIRECT)
    assert len(selected) == 8
    assert len([target for target in selected if target["origin_node_id"] == FAR]) == 7


def test_exhaustive_keeps_unreachable_and_delegated_holders():
    selected = federation_tasks._select_targets(
        [member("far", FAR), member("direct", DIRECT)], task_spec(mode="exhaustive_scope"), LOCAL,
        manifest=route_manifest(), unreachable_node_ids={DIRECT, FAR})
    assert selected == [member("direct", DIRECT), member("far", FAR)]


@pytest.mark.parametrize("direct_ready,allowed,negative,expected,calls", [
    (True, True, (), DIRECT, [DIRECT]),
    (False, True, (), FAR, [DIRECT, FAR]),
    (True, False, (), FAR, [FAR]),
    (True, True, (DIRECT,), FAR, [FAR]),
])
async def test_answer_probes_nearest_allowed_ready_node(
        monkeypatch, direct_ready, allowed, negative, expected, calls):
    configure_federation(monkeypatch)
    monkeypatch.setattr("ddp_corpus.config.settings.bundle_node_id", LOCAL)
    contacted = []

    def respond(request):
        origin = DIRECT if request.url.host == "direct.example" else FAR
        contacted.append(origin)
        ready = origin == FAR or direct_ready
        body = json.loads(request.content)
        result = peer_capability_probe(
            operation="rag.answer.cited", readiness="ready" if ready else "unhealthy",
            can_generate=ready)
        result.update(target_node_id=origin, task_spec_digest=body["task_spec_digest"],
                      consent_ref=body["consent_ref"])
        return httpx.Response(200, json=result)

    directory = PeerDirectory(
        parse_peers(json.dumps({DIRECT: {"endpoint": "https://direct.example"},
                                FAR: {"endpoint": "https://far.example"}})),
        actor=Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor"),
        transport=httpx.MockTransport(respond), signer=LocalControlSigner(issuer_node_id=LOCAL),
        delegation=Delegation(root_task_id="root-nearest", task_spec_digest="sha256:" + "c" * 64))
    budget = routing.RootBudget({"max_requests": 8, "max_bytes": 1 << 20,
                                 "max_generation_tokens": 100, "max_hops": 8,
                                 "deadline": EXPIRY}, now=utcnow().timestamp())

    async def spend(*, kind, amount):
        budget.reserve(kind, amount)

    # Negative-cache ordering is tested at equal distance: no delegated route here.
    manifest = None if negative else route_manifest()
    chosen, outcomes = await federation_tasks._probe_answer_candidates(
        root_task_id="root-nearest", task_spec_digest="sha256:" + "c" * 64,
        consent=exploration(recipients=(DIRECT, FAR) if allowed else (FAR,)),
        scope_ref="scope-nearest", targets=[member("far", FAR), member("direct", DIRECT)],
        peers=directory, budget=budget, spend=spend, manifest=manifest,
        unreachable_node_ids=negative)
    assert chosen == expected
    assert contacted == calls
    assert outcomes[expected] == "ready"
    if not direct_ready:
        assert outcomes[DIRECT] == "not_ready"


@pytest.mark.parametrize("cache_org,revision,age,expected", [
    (ORG, "1", 0, DIRECT),
    ("other-org", "1", 0, FAR),
    (ORG, "2", 0, FAR),
    (ORG, "1", 61, FAR),
])
async def test_reachability_ranking_uses_only_live_current_org_revision(
        session, cache_org, revision, age, expected):
    now = utcnow()
    await cache.record_negative(
        session, scope_key=cache.organization_scope(cache_org), node_id=FAR,
        node_revision=revision, reason="unreachable:timeout",
        now=now - timedelta(seconds=age), ttl_seconds=60)
    manifest = {"registry_revision_vector": [
        {"node_id": FAR, "registry_revision": 1},
        {"node_id": DIRECT, "registry_revision": 1}]}
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    negative = await federation_tasks._ranking_unreachable_nodes(session, actor, manifest, now=now)
    selected = federation_tasks._select_targets(
        [member("far", FAR), member("direct", DIRECT)], task_spec(), LOCAL,
        manifest=manifest, unreachable_node_ids=set(negative))
    assert selected[0]["origin_node_id"] == expected
