#!/usr/bin/env python3
"""Real PostgreSQL CAS smoke in a disposable random schema.

DDP_WIKI_TEST_DATABASE_URL must explicitly point to a scratch PostgreSQL database.
Creates only Wiki tables in its own schema and drops that schema on completion.
"""
import asyncio
import os
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import (
    Base, ClaimEvidenceBinding, DependencyManifest, Wiki, WikiHumanEdit, WikiPage,
    WikiRevision, WikiWriteKey,
)
from ddp_corpus.wiki import append_revision


async def main():
    url = os.environ.get("DDP_WIKI_TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Set DDP_WIKI_TEST_DATABASE_URL to an explicit scratch PostgreSQL URL")
    schema = "wiki_cas_" + uuid.uuid4().hex
    engine = create_async_engine(url, execution_options={"schema_translate_map": {None: schema}})
    tables = [model.__table__ for model in (
        Wiki, WikiRevision, WikiPage, DependencyManifest, ClaimEvidenceBinding, WikiHumanEdit, WikiWriteKey)]
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            wiki = Wiki(organization_id="pg-test", owner_id="alice", title="CAS test")
            session.add(wiki)
            await session.flush()
            base = WikiRevision(wiki_id=wiki.id, title=wiki.title, created_by="alice")
            session.add(base)
            await session.flush()
            wiki.current_revision_id = base.id
            await session.commit()
            wiki_id, base_id = wiki.id, base.id
        barrier = asyncio.Barrier(2)
        actor = Actor(id="alice", kind="user", organization_id="pg-test", role="contributor")

        async def contender(number):
            async with sessions() as session:
                wiki = await session.get(Wiki, wiki_id)
                assert wiki.current_revision_id == base_id
                await barrier.wait()
                try:
                    revision = await append_revision(session, actor, wiki, base_id,
                        kind="generated", title=f"winner-{number}", pages=[], deps=[], provider={},
                        limits={}, conflicts=[], key=f"write-{number}", request={"number": number})
                    await session.commit()
                    return ("saved", revision.id)
                except APIError as exc:
                    await session.rollback()
                    assert exc.status_code == 409 and exc.code == "revision_conflict"
                    return ("conflict", None)
        results = await asyncio.wait_for(asyncio.gather(contender(1), contender(2)), timeout=15)
        assert sorted(row[0] for row in results) == ["conflict", "saved"], results
        winner = next(row[1] for row in results if row[0] == "saved")
        async with sessions() as session:
            wiki = await session.get(Wiki, wiki_id)
            assert wiki.current_revision_id == winner
            assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 2
            assert await session.scalar(select(func.count()).select_from(WikiWriteKey)) == 1
        print("PASS: PostgreSQL simultaneous writers -> exactly one revision saved, one 409; loser rolled back")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
