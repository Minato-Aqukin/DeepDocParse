#!/usr/bin/env python3
"""Exercise independent ParseJob claims/leases on real PostgreSQL in an isolated schema."""
import asyncio
import os
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ddp_corpus.indexing import _fail_if_current, _renew_lease_once, claim_for_indexing
from ddp_corpus.models import Base, Document, ParseJob, utcnow


async def main():
    url = os.environ.get("DDP_INDEX_TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Set DDP_INDEX_TEST_DATABASE_URL to an explicit scratch PostgreSQL URL")
    schema = "parse_index_" + uuid.uuid4().hex
    engine = create_async_engine(url, execution_options={"schema_translate_map": {None: schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync,
                tables=[Document.__table__, ParseJob.__table__]))
        async with sessions() as session:
            doc = Document(uploaded_by="alice", organization_id="org-a", doc_id="a" * 64, filename="same.pdf")
            session.add(doc)
            await session.flush()
            a = ParseJob(document_id=doc.id, resource_id="asset-a", engine="borndigital", options_hash="a",
                         document_version=1, index_status="pending")
            b = ParseJob(document_id=doc.id, resource_id="asset-b", engine="borndigital", options_hash="b",
                         document_version=2, index_status="pending")
            session.add_all([a, b])
            await session.flush()
            doc.current_job_id = a.id
            await session.commit()
            doc_id, a_id, b_id = doc.id, a.id, b.id
        gate = asyncio.Barrier(3)
        async def claim(job_id):
            async with sessions() as session:
                await gate.wait()
                return await claim_for_indexing(session, doc_id, job_id=job_id)
        claims = await asyncio.wait_for(asyncio.gather(claim(a_id), claim(a_id), claim(b_id)), 15)
        assert sorted(value for value in claims[:2] if value is not None) == [1], claims
        assert claims[2] == 1, claims
        async with sessions() as session:
            a = await session.get(ParseJob, a_id)
            a.index_lease_until = utcnow() - timedelta(seconds=1)
            await session.commit()
        async with sessions() as session:
            assert await claim_for_indexing(session, doc_id, job_id=a_id) == 2
        assert not await _renew_lease_once(sessions, a_id, 1)
        assert await _renew_lease_once(sessions, b_id, 1)
        async with sessions() as session:
            await _fail_if_current(session, doc_id, a_id, 1, "late worker must lose")
        async with sessions() as session:
            a, b = await session.get(ParseJob, a_id), await session.get(ParseJob, b_id)
            assert a.index_generation == 2 and a.index_status == "indexing" and a.index_error is None
            assert b.index_generation == 1 and b.index_status == "indexing" and b.index_error is None
        print("PASS: concurrent duplicate A claim has one winner; B claims independently; expired A reclaimed;")
        print("PASS: stale A heartbeat/failure rejected while B lease and generation remain unchanged")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
