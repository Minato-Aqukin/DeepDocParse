"""P5 evidence/capability probes: real retrieval, honest partials, fail closed.

The positive path proves the probe is backed by the same hybrid retrieval the
search plane uses (real chunk -> real Evidence -> locator), and the negative
paths prove a draft/foreign/other-org collection and a missing peer credential
are refused *before* anything is stored.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import text as sql_text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import ACTOR, ORG, SERVICE, actor_headers
from ddp_corpus import upstream
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.main import app
from ddp_corpus.models import Chunk, Evidence, new_id, utcnow
from ddp_core.anchor import digest_of
from ddp_core.application.plans import content_digest
from ddp_core.tokenize import tokenized
from node_credentials_fixture import (
    OTHER_KEY,
    OTHER_NODE_ID,
    OTHER_PUBLIC_KEY,
    PEER_NODE_ID,
    PEER_PUBLIC_KEY,
    caller,
    install,
    trust_record,
)
from test_client_projection import asset

NODE = "node-" + "f" * 48
PEER = "peer-token-for-tests"
BASE = "/api/v1/federation"
SCHEMAS = json.loads((Path(__file__).resolve().parents[3]
                      / "packages/contracts/generated/schemas-resolved.json").read_text())


def validate_probe_contract(probe: dict) -> None:
    schema = SCHEMAS["schemas"]["ddp-task-probe/v1.json"]
    Draft202012Validator({"$ref": "#/$defs/ProbeResult", "$defs": schema["$defs"]}).validate(probe)
FEDERATION_TABLES = {
    "federation_probes", "federation_admissions", "federation_executions",
    "federation_requests", "coverage_ledgers", "coverage_entries",
    "federation_deliveries",
}


def configure_federation(monkeypatch):
    """配好节点身份、peer 凭据与进程内 embedding 替身。

    探测要真的走检索，但不该在单测里发 HTTP。三个 `test_federation_*` 文件
    各自的 autouse 夹具都调它 —— autouse 夹具不随 import 传播。
    """
    monkeypatch.setattr(settings, "bundle_node_id", NODE)
    monkeypatch.setattr(settings, "federation_peer_token", PEER)
    monkeypatch.setattr(settings, "federation_admissions_enabled", True)

    async def _embed(_http, _text):
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(upstream, "embed_one", _embed)


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)


def headers(who=ACTOR, *, org=ORG, role="contributor", peer=True, target=True):
    out = actor_headers(who, organization_id=org, role=role)
    if peer:
        out["X-DDP-Peer-Token"] = PEER
    if target:
        out["X-DDP-Target-Node"] = NODE
    return out


async def indexed_source(session, *, owner=ACTOR, texts=("retrieval target text",)):
    """一个已发布资产 + 每个文本一条 Evidence/Chunk，真的能被混合检索命中。"""
    resource, version, job, document = await asset(session, owner, publication="published")
    evidence_rows = []
    for seq, text in enumerate(texts):
        evidence = Evidence(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=seq,
                            atom_key=f"source:{seq}:{text[:8]}", page_idx=0,
                            bbox=[10, 20, 300, 60], page_size=[612, 792], kind="text",
                            content=text, content_digest=digest_of(text))
        session.add(evidence)
        await session.flush()
        session.add(Chunk(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=0, bbox=[10, 20, 300, 60], page_size=[612, 792],
                          text=text, char_len=len(text), block_type="text",
                          text_tokenized=tokenized(text), evidence_id=evidence.id))
        evidence_rows.append(evidence)
    await session.commit()
    return resource, version, job, document, evidence_rows


async def publish_collection(client, version, *, who=ACTOR, key="probe-collection"):
    created = await client.post("/api/v1/collections",
        headers={**headers(who), "Idempotency-Key": key},
        json={"name": "Probe collection", "licence": "CC-BY-4.0", "languages": ["en"],
              "topics": ["probe"], "version_ids": [version.id]})
    assert created.status_code == 201, created.text
    body = created.json()
    published = await client.post(
        f"/api/v1/collections/{body['collection_id']}/publish",
        headers={**headers(who), "Idempotency-Key": key + "-publish"},
        json={"expected_revision": body["revision"]})
    assert published.status_code == 200, published.text
    return published.json()


def probe_body(collection_id=None, *, kind="evidence_retrieval", query="retrieval target",
               target=NODE, digest=None, include_digest=True, candidate_limit=8, **over):
    body = {"schema": "ddp-task-probe/1#ProbeRequest",
            "task_spec_digest": content_digest(b"task-spec"),
            "consent_ref": "consent-probe-1", "probe_kind": kind,
            "target_node_id": target, "scope_ref": "scope-1",
            "query": query, "candidate_limit": candidate_limit, **over}
    if collection_id is not None:
        body["collection_id"] = collection_id
    if include_digest:
        body["query_digest"] = content_digest(query.encode()) if digest is None else digest
    return body


async def post_probe(client, body, *, key="probe-key", who=ACTOR, **header_over):
    return await client.post(f"{BASE}/probes",
        headers={**headers(who, **header_over), "Idempotency-Key": key}, json=body)


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    """本节点 NODE；PEER_NODE_ID 是控制面批准的同组织成员，组织取自信任记录。"""
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def peer(client, **over):
    """一个同组织远端节点对本节点的调用方：每次调用现签一张凭证。"""
    return caller(client, audience_node_id=NODE, **over)


async def post_probe_peer(client, body, *, key="probe-key", peer_caller=None, **over):
    """经 PeerCaller 现签的联邦探测 POST（新 helper，旧 post_probe 保持原样供外部 import）。"""
    caller_obj = peer_caller if peer_caller is not None else peer(client, **over)
    return await caller_obj.post(f"{BASE}/probes", json_body=body,
                                 headers={"Idempotency-Key": key})


async def get_probe_peer(client, probe_id, *, task_spec_digest, peer_caller=None,
                         root_task_id="root-1", **over):
    """经 PeerCaller 现签的联邦探测回读；凭证约束带上该回执的需求修订。"""
    caller_obj = peer_caller if peer_caller is not None else peer(client, **over)
    return await caller_obj.get(f"{BASE}/probes/{probe_id}",
                                constraints={"root_task_id": root_task_id,
                                             "task_spec_digest": task_spec_digest})


def stranger(client, **over):
    """另一个组织的远端节点调用方（信任记录组织为 other-org）。"""
    return caller(client, audience_node_id=NODE, issuer_node_id=OTHER_NODE_ID,
                  key=OTHER_KEY, **over)


async def test_federation_tables_are_registered(engine):
    """main.py 的 import 链必须让 Base.metadata 认识这些表（否则 create_all 悄悄少建）。"""
    async with engine.connect() as conn:
        rows = await conn.execute(sql_text("SELECT name FROM sqlite_master WHERE type='table'"))
    names = {row[0] for row in rows}
    assert FEDERATION_TABLES <= names


async def test_published_collection_probe_returns_real_evidence_and_index_revision(
        client, session, _peer_auth):
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(client, version)
    response = await post_probe_peer(client, probe_body(collection["collection_id"]))
    assert response.status_code == 201, response.text
    probe = response.json()
    validate_probe_contract(probe)
    assert probe["schema"] == "ddp-probe/1" and probe["target_node_id"] == NODE
    assert probe["probe_kind"] == "evidence_retrieval"
    assert probe["retrieval"]["status"] == "succeeded"
    assert probe["retrieval"]["internal_limits"] == []
    assert probe["retrieval"]["index_revision"] == collection["index_revision"]
    assert probe["retrieval"]["evidence_set_ref"]
    assert probe["capability_check"]["input_validation"] == "content_verified"

    # 响应体是合同对象（retrieval 里没有摘录字段）；真实摘录与 locator 落在持久回执里。
    row = await session.get(FederationProbe, probe["probe_id"])
    excerpt = next(item for item in row.result_json["evidence"]
                   if item["evidence_id"] == evidence_rows[0].id)
    assert excerpt["_excerpt"] == "retrieval target text"
    assert excerpt["locator"]["seq"] == 0
    assert excerpt["locator"]["bbox"] == [10, 20, 300, 60]
    assert excerpt["locator"]["page_size"] == {"width": 612, "height": 792}
    assert excerpt["source_type"] == "source" and excerpt["derived_from"] is None


async def test_peer_probe_draft_and_foreign_collections_are_hidden(client, session, _peer_auth):
    # 旧 test_probe_denied_for_draft_and_foreign_collections → 新：本地 owner/bob/other-org 自报头在 peer 模型不可区分；
    # 草稿 owner 原 409 collection_not_published 改为同组织 peer 404 collection_not_found（peer viewer 不可管理，未发布与不存在同形），bob/异组织仍 404。
    _, version, *_ = await indexed_source(session)
    draft = await client.post("/api/v1/collections",
        headers={**headers(), "Idempotency-Key": "draft-collection"},
        json={"name": "Draft", "licence": "CC-BY-4.0", "languages": ["en"],
              "topics": [], "version_ids": [version.id]})
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["collection_id"]

    owner_peer = peer(client)
    same_org = await post_probe_peer(client, probe_body(draft_id), key="draft-owner",
                                     peer_caller=owner_peer)
    assert same_org.status_code == 404
    assert same_org.json()["error"]["code"] == "collection_not_found"

    _peer_auth.records[OTHER_NODE_ID] = trust_record(
        OTHER_NODE_ID, OTHER_PUBLIC_KEY, organization_id="other-org", authority_node_id=NODE)
    foreign_peer = stranger(client)
    foreign_draft = await post_probe_peer(
        client, probe_body(draft_id), key="draft-org", peer_caller=foreign_peer)
    assert foreign_draft.status_code == 404
    assert foreign_draft.json()["error"]["code"] == "collection_not_found"

    published = await publish_collection(client, version, key="peer-foreign-published")
    foreign_published = await post_probe_peer(
        client, probe_body(published["collection_id"]), key="draft-org-published",
        peer_caller=foreign_peer)
    assert foreign_published.status_code == 404
    assert foreign_published.json()["error"]["code"] == "collection_not_found"


async def test_probe_requires_peer_credentials(actor_client, session):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    response = await actor_client.post(
        f"{BASE}/probes", headers={**actor_headers(), "Idempotency-Key": "no-peer"},
        json=probe_body(collection["collection_id"]))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "peer_unauthenticated"


async def test_probe_wrong_target_node_is_conflict(client, session, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    response = await post_probe_peer(client, probe_body(collection["collection_id"],
                                                        target="node-other"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wrong_target"


async def test_truncated_candidates_report_partial(client, session, _peer_auth):
    _, version, *_ = await indexed_source(
        session, texts=("retrieval target one", "retrieval target two"))
    collection = await publish_collection(client, version)
    response = await post_probe_peer(
        client, probe_body(collection["collection_id"], candidate_limit=1),
        key="truncated-probe")
    assert response.status_code == 201, response.text
    retrieval = response.json()["retrieval"]
    assert retrieval["status"] == "partial"
    assert "truncated_by_limit" in retrieval["internal_limits"]


async def test_expired_probe_is_not_served(client, session, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    body = probe_body(collection["collection_id"])
    created = await post_probe_peer(client, body, key="expiring-probe")
    assert created.status_code == 201
    probe_id = created.json()["probe_id"]
    await session.execute(update(FederationProbe)
                          .where(FederationProbe.probe_id == probe_id)
                          .values(expires_at=utcnow() - timedelta(seconds=1)))
    await session.commit()
    got = await get_probe_peer(client, probe_id, task_spec_digest=body["task_spec_digest"])
    assert got.status_code == 410
    assert got.json()["error"]["code"] == "probe_expired"


async def test_peer_probe_receipt_is_scoped_to_the_acting_peer_subject(client, session, _peer_auth):
    # 旧 test_probe_receipt_is_scoped_to_the_acting_actor → 新：本地 bob/other-org 自报头改为同节点不同 subject 的 peer 与异组织 peer；同组织 peer 可读已发布但读不到别人的回执（404 同形）。
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    body = probe_body(collection["collection_id"])
    owner_peer = peer(client)
    created = await post_probe_peer(client, body, key="actor-scoped-probe",
                                    peer_caller=owner_peer)
    assert created.status_code == 201, created.text
    probe_id = created.json()["probe_id"]

    owner = await get_probe_peer(client, probe_id, task_spec_digest=body["task_spec_digest"],
                                 peer_caller=owner_peer)
    assert owner.status_code == 200, owner.text
    assert owner.json()["probe_id"] == probe_id

    other_subject = peer(client, subject="user-other")
    same_org = await get_probe_peer(client, probe_id, task_spec_digest=body["task_spec_digest"],
                                    peer_caller=other_subject)
    unknown = await get_probe_peer(client, "0" * 24, task_spec_digest=body["task_spec_digest"],
                                   peer_caller=other_subject)
    assert same_org.status_code == 404, same_org.text
    assert same_org.status_code == unknown.status_code
    assert same_org.json()["error"]["code"] == "probe_not_found"
    assert same_org.json()["error"]["code"] == unknown.json()["error"]["code"]

    _peer_auth.records[OTHER_NODE_ID] = trust_record(
        OTHER_NODE_ID, OTHER_PUBLIC_KEY, organization_id="other-org", authority_node_id=NODE)
    foreign = await get_probe_peer(client, probe_id, task_spec_digest=body["task_spec_digest"],
                                   peer_caller=stranger(client))
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "probe_not_found"


async def test_capability_probe_reports_readiness_and_real_input_validation(
        client, monkeypatch, _peer_auth):
    from ddp_corpus import capabilities

    async def _no_gateway(_http):
        return None

    monkeypatch.setattr(capabilities, "_fetch_gateway", _no_gateway)
    p = peer(client)
    ok = await post_probe_peer(client,
        probe_body(kind="capability_input", query="what is X"), key="cap-ok",
        peer_caller=p)
    assert ok.status_code == 201, ok.text
    validate_probe_contract(ok.json())
    assert ok.json()["capability_check"]["input_validation"] == "content_verified"
    assert ok.json()["retrieval"] is None

    bad = await post_probe_peer(client, probe_body(
        kind="capability_input", query="what is X", digest="sha256:" + "0" * 64),
        key="cap-bad", peer_caller=p)
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "input_not_verified"

    plain = await post_probe_peer(client, probe_body(
        kind="capability_input", query="what is X", include_digest=False), key="cap-plain",
        peer_caller=p)
    assert plain.status_code == 201
    assert plain.json()["capability_check"]["input_validation"] == "metadata_only"


@respx.mock
async def test_capability_probe_reports_requested_operation_and_can_generate(
        client, monkeypatch, _peer_auth):
    """`operation` 缺省 corpus.retrieve；问 rag.answer.cited 时按同一份清单回答。

    `can_generate` 只有该 operation readiness=ready 才为 true —— 这正是协调者
    规划远端 answer 委托的唯一依据。
    """
    # conftest 为编译指纹用例把 chat_model 设成了假名字；显式归零，让本层按
    # 网关的 default 通道解析（否则会撞上"网关没有这个 model"的 unhealthy）。
    monkeypatch.setattr(settings, "chat_url", "")
    monkeypatch.setattr(settings, "chat_model", "")
    now = datetime.now(timezone.utc)
    respx.get(f"{SERVICE}/v1/capabilities").mock(return_value=httpx.Response(200, json={
        "capability_status": "observed", "profiles": [],
        "model_channels": [{
            "channel": "chat", "model": "qwen3-4b-instruct", "default": True,
            "readiness": "ready", "supports": {"instruct": True},
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=60)).isoformat()}]}))
    p = peer(client)
    ready = await post_probe_peer(
        client,
        probe_body(kind="capability_input", operation="rag.answer.cited"),
        key="cap-generate", peer_caller=p)
    assert ready.status_code == 201, ready.text
    body = ready.json()
    validate_probe_contract(body)
    assert body["capability_check"]["operation"] == "rag.answer.cited"
    assert body["capability_check"]["readiness"] == "ready"
    assert body["can_generate"] is True

    # 检索路仍按缺省报告 corpus.retrieve，且 can_generate 保持 false。
    retrieve = await post_probe_peer(
        client, probe_body(kind="capability_input"), key="cap-retrieve-default",
        peer_caller=p)
    assert retrieve.status_code == 201
    assert retrieve.json()["capability_check"]["operation"] == "corpus.retrieve"
    assert retrieve.json()["can_generate"] is False


async def test_capability_probe_for_generation_is_unknown_without_model_observation(
        client, monkeypatch, _peer_auth):
    """观测不到模型通道时，生成型 operation 必须是 unknown/不可生成。

    拿检索库的 ready 去冒充模型侧就绪，就是"能力声明虚高"（F5）的同一个病。
    """
    from ddp_corpus import capabilities

    async def _no_gateway(_http):
        return None

    monkeypatch.setattr(capabilities, "_fetch_gateway", _no_gateway)
    probed = await post_probe_peer(
        client,
        probe_body(kind="capability_input", operation="rag.answer.cited"),
        key="cap-generate-unknown")
    assert probed.status_code == 201, probed.text
    body = probed.json()
    assert body["capability_check"]["readiness"] == "unknown"
    assert body["can_generate"] is False


async def test_dropped_member_reports_subset_only(client, session, monkeypatch, _peer_auth):
    """发布后某个成员读不到时，探测只能报"只查了子集"（T85），不得当成功。"""
    from ddp_corpus import federation

    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    real_search_contexts = federation.search_contexts

    async def _drop_members(session_, actor_, document_id=None, *, version_ids=None):
        if version_ids is not None:
            return {}
        return await real_search_contexts(session_, actor_, document_id, version_ids=version_ids)

    monkeypatch.setattr(federation, "search_contexts", _drop_members)
    response = await post_probe_peer(client, probe_body(collection["collection_id"]),
                                     key="subset-probe")
    assert response.status_code == 201, response.text
    retrieval = response.json()["retrieval"]
    assert retrieval["status"] == "partial"
    assert "subset_only" in retrieval["internal_limits"]
    assert retrieval["evidence_set_ref"] is None


async def test_probe_replay_is_idempotent(client, session, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    body = probe_body(collection["collection_id"])
    p = peer(client)
    first = await post_probe_peer(client, body, key="replay-probe", peer_caller=p)
    second = await post_probe_peer(client, body, key="replay-probe", peer_caller=p)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()

    conflict = await post_probe_peer(client, probe_body(collection["collection_id"],
                                                        query="another query"),
                                     key="replay-probe", peer_caller=p)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_probe_duplicate_insert_race_replays_and_conflicts(
        client, session, monkeypatch, _peer_auth):
    """N4：两个请求都通过了存在性预检时，唯一约束必须映射成重放/409 而不是 500。"""
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(client, version)
    body = probe_body(collection["collection_id"])
    p = peer(client)
    first = await post_probe_peer(client, body, key="race-probe", peer_caller=p)
    assert first.status_code == 201, first.text

    real_get = AsyncSession.get
    misses = {"remaining": 1}

    async def _flaky_get(self, entity, ident, *args, **kwargs):
        if entity is FederationProbe and misses["remaining"]:
            misses["remaining"] -= 1
            return None
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", _flaky_get)
    replay = await post_probe_peer(client, body, key="race-probe", peer_caller=p)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    misses["remaining"] = 1
    conflict = await post_probe_peer(client, probe_body(collection["collection_id"],
                                                        query="another query"),
                                     key="race-probe", peer_caller=p)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
