#!/usr/bin/env python3
"""Verify GC/reference fencing on a disposable PostgreSQL schema.

DDP_GC_TEST_DATABASE_URL must point to an explicitly selected test database.
Creates/drops only a fresh schema with a generated name; never reads user objects.
Object storage is an in-memory fault gate, not a claim of MinIO integration testing.
"""

import asyncio
from datetime import timedelta
import json
import os
import platform
import uuid

import sqlalchemy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from ddp_corpus.config import settings
from ddp_corpus.gc import collect_deleted_objects
from ddp_corpus.models import Base, Document, Resource, ResourceVersion, new_id, utcnow
from ddp_corpus.storage import MemoryStorage


class DeleteGate(MemoryStorage):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def delete(self, key):
        self.entered.set()
        await self.release.wait()
        await super().delete(key)


async def verify(url):
    schema = "bundle_gc_" + uuid.uuid4().hex
    engine = create_async_engine(url, execution_options={"schema_translate_map": {None: schema}})
    created = False
    checks = {}
    try:
        async with engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
            created = True
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        storage = DeleteGate()
        stamp = utcnow() - timedelta(seconds=settings.gc_grace_seconds + 10)
        async with factory() as session:
            document = Document(
                id=new_id(),
                uploaded_by="fixture-a",
                organization_id="fixture-org",
                doc_id="a" * 64,
                filename="fixture.pdf",
                size_bytes=5,
                object_key="sources/original.pdf",
                deleted_at=stamp,
            )
            session.add(document)
            await session.flush()
            first = Resource(
                id=new_id(),
                owner_id="fixture-a",
                uploaded_by="fixture-a",
                organization_id="fixture-org",
                deleted_at=stamp,
            )
            second = Resource(
                id=new_id(),
                owner_id="fixture-b",
                uploaded_by="fixture-b",
                organization_id="fixture-org",
            )
            session.add_all([first, second])
            await session.flush()
            first_version = ResourceVersion(
                resource_id=first.id,
                document_id=document.id,
                source_digest=document.doc_id,
                deleted_at=stamp,
            )
            second_version = ResourceVersion(
                resource_id=second.id, document_id=document.id, source_digest=document.doc_id
            )
            session.add_all([first_version, second_version])
            await session.commit()
        await storage.put(document.object_key, b"bytes", "application/pdf")
        assert await collect_deleted_objects(factory, storage) == 0
        assert await storage.exists(document.object_key)
        checks["live_shared_version_prevents_gc"] = "PASS"
        async with factory() as session:
            current = await session.get(Resource, second.id)
            current.deleted_at = stamp
            version = await session.get(ResourceVersion, second_version.id)
            version.deleted_at = stamp
            await session.commit()

        gc_task = asyncio.create_task(collect_deleted_objects(factory, storage))
        await asyncio.wait_for(storage.entered.wait(), 5)
        writer_started = asyncio.Event()

        async def writer():
            async with factory() as session:
                writer_started.set()
                current = await session.scalar(
                    select(Document).where(Document.id == document.id).with_for_update()
                )
                assert current.object_key == "", "writer must see committed GC state"
                await storage.put("sources/reuploaded.pdf", b"bytes", "application/pdf")
                current.object_key = "sources/reuploaded.pdf"
                current.deleted_at = None
                resource = Resource(
                    id=new_id(),
                    owner_id="fixture-c",
                    uploaded_by="fixture-c",
                    organization_id="fixture-org",
                )
                session.add(resource)
                await session.flush()
                session.add(
                    ResourceVersion(
                        resource_id=resource.id,
                        document_id=current.id,
                        source_digest=current.doc_id,
                    )
                )
                await session.commit()

        writer_task = asyncio.create_task(writer())
        await writer_started.wait()
        try:
            await asyncio.wait_for(asyncio.shield(writer_task), 0.15)
        except TimeoutError:
            checks["reference_writer_waits_through_object_deletion"] = "PASS"
        else:
            raise AssertionError("writer bypassed document row lock")
        storage.release.set()
        assert await asyncio.wait_for(gc_task, 5) == 1
        await asyncio.wait_for(writer_task, 5)
        assert await storage.exists("sources/reuploaded.pdf")
        assert not await storage.exists("sources/original.pdf")
        assert await collect_deleted_objects(factory, storage) == 0
        checks["reupload_rebinds_verified_new_object_after_gc"] = "PASS"
        return {
            "checks": checks,
            "database": "PostgreSQL",
            "storage": "MemoryStorage delete gate",
            "python": platform.python_version(),
            "sqlalchemy": sqlalchemy.__version__,
            "platform": platform.system() + " " + platform.machine(),
        }
    finally:
        if created:
            async with engine.begin() as conn:
                await conn.execute(DropSchema(schema, cascade=True))
        await engine.dispose()


if __name__ == "__main__":
    selected_url = os.environ.get("DDP_GC_TEST_DATABASE_URL")
    if not selected_url:
        raise SystemExit("Set DDP_GC_TEST_DATABASE_URL to an explicitly selected test database")
    print(json.dumps(asyncio.run(verify(selected_url)), ensure_ascii=False, indent=2))
