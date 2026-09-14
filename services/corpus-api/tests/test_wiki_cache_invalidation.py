"""P6：来源被撤销/删除时，Wiki 发布指针与缓存投影都必须在同一事务里失效。

读路径（`wiki.dependency_state`）本来就会复核冻结来源；这里验的是它**之外**
的两道防线：`published_revision_id` 不能继续指向已撤回来源的修订，已建立的
缓存投影不能在撤回后继续被读出来。两个测试共用一条真实路径：建 Wiki ->
发布 -> 放缓存 -> DELETE 资源。
"""
import respx
from sqlalchemy import func, select

from conftest import actor_headers
from ddp_corpus import cache, wiki
from ddp_corpus.models import Resource, Wiki, utcnow
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
