"""Opt-in real PostgreSQL HTTP/race tests. Only a migrated dedicated scratch database."""
import asyncio
import hashlib
import os
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import actor_headers
from ddp_corpus import db
from ddp_corpus.client_models import ClientPage, ClientSnapshot, ClientView
from ddp_corpus.main import app
from ddp_corpus.models import Resource, new_id, utcnow
from ddp_corpus.routers import client as client_router
from test_client_projection import asset


@pytest.fixture
async def pg_client(monkeypatch):
    dsn = os.getenv("CENTER_CLIENT_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("CENTER_CLIENT_TEST_DATABASE_URL must point to a migrated scratch PostgreSQL database")
    engine = create_async_engine(dsn, pool_size=16, max_overflow=16)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_sessionmaker", factory)
    async def unknown(_):
        return {"capabilities":[], "capability_status":"unknown"}
    monkeypatch.setattr(client_router, "capabilities", unknown)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://corpus", trust_env=False) as client:
        yield client, factory
    await engine.dispose()


def scope_headers(subject, org):
    return {**actor_headers(subject, organization_id=org), "X-DDP-Client-Scope":"sha256:"+hashlib.sha256(subject.encode()).hexdigest()}


async def test_pg_concurrent_snapshots_events_and_permission_revoke(pg_client):
    client, factory = pg_client
    org, alice, bob = new_id(), new_id(), new_id()
    a, b = scope_headers(alice, org), scope_headers(bob, org)
    async with factory() as session:
        resource, version, job, document = await asset(session, alice, publication="published")
        resource.organization_id = document.organization_id = org
        await session.commit()
    async def snap(headers):
        result = await client.get("/api/v1/client/snapshot", headers=headers)
        assert result.status_code == 200, result.text
        return result.json()
    # Twelve independent transactions must all commit the same observed view revision.
    initial = await asyncio.gather(*(snap(a) for _ in range(12)))
    assert len({(row["cursor"],row["sequence"]) for row in initial}) == 1
    alice_head = initial[0]
    bob_head = await snap(b)
    async with factory() as session:
        await session.execute(update(Resource).where(Resource.id == resource.id).values(publication="withdrawn"))
        await session.commit()
    # Authorization is recomputed for each event, not copied from the cursor's old projection.
    replies = await asyncio.gather(*(client.get("/api/v1/client/events", params={"after":bob_head["cursor"]}, headers=b) for _ in range(8)))
    frames = []
    for reply in replies:
        assert reply.status_code == 200, reply.text
        frames.append(reply.json()["events"][0])
    assert len({(frame["cursor"],frame["sequence"]) for frame in frames}) == 1
    assert frames[0]["sequence"] == bob_head["sequence"]+1
    assert frames[0]["previous_sequence"] == bob_head["sequence"]
    assert resource.id not in str(frames[0]["state"])
    assert (await client.get("/api/v1/client/events", params={"after":alice_head["cursor"]}, headers=b)).status_code == 410
    async with factory() as session:
        row = await session.get(ClientSnapshot, frames[0]["cursor"])
        row.expires_at = utcnow()-timedelta(seconds=1)
        await session.commit()
    assert (await client.get("/api/v1/client/events", params={"after":frames[0]["cursor"]}, headers=b)).status_code == 410
    fresh = await snap(b)
    assert fresh["sequence"] > frames[0]["sequence"]
    # Dedicated scope cleanup; the resource remains a harmless unique test fixture.
    async with factory() as session:
        scopes = (await session.execute(select(ClientSnapshot.scope).where(ClientSnapshot.id.in_(
            [alice_head["cursor"], bob_head["cursor"], fresh["cursor"]])))).scalars().all()
        snapshots = select(ClientSnapshot.id).where(ClientSnapshot.scope.in_(scopes))
        await session.execute(delete(ClientPage).where(ClientPage.snapshot_id.in_(snapshots)))
        await session.execute(delete(ClientSnapshot).where(ClientSnapshot.scope.in_(scopes)))
        await session.execute(delete(ClientView).where(ClientView.scope.in_(scopes)))
        await session.execute(update(Resource).where(Resource.id == resource.id).values(deleted_at=utcnow()))
        await session.commit()
