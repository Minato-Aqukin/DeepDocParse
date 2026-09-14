"""N8：本地/远端 Probe 的确定性主键碰撞穿过 create_plan 时不得把规划打挂。

独立复验的反例：`federation.run_probe` 在碰撞分支里做全量 `session.rollback()`，
会把 `create_plan` 已加载的 `FederationRequest` expire；随后构造计划读属性触发
async 懒加载 → `MissingGreenlet` → 500。修复是 SAVEPOINT + 规划侧按主键重读；
这两条用例在旧行为下确定性变红。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import federation, federation_tasks
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.federation_peers import PeerDirectory, parse_peers
from ddp_corpus.models import utcnow
from test_federation_probes import (
    NODE, configure_federation, indexed_source, post_probe, probe_body,
    publish_collection,
)
from test_federation_tasks import (
    PEER_NODE, StubPeer, create_intent, exploration, member, peer_evidence,
    peer_probe, scope_manifest, task_spec,
)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


def _flaky_get_once(monkeypatch):
    """让 probe 的存在性预检恰好 miss 一次，碰撞落在唯一约束上。"""
    real_get = AsyncSession.get
    misses = {"remaining": 1}

    async def _flaky_get(self, entity, ident, *args, **kwargs):
        if entity is FederationProbe and misses["remaining"]:
            misses["remaining"] -= 1
            return None
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", _flaky_get)
    return real_get


async def test_create_plan_survives_a_local_probe_commit_collision(
        actor_client, session, monkeypatch):
    _resource, version, _job, _doc, _evidence = await indexed_source(session)
    collection = await publish_collection(actor_client, version, key="n8-local-probe")
    spec = task_spec(scope="site_public", mode="fast")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(actor_client, spec=spec, consent=consent)
    root = intent["root_task_id"]

    target = {"origin_node_id": NODE, "collection_id": collection["collection_id"],
              "operation": federation_tasks.RETRIEVAL_OPERATION}
    key = federation_tasks._probe_key(root, target)
    primed = await post_probe(actor_client, probe_body(collection["collection_id"],
                                                       query="retrieval target"), key=key)
    assert primed.status_code == 201, primed.text

    _flaky_get_once(monkeypatch)
    response = await actor_client.post("/api/v1/task-plans", json={"root_task_id": root})

    assert response.status_code == 200, response.text
    assert response.json()["planning_state"] == "ready"
    stored = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert stored["planning_state"] == "ready"
    assert await session.scalar(select(func.count()).select_from(FederationProbe)) == 1


async def test_create_plan_survives_a_remote_probe_savepoint_collision(
        actor_client, session, monkeypatch):
    peer = StubPeer(items=[{**peer_evidence(), "excerpt": "peer excerpt"}])
    peers = parse_peers(json.dumps({PEER_NODE: {
        "endpoint": "https://peer.example", "service_token": "s", "peer_token": "p"}}))

    def factory(actor):
        return PeerDirectory(peers, actor=actor, transport=peer.transport())

    monkeypatch.setattr(federation_tasks, "peer_directory", factory)

    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)], enumeration="sealed")
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                       scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    target = {"origin_node_id": PEER_NODE, "collection_id": "peer-collection-1",
              "operation": federation_tasks.RETRIEVAL_OPERATION}
    key = federation_tasks._probe_key(root, target)
    actor = Actor(id="actor-alice", kind="user", organization_id="org-test",
                  role="contributor")
    await federation_tasks._persist_remote_probe(
        session, actor, peer_probe(), [peer_evidence()], key=key,
        task_spec_digest="sha256:" + "a" * 64, consent_ref="explore-1",
        query_digest="sha256:" + "b" * 64, request_digest="sha256:" + "c" * 64,
        now=utcnow())

    _flaky_get_once(monkeypatch)
    response = await actor_client.post("/api/v1/task-plans", json={"root_task_id": root})

    assert response.status_code == 200, response.text
    stored = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert stored["planning_state"] == "ready"
