"""Opt-in PostgreSQL checks for `federation_cache_entries` (migrated scratch DB).

单测用 SQLite 覆盖语义；这条路径验的是 SQLite 给不了的三件事：
1. 迁移链真的建出了这张表（head 断言，不创建 metadata）；
2. `(scope_key, cache_key)` 唯一约束在真库上成立；
3. 多进程级并发下 advisory lock 让上限依旧成立。

未设置 `CACHE_TEST_DATABASE_URL` 时显式 skip（原因写得足够响）：
需要一个跑过 `alembic upgrade head` 的一次性 PostgreSQL（pgvector 非必需）。
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ddp_corpus import cache
from ddp_corpus.cache import CacheLimits, FederationCacheEntry
from ddp_corpus.models import new_id, utcnow


def alembic_head() -> str:
    """按迁移脚本现算 head，不写死版本号 —— 否则每次加迁移都变成假红。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "database/corpus/alembic.ini"))
    config.set_main_option("script_location", str(root / "database/corpus/alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture
async def cache_pg():
    dsn = os.getenv("CACHE_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("CACHE_TEST_DATABASE_URL must point to a migrated scratch PostgreSQL "
                    "database (e.g. a scratch pgvector container on port 15460+)")
    engine = create_async_engine(dsn, pool_size=8, max_overflow=16)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_pg_table_exists_at_head_and_enforces_unique(cache_pg):
    scope = "pg-cache-" + new_id()
    async with cache_pg() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == alembic_head()
        await cache.put(session, scope_key=scope, cache_key="k1", kind="k",
                        value={"v": 1}, ttl_seconds=60,
                        limits=CacheLimits(entries=10, bytes=10_000, ttl_seconds=60,
                                           per_scope_entries=10), now=utcnow())
        await session.commit()
    async with cache_pg() as session:
        # upsert 走 ORM；重复插入直接撞唯一约束才是迁移真的建了它。
        session.add(FederationCacheEntry(scope_key=scope, cache_key="k1", kind="k",
                                         value_json={"v": 2}, bytes=10, hits=0,
                                         created_at=utcnow(), expires_at=utcnow()))
        with pytest.raises(IntegrityError) as excinfo:
            await session.flush()
        assert "uq_federation_cache_scope_key" in str(excinfo.value)
        await session.rollback()
    async with cache_pg() as session:
        await cache.invalidate(session, scope_key=scope)
        await session.commit()


async def test_pg_json_roundtrip_caps_and_concurrent_puts(cache_pg):
    scope = "pg-cache-" + new_id()
    limits = CacheLimits(entries=5, bytes=64 * 1024, ttl_seconds=60, per_scope_entries=5)
    value = {"payload": {"nested": [1, 2, 3], "text": "中文"}, "flag": True}
    now = utcnow()
    async with cache_pg() as session:
        await cache.put(session, scope_key=scope, cache_key="roundtrip", kind="k",
                        value=value, ttl_seconds=60, limits=limits, now=now)
        await session.commit()
    async with cache_pg() as session:
        assert await cache.get(session, scope_key=scope, cache_key="roundtrip", now=now) == value
        await session.commit()

    async def writer(index):
        async with cache_pg() as session:
            await cache.put(session, scope_key=scope, cache_key=f"race-{index}", kind="k",
                            value={"i": index}, ttl_seconds=60, limits=limits,
                            now=now + timedelta(microseconds=index))
            await session.commit()

    await asyncio.gather(*(writer(index) for index in range(24)))
    async with cache_pg() as session:
        total, size = (await session.execute(select(
            func.count(FederationCacheEntry.id),
            func.coalesce(func.sum(FederationCacheEntry.bytes), 0)).where(
            FederationCacheEntry.scope_key == scope))).one()
        assert total <= limits.entries, f"并发写入后 {total} 条超过上限"
        assert size <= limits.bytes
        await cache.invalidate(session, scope_key=scope)
        await session.commit()
