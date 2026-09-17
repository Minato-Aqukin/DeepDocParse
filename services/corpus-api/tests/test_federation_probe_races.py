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

from conftest import ORG
from ddp_corpus import federation, federation_tasks
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.federation_peers import Delegation, PeerDirectory, parse_peers
from ddp_corpus.main import app
from ddp_corpus.models import utcnow
from node_credentials_fixture import LocalControlSigner, caller, install
from test_federation_probes import (
    NODE, configure_federation, indexed_source, post_probe_peer, probe_body,
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


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    """本节点 NODE；PEER_NODE_ID 是控制面批准的同组织成员，组织取自信任记录。"""
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def peer(client, **over):
    """一个同组织远端节点对本节点的调用方：每次调用现签一张凭证。"""
    return caller(client, audience_node_id=NODE, **over)


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


async def test_create_plan_survives_a_local_probe_commit_collision_same_actor(
        actor_client, session, monkeypatch, app_state):
    # 旧 primed 经共享口令 HTTP 以同本地 actor 落行 → 新经同本地 actor 直接 run_probe 落行（联邦端点现为 peer-only，同组织 peer 与本地 actor 的 probe_id 不同，无碰撞，计数会变 2）。
    from ddp_corpus.models import utcnow as _utcnow
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
    local_actor = Actor(id="actor-alice", kind="user", organization_id=ORG,
                        role="contributor")
    primed = await federation.run_probe(
        session, local_actor, probe_body(collection["collection_id"],
                                         query="retrieval target"),
        now=_utcnow(), http=app_state.http, index=app_state.search_index,
        idempotency_key=key)
    assert primed["probe_kind"] == "evidence_retrieval"

    _flaky_get_once(monkeypatch)
    response = await actor_client.post("/api/v1/task-plans", json={"root_task_id": root})

    assert response.status_code == 200, response.text
    assert response.json()["planning_state"] == "ready"
    stored = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert stored["planning_state"] == "ready"
    assert await session.scalar(select(func.count()).select_from(FederationProbe)) == 1


async def test_create_plan_survives_a_remote_probe_savepoint_collision(
        actor_client, session, monkeypatch):
    stub = StubPeer(items=[{**peer_evidence(), "excerpt": "peer excerpt"}])
    peers = parse_peers(json.dumps({PEER_NODE: {
        "endpoint": "https://peer.example"}}), shared_token=False)

    def factory(actor, delegation=None):
        return PeerDirectory(peers, actor=actor, transport=stub.transport(),
                             signer=LocalControlSigner(issuer_node_id=NODE),
                             delegation=delegation, shared_token=False)

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
