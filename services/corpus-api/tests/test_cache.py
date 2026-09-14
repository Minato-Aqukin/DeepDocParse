"""P6 有界缓存：上限、确定性驱逐、失效、负面键与探测复用。

每个上限都有正反两面：写入确实落库，且超过上限时**返回前的表**已经压回上限
之内（不是等某个后台任务）。驱逐顺序用固定时间戳钉住 —— 排序键里的
`created_at` 只有在构造测试时递增才可复现。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import ACTOR, ORG, actor_headers
from ddp_corpus import cache, catalog
from ddp_corpus.cache import CacheLimits, FederationCacheEntry
from ddp_corpus.deps import Actor
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.models import as_aware, new_id, utcnow
from ddp_core.application.plans import content_digest
from ddp_core.application.probe import build_probe
from ddp_core.models import Base
from test_collection_catalog import create, publish, source

NODE = "node-" + "c" * 48
TARGET = {"origin_node_id": NODE, "collection_id": "col-1", "operation": "corpus.retrieve"}
PROBE_ACTOR = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")


def limits(**over) -> CacheLimits:
    base = {"entries": 100, "bytes": 100_000, "ttl_seconds": 900, "per_scope_entries": 100}
    base.update(over)
    return CacheLimits(**base)


async def row_count(session) -> int:
    return int(await session.scalar(select(func.count(FederationCacheEntry.id))) or 0)


async def keys(session) -> set[str]:
    return set((await session.scalars(select(FederationCacheEntry.cache_key))).all())


async def put_at(session, *, offset, scope_key="s", cache_key, value, lim, kind="k",
                 ttl_seconds=60, now):
    await cache.put(session, scope_key=scope_key, cache_key=cache_key, kind=kind,
                    value=value, ttl_seconds=ttl_seconds, limits=lim,
                    now=now + timedelta(microseconds=offset))


# --------------------------------------------------------------- 上限与驱逐

async def test_entries_cap_evicts_least_used_then_oldest(session):
    now = utcnow()
    lim = limits(entries=3, per_scope_entries=3)
    for index, key in enumerate(("a", "b", "c")):
        await put_at(session, offset=index, cache_key=key, value={"n": key}, lim=lim, now=now)
    # a 被读一次 -> hits=1；b/c 都是 0，b 先建。
    assert await cache.get(session, scope_key="s", cache_key="a", now=now) is not None
    await put_at(session, offset=3, cache_key="d", value={"n": "d"}, lim=lim, now=now)
    assert await keys(session) == {"a", "c", "d"}, "最不常使用（b）先被驱逐"
    assert await row_count(session) == 3

    await put_at(session, offset=4, cache_key="e", value={"n": "e"}, lim=lim, now=now)
    assert await keys(session) == {"a", "d", "e"}, "同 hits 时先建先出（c）"
    assert await row_count(session) == 3


async def test_per_scope_cap_does_not_starve_other_scopes(session):
    now = utcnow()
    lim = limits(entries=10, per_scope_entries=2)
    for index, key in enumerate(("a", "b", "c")):
        await put_at(session, offset=index, scope_key="org-1", cache_key=key,
                     value={"n": key}, lim=lim, now=now)
    assert await row_count(session) == 2, "单 scope 超限时在被写入的那个 scope 内驱逐"
    await put_at(session, offset=3, scope_key="org-2", cache_key="z",
                 value={"n": "z"}, lim=lim, now=now)
    assert await row_count(session) == 3
    scoped = list((await session.scalars(select(FederationCacheEntry.scope_key))).all())
    assert scoped.count("org-2") == 1


async def test_bytes_cap_is_enforced_by_eviction(session):
    now = utcnow()
    one = cache.payload_bytes({"p": "a" * 100})
    two = cache.payload_bytes({"p": "b" * 100})
    three = cache.payload_bytes({"p": "c" * 100})
    assert one == two == three
    lim = limits(entries=100, bytes=one + two, per_scope_entries=100)
    await put_at(session, offset=0, cache_key="v1", value={"p": "a" * 100}, lim=lim, now=now)
    await put_at(session, offset=1, cache_key="v2", value={"p": "b" * 100}, lim=lim, now=now)
    await put_at(session, offset=2, cache_key="v3", value={"p": "c" * 100}, lim=lim, now=now)
    total = int(await session.scalar(select(func.coalesce(func.sum(FederationCacheEntry.bytes), 0))))
    assert total <= lim.bytes, "写入返回时字节数必须已回落到上限内"
    assert await keys(session) == {"v2", "v3"}, "字节超限先驱逐最不常使用的旧条目"


async def test_value_larger_than_byte_cap_is_not_stored_and_replaces_old(session):
    now = utcnow()
    value = {"p": "x" * 100}
    await cache.put(session, scope_key="s", cache_key="k", kind="k", value={"old": True},
                    ttl_seconds=60, limits=limits(), now=now)
    lim = limits(bytes=cache.payload_bytes(value) - 1)
    await cache.put(session, scope_key="s", cache_key="k", kind="k", value=value,
                    ttl_seconds=60, limits=lim, now=now)
    assert await row_count(session) == 0, "装不下的值不截断、不落库；同键旧值一并清掉"
    assert await cache.get(session, scope_key="s", cache_key="k", now=now) is None


async def test_ttl_is_clamped_to_the_limit_and_expiry_is_lazy(session):
    now = utcnow()
    lim = limits(ttl_seconds=100)
    await cache.put(session, scope_key="s", cache_key="k", kind="k", value={"v": 1},
                    ttl_seconds=10_000, limits=lim, now=now)
    row = await session.scalar(select(FederationCacheEntry).where(
        FederationCacheEntry.cache_key == "k"))
    assert as_aware(row.expires_at) == now + timedelta(seconds=100), "请求 TTL 被压到上限"
    assert await cache.get(session, scope_key="s", cache_key="k",
                           now=now + timedelta(seconds=99)) is not None
    assert await cache.get(session, scope_key="s", cache_key="k",
                           now=now + timedelta(seconds=101)) is None
    assert await row_count(session) == 0, "过期读取顺手删除"


async def test_nonpositive_ttl_and_invalid_limits_are_rejected(session):
    now = utcnow()
    with pytest.raises(ValueError):
        await cache.put(session, scope_key="s", cache_key="k", kind="k", value={},
                        ttl_seconds=0, limits=limits(), now=now)
    with pytest.raises(ValueError):
        CacheLimits(entries=0)
    with pytest.raises(ValueError):
        CacheLimits(bytes=0)
    with pytest.raises(ValueError):
        CacheLimits(ttl_seconds=0)
    with pytest.raises(ValueError):
        CacheLimits(per_scope_entries=0)


async def test_put_is_an_upsert_and_keeps_the_hit_counter(session):
    now = utcnow()
    lim = limits()
    await put_at(session, offset=0, cache_key="k", value={"v": 1}, lim=lim, kind="first", now=now)
    await cache.get(session, scope_key="s", cache_key="k", now=now)
    await cache.get(session, scope_key="s", cache_key="k", now=now)
    await put_at(session, offset=1, cache_key="k", value={"v": 2}, lim=lim, kind="second", now=now)
    assert await row_count(session) == 1
    row = await session.scalar(select(FederationCacheEntry))
    assert row.kind == "second" and row.hits == 2
    assert await cache.get(session, scope_key="s", cache_key="k", now=now) == {"v": 2}


async def test_concurrent_puts_do_not_exceed_caps(tmp_path):
    """SQLite 文件库上并发写入：写事务串行化 + put 内的上限判定，最终不超限。

    PostgreSQL 下同样的用例还覆盖 advisory lock（见 test_cache_pg.py）：
    SQLite 靠单写者，PG 靠显式锁，两者都必须给出「上限内」的结果。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cache.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lim = limits(entries=3, per_scope_entries=3)
    now = utcnow()

    async def writer(index):
        async with factory() as worker:
            await cache.put(worker, scope_key="race", cache_key=f"k{index}", kind="k",
                            value={"i": index}, ttl_seconds=60, limits=lim,
                            now=now + timedelta(microseconds=index))
            await worker.commit()

    await asyncio.gather(*(writer(index) for index in range(12)))
    async with factory() as check:
        total = int(await check.scalar(select(func.count(FederationCacheEntry.id))) or 0)
    assert total <= 3, f"并发写入后条目数 {total} 超过上限"
    await engine.dispose()


# --------------------------------------------------------------- 失效

async def test_invalidate_supports_each_filter_and_combinations(session):
    now = utcnow()
    lim = limits()
    await put_at(session, offset=0, scope_key="s1", cache_key="k1", kind="a",
                 value={"v": 1}, lim=lim, now=now)
    await put_at(session, offset=1, scope_key="s1", cache_key="k2", kind="b",
                 value={"v": 2}, lim=lim, now=now)
    await put_at(session, offset=2, scope_key="s2", cache_key="k3", kind="a",
                 value={"v": 3}, lim=lim, now=now)
    assert await cache.invalidate(session, scope_key="s1") == 2
    assert await keys(session) == {"k3"}
    assert await cache.invalidate(session, kind="a") == 1
    assert await row_count(session) == 0

    await put_at(session, offset=3, scope_key="s1", cache_key="k4", kind="a",
                 value={"v": 4}, lim=lim, now=now)
    assert await cache.invalidate(session, cache_key="k4") == 1
    assert await row_count(session) == 0


async def test_invalidate_requires_a_filter_and_is_zero_when_nothing_matches(session):
    with pytest.raises(ValueError):
        await cache.invalidate(session)
    assert await cache.invalidate(session, scope_key="missing") == 0


async def test_purge_expired_is_bounded_oldest_first(session):
    now = utcnow()
    lim = limits()
    for index in range(3):
        await put_at(session, offset=index, cache_key=f"old{index}", value={"i": index},
                     lim=lim, ttl_seconds=1, now=now)
    await put_at(session, offset=3, cache_key="fresh", value={"i": 3}, lim=lim, ttl_seconds=600, now=now)
    later = now + timedelta(seconds=2)
    assert await cache.purge_expired(session, now=later, limit=2) == 2
    assert await cache.purge_expired(session, now=later, limit=500) == 1
    assert await keys(session) == {"fresh"}
    with pytest.raises(ValueError):
        await cache.purge_expired(session, now=later, limit=0)


# --------------------------------------------------------------- 负面缓存键

def test_negative_key_binds_scope_node_and_revision():
    base = cache.negative_cache_key("org-1", "node-a", "rev-1")
    assert base != cache.negative_cache_key("org-2", "node-a", "rev-1")
    assert base != cache.negative_cache_key("org-1", "node-b", "rev-1")
    assert base != cache.negative_cache_key("org-1", "node-a", "rev-2")
    assert base == cache.negative_cache_key("org-1", "node-a", "rev-1")
    with pytest.raises(ValueError):
        cache.negative_cache_key("org-1", "node-a", "")


async def test_negative_entry_does_not_block_a_new_revision(session):
    """新节点修订（新上传/重新登记）绝不能命中旧否定条目。"""
    now = utcnow()
    await cache.record_negative(session, scope_key="org-1", node_id="node-a",
                                node_revision="rev-1", reason="unreachable", now=now)
    stale = await cache.get_negative(session, scope_key="org-1", node_id="node-a",
                                     node_revision="rev-1", now=now)
    assert stale is not None and stale["reason"] == "unreachable"
    assert await cache.get_negative(session, scope_key="org-1", node_id="node-a",
                                    node_revision="rev-2", now=now) is None
    assert await cache.get_negative(session, scope_key="org-2", node_id="node-a",
                                    node_revision="rev-1", now=now) is None
    # 短 TTL：过期后不再挡路。
    assert await cache.get_negative(session, scope_key="org-1", node_id="node-a",
                                    node_revision="rev-1",
                                    now=now + timedelta(seconds=cache.NEGATIVE_TTL_SECONDS + 1)) is None


# --------------------------------------------------------------- 探测键与复用

def probe_receipt(*, collection="col-1", query_digest, index_revision, observed_at,
                  node=NODE):
    return build_probe(
        probe_id="probe-" + new_id()[:20], target_node_id=node,
        task_spec_digest=content_digest(b"task-spec"), consent_ref="consent-1",
        probe_kind="evidence_retrieval",
        capability_check={"operation": "corpus.retrieve", "readiness": "ready"},
        retrieval={"status": "succeeded", "collection_ref": collection,
                   "index_revision": index_revision, "candidate_limit": 8,
                   "continuation_ref": None, "evidence_set_ref": None,
                   "internal_limits": []},
        can_generate=False, observed_at=observed_at.isoformat())


async def store_probe(session, *, collection="col-1", query_digest, index_revision,
                      observed_at, expires_at, policy_revision=None, org=ORG):
    result = probe_receipt(collection=collection, query_digest=query_digest,
                           index_revision=index_revision, observed_at=observed_at)
    envelope = {"kind": "evidence_retrieval", "result": result, "evidence": [],
                "request_digest": content_digest(b"request")}
    if policy_revision is not None:
        envelope["policy_revision"] = policy_revision
    probe_id = "probe-" + new_id()[:20]
    session.add(FederationProbe(
        probe_id=probe_id, organization_id=org, actor_id=ACTOR, target_node_id=NODE,
        task_spec_digest=content_digest(b"task-spec"), consent_ref="consent-1",
        probe_kind="evidence_retrieval", collection_id=collection,
        query_digest=query_digest, state="succeeded", result_json=envelope,
        expires_at=expires_at, created_at=observed_at))
    await session.flush()
    return result


async def find(session, *, query_digest, index_revision, policy_revision="policy-1", now,
               target=TARGET, actor=PROBE_ACTOR):
    return await cache.find_reusable_probe(
        session, actor, target_key=target, query_digest=query_digest,
        index_revision=index_revision, policy_revision=policy_revision, now=now)


async def test_probe_reuse_returns_the_stored_receipt(session):
    now = utcnow()
    query_digest = content_digest(b"question")
    revision = "sha256:" + "1" * 64
    stored = await store_probe(session, query_digest=query_digest, index_revision=revision,
                               observed_at=now, expires_at=now + timedelta(seconds=300))
    found = await find(session, query_digest=query_digest, index_revision=revision, now=now)
    assert found == stored, "返回的是存下来的回执本身，不是重新拼的近似物"


async def test_probe_reuse_rejects_expired_digest_revision_and_org(session):
    now = utcnow()
    query_digest = content_digest(b"question")
    revision = "sha256:" + "1" * 64
    await store_probe(session, query_digest=query_digest, index_revision=revision,
                      observed_at=now - timedelta(seconds=10),
                      expires_at=now - timedelta(seconds=9))
    assert await find(session, query_digest=query_digest, index_revision=revision, now=now) is None
    await store_probe(session, query_digest=query_digest, index_revision=revision,
                      observed_at=now, expires_at=now + timedelta(seconds=300))
    assert await find(session, query_digest=content_digest(b"other"),
                      index_revision=revision, now=now) is None
    assert await find(session, query_digest=query_digest,
                      index_revision="sha256:" + "2" * 64, now=now) is None
    assert await find(session, query_digest=query_digest, index_revision=revision, now=now,
                      actor=Actor(id=ACTOR, kind="user", organization_id="other-org",
                                  role="contributor")) is None


async def test_probe_reuse_policy_revision_only_enforced_when_recorded(session):
    now = utcnow()
    query_digest = content_digest(b"question")
    revision = "sha256:" + "1" * 64
    stored = await store_probe(session, query_digest=query_digest, index_revision=revision,
                               observed_at=now, expires_at=now + timedelta(seconds=300),
                               policy_revision="policy-1")
    assert await find(session, query_digest=query_digest, index_revision=revision,
                      policy_revision="policy-2", now=now) is None
    assert await find(session, query_digest=query_digest, index_revision=revision,
                      policy_revision="policy-1", now=now) == stored


async def test_probe_reuse_requires_recorded_index_revision(session):
    """回执缺 `retrieval.index_revision` 时不假装能复用 —— 直接 None。"""
    now = utcnow()
    query_digest = content_digest(b"question")
    session.add(FederationProbe(
        probe_id="probe-" + new_id()[:20], organization_id=ORG, actor_id=ACTOR,
        target_node_id=NODE, task_spec_digest=content_digest(b"task-spec"),
        consent_ref="consent-1", probe_kind="evidence_retrieval", collection_id="col-1",
        query_digest=query_digest, state="succeeded",
        result_json={"kind": "evidence_retrieval", "result": {
            "probe_kind": "evidence_retrieval", "target_node_id": NODE,
            "retrieval": {"collection_ref": "col-1"}, "observed_at": now.isoformat()}},
        expires_at=now + timedelta(seconds=300), created_at=now))
    await session.flush()
    assert await find(session, query_digest=query_digest,
                      index_revision="sha256:" + "1" * 64, now=now) is None


async def test_negative_entries_are_never_probe_receipts(session):
    now = utcnow()
    query_digest = content_digest(b"question")
    revision = "sha256:" + "1" * 64
    await cache.record_negative(session, scope_key=cache.organization_scope(ORG),
                                node_id=NODE, node_revision=revision,
                                reason="unreachable", now=now)
    assert await find(session, query_digest=query_digest, index_revision=revision, now=now) is None
    stored = await store_probe(session, query_digest=query_digest, index_revision=revision,
                               observed_at=now, expires_at=now + timedelta(seconds=300))
    found = await find(session, query_digest=query_digest, index_revision=revision, now=now)
    assert found == stored and found.get("negative") is not True


def test_probe_cache_key_binds_every_component():
    query_digest = content_digest(b"q")
    revision = "sha256:" + "1" * 64
    base = cache.probe_cache_key(TARGET, query_digest, revision, "policy-1")
    assert base == cache.probe_cache_key(TARGET, query_digest, revision, "policy-1")
    assert base != cache.probe_cache_key({**TARGET, "collection_id": "col-2"},
                                         query_digest, revision, "policy-1")
    assert base != cache.probe_cache_key(TARGET, content_digest(b"other"), revision, "policy-1")
    assert base != cache.probe_cache_key(TARGET, query_digest, "sha256:" + "2" * 64, "policy-1")
    assert base != cache.probe_cache_key(TARGET, query_digest, revision, "policy-2")
    with pytest.raises(ValueError):
        cache.probe_cache_key({"origin_node_id": NODE}, query_digest, revision, "policy-1")


# --------------------------------------------------------------- 目录指纹与撤回

def test_catalog_cache_revision_tracks_every_input():
    row = SimpleNamespace(id="col-1", revision=2, publication="published")
    first = catalog.cache_revision(row, "sha256:" + "1" * 64)
    assert first == catalog.cache_revision(row, "sha256:" + "1" * 64)
    assert first != catalog.cache_revision(SimpleNamespace(id="col-1", revision=3,
                                                           publication="published"),
                                           "sha256:" + "1" * 64)
    assert first != catalog.cache_revision(SimpleNamespace(id="col-1", revision=2,
                                                           publication="withdrawn"),
                                           "sha256:" + "1" * 64)
    assert first != catalog.cache_revision(row, "sha256:" + "2" * 64)
    assert first.startswith("sha256:")


async def test_withdrawn_collection_invalidates_cached_projection(actor_client, session):
    _, version, _, _ = await source(session)
    created = await create(actor_client, version)
    assert created.status_code == 201, created.text
    published = await publish(actor_client, created.json())
    assert published.status_code == 200, published.text
    collection = published.json()
    now = utcnow()
    scope = cache.collection_scope(collection["collection_id"])
    await cache.put(session, scope_key=scope, cache_key="projection", kind="collection",
                    value={"revision": collection["revision"]}, ttl_seconds=900,
                    limits=limits(), now=now)
    assert await cache.get(session, scope_key=scope, cache_key="projection", now=now) is not None
    withdraw = await actor_client.post(
        f"/api/v1/collections/{collection['collection_id']}/withdraw",
        headers={**actor_headers(), "Idempotency-Key": "withdraw"},
        json={"expected_revision": collection["revision"]})
    assert withdraw.status_code == 200, withdraw.text
    assert await cache.get(session, scope_key=scope, cache_key="projection", now=now) is None, \
        "撤回的对象任何版本都不该再从缓存提供"
