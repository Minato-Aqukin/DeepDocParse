"""Opt-in migrated PostgreSQL receipts/CAS/revocation checks, never create_all."""
import asyncio
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import actor_headers
from ddp_corpus import db
from ddp_corpus.collection_models import Collection, CollectionCatalogSnapshot, CollectionMember
from ddp_corpus.main import app
from ddp_corpus.models import Resource, new_id, utcnow
from test_collection_catalog import BASE, INTERNAL, body, internal, source


def alembic_head() -> str:
    """按迁移脚本现算 head，不写死版本号 —— 否则每次加迁移都变成假红。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "database/corpus/alembic.ini"))
    config.set_main_option("script_location", str(root / "database/corpus/alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture
async def catalog_pg(monkeypatch):
    dsn = os.getenv("COLLECTION_CATALOG_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("COLLECTION_CATALOG_TEST_DATABASE_URL must point to a migrated scratch PostgreSQL database")
    engine = create_async_engine(dsn, pool_size=16, max_overflow=16)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_sessionmaker", factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://corpus", trust_env=False) as client:
        yield client, factory
    await engine.dispose()


async def test_pg_migrated_constraints_concurrent_receipts_cas_and_revocation(catalog_pg):
    client, factory = catalog_pg
    org, owner = new_id(), new_id()
    async with factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) \
            == alembic_head()
        resource, version, _, document = await source(session, owner)
        resource.organization_id = document.organization_id = org
        await session.commit()
    headers = {**actor_headers(owner, organization_id=org), "Idempotency-Key": "create"}
    async def create():
        response = await client.post(BASE, json=body(version), headers=headers)
        assert response.status_code == 201, response.text
        return response.json()
    replies = await asyncio.gather(*(create() for _ in range(10)))
    assert len({r["collection_id"] for r in replies}) == 1
    cid = replies[0]["collection_id"]
    async with factory() as session:
        rows = list((await session.scalars(select(Collection).where(Collection.organization_id == org))).all())
        assert len(rows) == 1
        members = list((await session.scalars(select(CollectionMember).where(CollectionMember.collection_id == cid))).all())
        assert len(members) == 1 and members[0].version_id == version.id
    async def publish(i):
        return await client.post(f"{BASE}/{cid}/publish", json={"expected_revision": 1},
            headers={**headers, "Idempotency-Key": f"publish-{i}"})
    writes = await asyncio.gather(*(publish(i) for i in range(8)))
    assert sorted(r.status_code for r in writes) == [200]+[409]*7
    async def snapshot():
        response = await client.get(INTERNAL, params={"scope_id": "scope"}, headers=internal(owner, org))
        assert response.status_code == 200, response.text
        return response.json()
    snapshots = await asyncio.gather(*(snapshot() for _ in range(8)))
    assert len({s["registry_revision"] for s in snapshots}) == 1
    assert all(s["total"] == 1 for s in snapshots)
    async with factory() as session:
        await session.execute(update(Resource).where(Resource.id == resource.id).values(publication="private"))
        await session.commit()
    for snap in snapshots[:2]:
        denied = await client.get(INTERNAL, params={"scope_id": "scope", "snapshot_id": snap["snapshot_id"],
            "cursor": snap["terminal_cursor"]}, headers=internal(owner, org))
        assert denied.status_code == 410
        assert denied.json()["revoked_collection_ids"] == [cid]
    assert (await snapshot())["total"] == 0


async def test_pg_expired_snapshot_wrong_binding_is_invalid_not_expired(catalog_pg):
    """Ported reviewer counterexample on migrated PostgreSQL: binding beats expiry."""
    client, factory = catalog_pg
    org, owner = new_id(), new_id()
    async with factory() as session:
        resource, version, _, document = await source(session, owner)
        resource.organization_id = document.organization_id = org
        await session.commit()
    headers = {**actor_headers(owner, organization_id=org), "Idempotency-Key": "create"}
    created = await client.post(BASE, json=body(version), headers=headers)
    assert created.status_code == 201, created.text
    collection = created.json()
    published = await client.post(f"{BASE}/{collection['collection_id']}/publish", json={"expected_revision": 1},
        headers={**headers, "Idempotency-Key": "publish"})
    assert published.status_code == 200, published.text
    snapshot = await client.get(INTERNAL, params={"scope_id": "expired-binding"}, headers=internal(owner, org))
    assert snapshot.status_code == 200, snapshot.text
    first = snapshot.json()
    async with factory() as session:
        await session.execute(update(CollectionCatalogSnapshot).where(
            CollectionCatalogSnapshot.id == first["snapshot_id"]).values(valid_until=utcnow()-timedelta(seconds=1)))
        await session.commit()
    params = {"scope_id": "expired-binding", "snapshot_id": first["snapshot_id"]}
    wrong = await client.get(INTERNAL, params={**params, "cursor": "incorrect-cursor"}, headers=internal(owner, org))
    assert wrong.status_code == 410, wrong.text
    assert wrong.json()["error"]["code"] == "catalog_snapshot_invalid", wrong.text
    assert "revoked_collection_ids" not in wrong.json()
    expired = await client.get(INTERNAL, params={**params, "cursor": first["terminal_cursor"]}, headers=internal(owner, org))
    assert expired.status_code == 410, expired.text
    assert expired.json()["error"]["code"] == "catalog_snapshot_expired", expired.text
