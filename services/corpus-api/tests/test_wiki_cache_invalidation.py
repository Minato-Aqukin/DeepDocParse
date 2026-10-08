"""P6：来源被撤销/删除时，Wiki 发布指针与缓存投影都必须在同一事务里失效。

读路径（`wiki.dependency_state`）本来就会复核冻结来源；这里验的是它**之外**
的两道防线：`published_revision_id` 不能继续指向已撤回来源的修订，已建立的
缓存投影不能在撤回后继续被读出来。两个测试共用一条真实路径：建 Wiki ->
发布 -> 放缓存 -> DELETE 资源。
"""
import hashlib

import respx
from sqlalchemy import func, select, update
from conftest import actor_headers
from ddp_corpus import cache, wiki
from ddp_corpus.models import Evidence, Resource, Wiki, utcnow
from ddp_corpus.resources import tombstone_resource
from test_wiki_revisions import body, model, source


async def published_wiki(actor_client, session, *, key):
    resource, version, evidence, _ = await source(session, publication="published")
    model(evidence)
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": key})
    assert created.status_code == 201, created.text
    result = created.json()
    published = await actor_client.post(
        f"/api/wikis/{result['wiki']['id']}/publish",
        json={"base_revision_id": result["revision"]["id"]})
    assert published.status_code == 200, published.text
    return resource, result["wiki"]["id"]


async def put_projection(session, scope, now):
    await cache.put(session, scope_key=scope, cache_key="wiki-projection", kind="wiki",
                    value={"stale": False}, ttl_seconds=900, limits=cache.CacheLimits(),
                    now=now)


@respx.mock
async def test_tombstoned_source_unpublishes_wiki_and_purges_cache(actor_client, session):
    resource, wiki_id = await published_wiki(actor_client, session, key="p6-wiki")
    now = utcnow()
    scopes = (cache.wiki_scope(wiki_id), cache.resource_scope(resource.id))
    for scope in scopes:
        await put_projection(session, scope, now)
    await session.commit()
    assert (await actor_client.get(f"/api/wikis/{wiki_id}",
                                   headers=actor_headers("bob"))).status_code == 200

    deleted = await actor_client.delete(f"/api/resources/{resource.id}")
    assert deleted.status_code == 204, deleted.text

    row = await session.get(Wiki, wiki_id, populate_existing=True)
    assert row.published_revision_id is None, "撤回来源的 Wiki 不能继续有已发布指针"
    assert (await actor_client.get(f"/api/wikis/{wiki_id}",
                                   headers=actor_headers("bob"))).status_code == 404
    for scope in scopes:
        assert await cache.get(session, scope_key=scope, cache_key="wiki-projection",
                               now=now) is None, "缓存不能把撤回的投影复活"


@respx.mock
async def test_publication_flip_away_from_published_invalidates_wiki_and_cache(
        actor_client, session):
    """PATCH 把 publication 从 published 翻走与 DELETE 同等失效。

    Publish -> flip to private must clear published_revision_id and purge
    resource/version/collection projections in the same commit.
    """
    from ddp_corpus.collection_models import Collection
    from ddp_corpus.models import Chunk, Document, ParseJob, ResourceVersion, new_id
    from tests.conftest import ACTOR, ORG
    from tests.test_collection_catalog import (
        create as catalog_create, publish as catalog_publish)
    document = Document(id=new_id(), uploaded_by=ACTOR, organization_id=ORG,
        doc_id=new_id() * 2, origin="web", filename="manual.pdf", mime="application/pdf",
        size_bytes=100, object_key="flip-object-key")
    session.add(document)
    await session.flush()
    resource = Resource(id=new_id(), organization_id="org-test", owner_id="actor-alice",
        uploaded_by="actor-alice", display_name="Manual", publication="private")
    session.add(resource)
    await session.flush()
    job = ParseJob(id=new_id(), document_id=document.id, resource_id=resource.id,
        initiated_by="actor-alice", engine="borndigital", options_hash=new_id(),
        document_version=1, status="succeeded", index_status="ready")
    session.add(job)
    await session.flush()
    version = ResourceVersion(id=new_id(), resource_id=resource.id, version_no=1,
        document_id=document.id, source_digest=document.doc_id, filename="manual.pdf",
        size_bytes=document.size_bytes, parse_job_id=job.id)
    session.add(version)
    await session.flush()
    session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=0,
        text="flip fact", text_tokenized="flip fact"))
    await session.commit()
    published = await actor_client.patch(
        f"/api/resources/{resource.id}", json={"publication": "published"})
    assert published.status_code == 200, published.text
    assert published.json()["publication"] == "published"
    from tests.test_wiki_revisions import body as wiki_body, model as wiki_model
    evidence = Evidence(id=new_id(), document_id=document.id, parse_job_id=job.id,
        seq=0, atom_key="text-0", content="flip fact",
        content_digest=hashlib.sha256(b"flip fact").hexdigest(), kind="text", page_idx=0,
        bbox=[0, 0, 50, 50], page_size=[100, 100])
    session.add(evidence)
    await session.flush()
    session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=1,
        text="flip fact", text_tokenized="flip fact", evidence_id=evidence.id))
    await session.commit()
    wiki_model(evidence)
    created = await actor_client.post("/api/wikis", json=wiki_body(resource, version),
        headers={"Idempotency-Key": "p6-flip"})
    assert created.status_code == 201, created.text
    wiki_id = created.json()["wiki"]["id"]
    released = await actor_client.post(f"/api/wikis/{wiki_id}/publish",
        json={"base_revision_id": created.json()["revision"]["id"]})
    assert released.status_code == 200, released.text
    row = await session.get(Wiki, wiki_id, populate_existing=True)
    assert row.published_revision_id is not None
    collection = (await catalog_create(
        actor_client, version, key="p6-flip-collection")).json()
    assert (await catalog_publish(actor_client, collection, key="p6-flip-collection")).status_code == 200
    collection_id = collection["collection_id"]
    assert (await session.get(Collection, collection_id)) is not None
    now = utcnow()
    scopes = (cache.wiki_scope(wiki_id), cache.resource_scope(resource.id),
              cache.version_scope(version.id), cache.collection_scope(collection_id))
    for scope in scopes:
        await put_projection(session, scope, now)
    await session.commit()
    flipped = await actor_client.patch(
        f"/api/resources/{resource.id}", json={"publication": "private"})
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["publication"] == "private"
    row = await session.get(Wiki, wiki_id, populate_existing=True)
    assert row.published_revision_id is None, "翻成私有的 Wiki 不能继续有已发布指针"
    for scope in scopes:
        assert await cache.get(session, scope_key=scope, cache_key="wiki-projection",
                               now=now) is None, "翻成私有的投影必须在同一提交里失效"


@respx.mock
async def test_invalidation_hook_is_idempotent(actor_client, session):
    resource, wiki_id = await published_wiki(actor_client, session, key="p6-idem")
    first = await wiki.invalidate_dependents(session, resource_id=resource.id)
    await session.commit()
    assert first == [wiki_id]
    row = await session.get(Wiki, wiki_id, populate_existing=True)
    assert row.published_revision_id is None
    remaining = int(await session.scalar(select(func.count())
                                         .select_from(cache.FederationCacheEntry)) or 0)

    second = await wiki.invalidate_dependents(session, resource_id=resource.id)
    await session.commit()
    assert second == [wiki_id], "依赖关系还在，返回同样的受影响集合"
    assert int(await session.scalar(select(func.count())
                                    .select_from(cache.FederationCacheEntry)) or 0) == remaining
    await tombstone_resource(session, await session.get(Resource, resource.id))
    await session.commit()
    await tombstone_resource(session, await session.get(Resource, resource.id))
    await session.commit()
    assert (await session.get(Wiki, wiki_id, populate_existing=True)).published_revision_id is None


@respx.mock
async def test_unrelated_resource_tombstone_leaves_wiki_alone(actor_client, session):
    _, wiki_id = await published_wiki(actor_client, session, key="p6-other")
    other, _, _, _ = await source(session, publication="published", text="Unrelated fact.")
    await tombstone_resource(session, await session.get(Resource, other.id))
    await session.commit()
    row = await session.get(Wiki, wiki_id, populate_existing=True)
    assert row.published_revision_id is not None
