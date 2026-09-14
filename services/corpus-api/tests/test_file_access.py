from ddp_corpus.models import Document, Resource, ResourceVersion
from tests.conftest import ACTOR, ORG, actor_headers


async def test_file_capability_is_bound_to_resource_and_revocation(actor_client, session):
    document = Document(id="doc1", uploaded_by=ACTOR, organization_id=ORG,
        doc_id="a"*64, filename="manual.pdf", mime="application/pdf", object_key="uploads/1")
    session.add(document)
    await session.flush()
    for rid in ("r1", "r2"):
        session.add(Resource(id=rid, organization_id=ORG, owner_id=ACTOR,
                             uploaded_by=ACTOR, display_name="manual.pdf"))
        await session.flush()
        session.add(ResourceVersion(id="v"+rid, resource_id=rid, version_no=1,
            document_id=document.id, source_digest=document.doc_id,
            filename=document.filename, size_bytes=1))
    await session.commit()
    path = "/internal/file-access/doc1"
    assert (await actor_client.get(path)).status_code == 409
    response = await actor_client.get(path, params={"resource_id":"r1"})
    assert response.status_code == 200
    assert response.json()["resource_id"] == "r1"
    assert response.json()["object_key"] == "uploads/1"
    denied = await actor_client.get(path, params={"resource_id":"r1"}, headers=actor_headers("other"))
    assert denied.status_code == 404
    from ddp_corpus.resources import tombstone_resource
    await tombstone_resource(session, await session.get(Resource,"r1"))
    await session.commit()
    # Another readable asset sharing the bytes must not revive the withdrawn grant.
    assert (await actor_client.get(path,params={"resource_id":"r1"})).status_code == 404
    assert (await actor_client.get(path,params={"resource_id":"r2"})).status_code == 200


async def test_file_access_requires_authenticated_context(client):
    assert (await client.get("/internal/file-access/missing")).status_code == 401
