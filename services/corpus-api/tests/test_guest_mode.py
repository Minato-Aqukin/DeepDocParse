"""Guest mode (游客模式): published-only reads, nothing private, nothing written.

游客看到的正是本站已发布集合（`publication = published`），与
`policy.public_viewer` 同一条谓词；写操作在 `deps.current_actor` 一层
直接 403（`guest_read_only`），不落库、不计量、不落 outbox。
"""
import httpx
import pytest
import respx
from sqlalchemy import func, select

from ddp_contracts import ACTOR_KIND_VALUES
from ddp_corpus.deps import Actor
from ddp_corpus.models import CorpusOutbox, Evidence, ParseJob, new_id
from tests.conftest import ACTOR, ORG, EMBEDDINGS, actor_headers
from tests.test_resource_acl import seed


def guest_headers(**overrides) -> dict[str, str]:
    """入口下发的游客身份头：kind=guest，actor=guest，role=viewer，无用户。"""
    headers = actor_headers("guest", role="viewer", kind="guest")
    assert "X-DDP-User" not in headers
    headers.update(overrides)
    return headers


async def test_guest_identity_borrows_nothing_and_owns_nothing():
    assert "guest" in ACTOR_KIND_VALUES
    guest = Actor(id="guest", kind="guest", organization_id=ORG, role="viewer")
    assert guest.principal_id is None


async def test_guest_lists_and_reads_published_but_not_private(actor_client, session):
    pub_doc, pub, pub_version, _, pub_evidence = await seed(
        session, owner="bob", publication="published", text="public plutonium manual")
    priv_doc, priv, _, _, priv_evidence = await seed(
        session, owner="bob", publication="private", text="private plutonium diary")
    guest = guest_headers()
    # 默认 scope=mine 按写权限列"我的"：游客 principal 为空，一条也不该有。
    assert (await actor_client.get("/api/resources", headers=guest)).json()["items"] == []
    public = await actor_client.get("/api/resources?scope=site_public", headers=guest)
    assert [item["id"] for item in public.json()["items"]] == [pub.id]
    assert (await actor_client.get(f"/api/resources/{pub.id}", headers=guest)).status_code == 200
    assert (await actor_client.get(
        f"/api/v1/resources/{pub.id}/versions/{pub_version.id}", headers=guest)).status_code == 200
    assert (await actor_client.get(f"/api/documents/{pub_doc.id}", headers=guest)).status_code == 200
    assert (await actor_client.get(f"/api/evidence/{pub_evidence.id}", headers=guest)).status_code == 200
    assert (await actor_client.get(
        f"/api/evidence/{pub_evidence.id}/backlinks", headers=guest)).status_code == 200
    summary = await actor_client.get("/api/documents/stats/summary", headers=guest)
    assert summary.json()["documents"] == 1

    denied = await actor_client.get(f"/api/resources/{priv.id}", headers=guest)
    unknown = await actor_client.get(f"/api/resources/{new_id()}", headers=guest)
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json(), "私有与不存在必须同形"
    assert (await actor_client.get(f"/api/documents/{priv_doc.id}", headers=guest)).status_code == 404
    assert (await actor_client.get(f"/api/documents/{priv_doc.id}/jobs", headers=guest)).status_code == 404
    assert (await actor_client.get(f"/api/evidence/{priv_evidence.id}", headers=guest)).status_code == 404
    assert "private plutonium diary" not in public.text

@respx.mock
async def test_guest_search_sees_published_hits_only_before_ranking(actor_client, session):
    await seed(session, owner="bob", publication="private", text="香蕉 香蕉 香蕉 香蕉")
    pub_doc, _, _, _, _ = await seed(session, owner="bob", publication="published",
                                     text="苹果 香蕉 橙子")
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))
    response = await actor_client.get("/api/search?q=香蕉&limit=1", headers=guest_headers())
    assert response.status_code == 200
    assert [g["document_id"] for g in response.json()["groups"]] == [pub_doc.id]
    assert "香蕉 香蕉" not in response.text


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/resources", {"document_id": "d", "display_name": "x"}),
    ("PATCH", "/api/resources/{resource}", {"display_name": "x"}),
    ("DELETE", "/api/resources/{resource}", None),
    ("POST", "/api/documents/{doc}/reparse", {"engine": "borndigital"}),
    ("PUT", "/api/documents/{doc}/current-job", {"job_id": "j"}),
    ("POST", "/api/documents/{doc}/reindex", {}),
    ("POST", "/api/evidence/{evidence}/verification", {"verdict": "pass"}),
    ("POST", "/api/wikis", {"title": "t", "sources": []}),
    ("POST", "/api/v1/collections", {"name": "c", "version_ids": []}),
])
async def test_guest_writes_are_rejected_before_touching_state(actor_client, session, method, path, body):
    doc, resource, _, job, evidence = await seed(session, owner="bob", publication="published")
    url = path.format(resource=resource.id, doc=doc.id, evidence=evidence.id)
    headers = guest_headers(**({"Idempotency-Key": "guest-write"} if method in ("POST", "PUT", "PATCH") else {}))
    response = await actor_client.request(method, url, headers=headers,
                                          **({"json": body} if body is not None else {}))
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "guest_read_only"
    assert response.json()["error"]["type"] == "permission_error"
    assert await session.scalar(select(func.count()).select_from(ParseJob)) == 1
    assert await session.scalar(select(func.count()).select_from(CorpusOutbox)) == 0
    assert await session.scalar(select(func.count()).select_from(Evidence)) == 1


async def test_guest_internal_file_access_authorizes_published_only(actor_client, session):
    pub_doc, _, _, _, _ = await seed(session, owner="bob", publication="published")
    priv_doc, _, _, _, _ = await seed(session, owner="bob", publication="private")
    guest = guest_headers()
    granted = await actor_client.get(f"/internal/file-access/{pub_doc.id}", headers=guest)
    assert granted.status_code == 200, granted.text
    assert granted.json()["object_key"] == "private/source.pdf"
    denied = await actor_client.get(f"/internal/file-access/{priv_doc.id}", headers=guest)
    unknown = await actor_client.get(f"/internal/file-access/{new_id()}", headers=guest)
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json()


async def test_unknown_actor_kinds_still_rejected(client):
    for kind in ("robot", "peer", ""):
        headers = actor_headers("someone", kind=kind or "user", role="viewer")
        headers["X-DDP-Actor-Kind"] = kind
        response = await client.get("/api/resources", headers=headers)
        assert response.status_code == 401, (kind, response.text)
        assert response.json()["error"]["code"] in ("unknown_actor_kind", "missing_actor_context")

async def test_guest_gets_no_stable_file_url(actor_client, session):
    pub_doc, _, _, _, _ = await seed(session, owner="bob", publication="published")
    response = await actor_client.get(f"/api/documents/{pub_doc.id}/source-url",
                                      headers=guest_headers())
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "guest_read_only"


async def test_guest_bundle_export_cannot_include_wiki(actor_client, session):
    # 附带 Wiki 的导出取"引用该版本的最新修订"，不走 Wiki 自己的发布判定 ——
    # 游客拿到它就等于读到别人的草稿 Wiki。默认导出（原文 + 出处 + 版面）不受影响。
    _, resource, version, _, _ = await seed(session, owner="bob", publication="published")
    url = f"/api/resources/{resource.id}/versions/{version.id}/bundle"
    for flag in ("true", "1", "on"):
        response = await actor_client.get(url, params={"include_wiki": flag}, headers=guest_headers())
        assert response.status_code == 403, (flag, response.text)
        assert response.json()["error"]["code"] == "guest_read_only"


@respx.mock
async def test_guest_reads_published_wiki_but_not_draft(actor_client, session):
    from tests.test_wiki_revisions import body, model, source
    resource, version, evidence, _ = await source(session, publication="published")
    model(evidence)
    first = (await actor_client.post("/api/wikis", json=body(resource, version),
        headers={"Idempotency-Key": "guest-wiki"})).json()
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    assert (await actor_client.get(f"/api/wikis/{wiki_id}",
                                   headers=guest_headers())).status_code == 404
    assert (await actor_client.post(f"/api/wikis/{wiki_id}/publish",
        json={"base_revision_id": revision_id})).status_code == 200
    read = await actor_client.get(f"/api/wikis/{wiki_id}", headers=guest_headers())
    assert read.status_code == 200 and read.json()["wiki"]["title"] == "Manual"
    listed = await actor_client.get("/api/wikis", headers=guest_headers())
    assert any(w["wiki"]["id"] == wiki_id for w in listed.json())


async def test_guest_reads_published_collection_only(actor_client, session):
    from tests.test_collection_catalog import create, publish, source
    _, version, _, _ = await source(session)
    collection = (await create(actor_client, version, key="guest-collection")).json()
    assert (await actor_client.get(
        f"/api/v1/collections/{collection['collection_id']}",
        headers=guest_headers())).status_code == 404
    assert (await publish(actor_client, collection, key="guest-publish")).status_code == 200
    read = await actor_client.get(f"/api/v1/collections/{collection['collection_id']}",
                                  headers=guest_headers())
    assert read.status_code == 200, read.text
    assert read.json()["publication"] == "published"
