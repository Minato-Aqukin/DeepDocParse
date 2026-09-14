"""P5 locate + evidence resolve: identity without bytes, authorization in the loop.

`LocateResult` never carries a URL or file bytes (a download URL is a temporary
location, not an identity). Evidence resolution shares the exact authorization
the MCP/evidence plane uses: readable content is not enough — the *fixed parse*
must be open to this actor, and a forged/unauthorized ref is indistinguishable
from a missing one.
"""
import pytest

from ddp_corpus.models import Chunk, Evidence, new_id
from ddp_core.anchor import digest_of
from ddp_core.tokenize import tokenized
from test_client_projection import asset
from test_federation_probes import BASE, NODE, configure_federation, headers


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)


async def private_source(session, *, owner="actor-alice"):
    resource, version, job, document = await asset(session, owner)
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


async def test_locate_own_resource_returns_identity_without_file_bytes(actor_client, session):
    resource, version, _, document, _ = await private_source(session)
    response = await actor_client.post(f"{BASE}/resources/locate", headers=headers(),
        json={"resource_id": resource.id, "version_id": version.id})
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


async def test_locate_another_actors_or_orgs_resource_is_denied(actor_client, session):
    resource, version, _, _, _ = await private_source(session)
    for who, org in (("bob", "org-test"), ("actor-alice", "other-org")):
        response = await actor_client.post(f"{BASE}/resources/locate",
            headers=headers(who, org=org),
            json={"resource_id": resource.id, "version_id": version.id})
        assert response.status_code == 404, (who, org, response.text)
        assert response.json()["error"]["code"] == "resource_not_found"


async def test_locate_unknown_version_is_not_found(actor_client, session):
    resource, _, _, _, _ = await private_source(session)
    response = await actor_client.post(f"{BASE}/resources/locate", headers=headers(),
        json={"resource_id": resource.id, "version_id": "0" * 32})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_version_not_found"


async def test_resolve_returns_contract_envelope_without_internal_fields(actor_client, session):
    _, version, job, document, evidence = await private_source(session)
    response = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
        json={"evidence_ref": evidence.id})
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


async def test_resolve_other_actor_and_forged_refs_are_denied(actor_client, session):
    _, _, _, _, evidence = await private_source(session)
    denied = await actor_client.post(f"{BASE}/results/resolve", headers=headers("bob"),
                                     json={"evidence_ref": evidence.id})
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "evidence_not_found"
    # 伪造引用与"不是你的"同形，不给出存在性探测口。
    forged = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
                                     json={"evidence_ref": "0" * 32})
    assert forged.status_code == 404
    assert forged.json()["error"]["code"] == "evidence_not_found"


async def test_resolve_accepts_the_evidence_prefix(actor_client, session):
    _, _, _, _, evidence = await private_source(session)
    response = await actor_client.post(f"{BASE}/results/resolve", headers=headers(),
        json={"evidence_ref": f"evidence:{evidence.id}"})
    assert response.status_code == 200
    assert response.json()["evidence_id"] == evidence.id


async def test_resource_locate_probe_reports_the_locate_capability(actor_client):
    """probe_kind=resource_locate 的 /probes 回执是 ProbeResult（契约），精确版本定位
    在 /resources/locate；这里报 corpus.locate 的就绪度。"""
    from test_federation_probes import post_probe, probe_body

    body = probe_body(kind="resource_locate", query="", include_digest=False)
    response = await post_probe(actor_client, body, key="locate-probe")
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
