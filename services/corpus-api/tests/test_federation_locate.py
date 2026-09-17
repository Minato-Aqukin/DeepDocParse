"""P5 locate + evidence resolve: identity without bytes, authorization in the loop.

`LocateResult` never carries a URL or file bytes (a download URL is a temporary
location, not an identity). Evidence resolution shares the exact authorization
the MCP/evidence plane uses: readable content is not enough — the *fixed parse*
must be open to this actor, and a forged/unauthorized ref is indistinguishable
from a missing one.

认证形态：联邦端点只认节点凭证（`peer_unauthenticated` 另有专测）。
这里的调用方是受信任的同组织远端节点（`PeerCaller` 现签），不是本地用户 ——
本地用户走 client/MCP 平面，从不直接调联邦端点。所以"自己的资源"指本组织
已发布的资源（peer viewer 可见），"别人的"指别的组织或被撤销节点的。
"""
import pytest

from conftest import ORG
from ddp_corpus.main import app
from ddp_corpus.models import Chunk, Evidence, new_id
from ddp_core.anchor import digest_of
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
from test_federation_probes import BASE, NODE, configure_federation, headers, probe_body


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    """本节点 NODE；PEER_NODE_ID 是控制面批准的同组织成员，组织取自信任记录。"""
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def peer(client, **over):
    """一个同组织远端节点对本节点的调用方：每次调用现签一张凭证。"""
    return caller(client, audience_node_id=NODE, **over)


async def private_source(session, *, owner="actor-alice"):
    # peer viewer 只能读本组织已发布的资源：断言"可读"的前提是发布。
    resource, version, job, document = await asset(session, owner, publication="published")
    evidence = Evidence(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=0,
                        atom_key="source:0", page_idx=0, bbox=[5, 6, 100, 200],
                        page_size=[612, 792], kind="text", content="private locate text",
                        content_digest=digest_of("private locate text"))
    session.add(evidence)
    await session.flush()
    session.add(Chunk(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=0,
                      page_idx=0, bbox=[5, 6, 100, 200], page_size=[612, 792],
                      text="private locate text", char_len=19, block_type="text",
                      text_tokenized=tokenized("private locate text"), evidence_id=evidence.id))
    await session.commit()
    return resource, version, job, document, evidence


async def test_locate_own_resource_returns_identity_without_file_bytes(client, session, _peer_auth):
    resource, version, _, document, _ = await private_source(session)
    response = await peer(client).post(f"{BASE}/resources/locate",
        json_body={"resource_id": resource.id, "version_id": version.id})
    assert response.status_code == 200, response.text
    body = response.json()
    # 字段集由 federation-tasks-v1.yaml#LocateResult 冻结（additionalProperties=false）
    assert set(body) == {"resource_id", "version_id", "readable", "origin_node_id",
                         "authority_node_id", "source_digest", "policy_revision",
                         "unavailable_reason"}
    assert body["readable"] is True
    assert body["resource_id"] == resource.id and body["version_id"] == version.id
    assert body["source_digest"] == "sha256:" + document.doc_id
    assert body["origin_node_id"] == NODE and body["authority_node_id"] == NODE
    assert body["policy_revision"]
    assert body["unavailable_reason"] is None
    for forbidden in ("url", "download_url", "crop", "bytes", "content_base64", "object_key"):
        assert forbidden not in body, forbidden


async def test_locate_other_orgs_resource_is_denied(client, session, _peer_auth):
    """别的组织的远端节点看不到本组织的资源 —— 与"不存在"同形，不给存在性探测口。"""
    resource, version, _, _, _ = await private_source(session)
    _peer_auth.records[OTHER_NODE_ID] = trust_record(
        OTHER_NODE_ID, OTHER_PUBLIC_KEY, organization_id="other-org", authority_node_id=NODE)
    stranger = caller(client, audience_node_id=NODE, issuer_node_id=OTHER_NODE_ID,
                      key=OTHER_KEY)
    for payload in ({"resource_id": resource.id, "version_id": version.id},
                    {"resource_id": resource.id, "version_id": "0" * 32}):
        response = await stranger.post(f"{BASE}/resources/locate", json_body=payload)
        assert response.status_code == 404, payload
        assert response.json()["error"]["code"] in ("resource_not_found",
                                                    "resource_version_not_found")


async def test_locate_revoked_peer_is_rejected(client, session, _peer_auth):
    resource, version, _, _, _ = await private_source(session)
    _peer_auth.records[PEER_NODE_ID] = trust_record(
        PEER_NODE_ID, PEER_PUBLIC_KEY,
        organization_id=ORG, authority_node_id=NODE, state="revoked")
    response = await peer(client).post(f"{BASE}/resources/locate",
        json_body={"resource_id": resource.id, "version_id": version.id})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "node_revoked"


async def test_locate_unknown_version_is_not_found(client, session, _peer_auth):
    resource, _, _, _, _ = await private_source(session)
    response = await peer(client).post(f"{BASE}/resources/locate",
        json_body={"resource_id": resource.id, "version_id": "0" * 32})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_version_not_found"


async def test_resolve_returns_contract_envelope_without_internal_fields(client, session, _peer_auth):
    _, version, job, document, evidence = await private_source(session)
    response = await peer(client).post(f"{BASE}/results/resolve",
        json_body={"evidence_ref": evidence.id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "ddp-evidence/1#FederatedEvidence"
    assert body["evidence_id"] == evidence.id
    assert body["resource_id"] == version.resource_id
    assert body["source_version_id"] == version.id
    assert body["parse_revision"] == job.id
    assert body["source_type"] == "source" and body["derived_from"] is None
    assert body["locator"]["kind"] == "page_block"
    assert body["locator"]["seq"] == 0 and body["locator"]["bbox"] == [5, 6, 100, 200]
    assert body["locator"]["page_size"] == {"width": 612, "height": 792}
    assert body["source_digest"].startswith("sha256:")
    assert body["excerpt_digest"].startswith("sha256:")
    assert not any(key.startswith("_") for key in body), "内部审计字段不得出现在出口"


async def test_resolve_other_org_and_forged_refs_are_denied(client, session, _peer_auth):
    """别的组织的节点读不到 —— 与伪造引用同形，不给出存在性探测口。

    同组织的不同用户在 peer 模型里不可区分（组织取自信任记录，不取自报主体），
    所以"不是你的"只能按组织与信任状态表达。
    """
    _, _, _, _, evidence = await private_source(session)
    _peer_auth.records[OTHER_NODE_ID] = trust_record(
        OTHER_NODE_ID, OTHER_PUBLIC_KEY, organization_id="other-org", authority_node_id=NODE)
    stranger = caller(client, audience_node_id=NODE, issuer_node_id=OTHER_NODE_ID,
                      key=OTHER_KEY)
    denied = await stranger.post(f"{BASE}/results/resolve",
                                 json_body={"evidence_ref": evidence.id})
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "evidence_not_found"
    # 伪造引用与"不是你的"同形，不给出存在性探测口。
    forged = await peer(client).post(f"{BASE}/results/resolve",
                                     json_body={"evidence_ref": "0" * 32})
    assert forged.status_code == 404
    assert forged.json()["error"]["code"] == "evidence_not_found"


async def test_resolve_accepts_the_evidence_prefix(client, session, _peer_auth):
    _, _, _, _, evidence = await private_source(session)
    response = await peer(client).post(f"{BASE}/results/resolve",
        json_body={"evidence_ref": f"evidence:{evidence.id}"})
    assert response.status_code == 200
    assert response.json()["evidence_id"] == evidence.id


async def test_resource_locate_probe_reports_the_locate_capability(client, _peer_auth):
    """probe_kind=resource_locate 的 /probes 回执是 ProbeResult（契约），精确版本定位
    在 /resources/locate；这里报 corpus.locate 的就绪度。"""
    body = probe_body(kind="resource_locate", query="", include_digest=False)
    response = await peer(client).post(f"{BASE}/probes", json_body=body,
                                       headers={"Idempotency-Key": "locate-probe"})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["schema"] == "ddp-probe/1"
    assert result["probe_kind"] == "resource_locate"
    assert result["capability_check"]["operation"] == "corpus.locate"
    assert result["retrieval"] is None


async def test_locate_and_resolve_require_peer_credentials(actor_client, session):
    resource, version, _, _, evidence = await private_source(session)
    locate = await actor_client.post(f"{BASE}/resources/locate", headers=headers(peer=False),
        json={"resource_id": resource.id, "version_id": version.id})
    assert locate.status_code == 401
    assert locate.json()["error"]["code"] == "peer_unauthenticated"
    resolve = await actor_client.post(f"{BASE}/results/resolve", headers=headers(peer=False),
        json={"evidence_ref": evidence.id})
    assert resolve.status_code == 401
