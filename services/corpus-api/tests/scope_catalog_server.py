"""Real migrated-PG catalog HTTP fixture; no production lifespan or mocked catalog.

SCOPE_CORPUS_DATABASE_URL and SERVICE_TOKEN are mandatory. The parent integration test
provides a dedicated scratch database and its random organization/owner, then consumes the
single JSON fixture line before connecting to the selected loopback port.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from ddp_corpus import catalog, db
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import install_error_handlers
from ddp_corpus.models import Chunk, Document, ParseJob, Resource, ResourceVersion, new_id
from ddp_corpus.routers.collections import router


async def seed(organization, owner):
    actor = Actor(id=owner, kind="user", organization_id=organization, role="contributor")
    collections, resources = [], []
    async with db.get_sessionmaker()() as session:
        for index in range(3):
            doc = Document(id=new_id(), uploaded_by=owner, organization_id=organization,
                doc_id=new_id()*2, origin="web", filename="fixture.pdf", mime="application/pdf",
                size_bytes=100, object_key="catalog-integration/fixture.pdf")
            session.add(doc)
            await session.flush()
            resource = Resource(id=new_id(), organization_id=organization, owner_id=owner,
                uploaded_by=owner, display_name="Private decoy" if index == 2 else "Public fixture",
                publication="private" if index == 2 else "published")
            session.add(resource)
            await session.flush()
            job = ParseJob(id=new_id(), document_id=doc.id, resource_id=resource.id,
                engine="borndigital", options_hash=new_id(), document_version=1,
                initiated_by=owner, status="succeeded", index_status="ready", index_generation=1)
            session.add(job)
            await session.flush()
            version = ResourceVersion(id=new_id(), resource_id=resource.id, document_id=doc.id,
                version_no=1, parse_job_id=job.id, source_digest=doc.doc_id, filename="fixture.pdf", size_bytes=100)
            session.add_all([version, Chunk(document_id=doc.id, parse_job_id=job.id, seq=0,
                text="PRIVATE DECOY" if index == 2 else "Fixture evidence", char_len=16)])
            await session.flush()
            created = await catalog.mutate(session, actor, "create", {
                "name": "Private decoy" if index == 2 else f"Public collection {index+1}",
                "licence": "CC0", "languages": ["en"], "topics": ["private"] if index == 2 else ["manuals"],
                "version_ids": [version.id]}, "fixture-create-"+str(index))
            if index < 2:
                published = await catalog.mutate(session, actor, "publish",
                    {"expected_revision": created["revision"]}, "fixture-publish-"+str(index), created["collection_id"])
                collections.append(published["collection_id"])
                resources.append(resource.id)
            else:
                private_id = created["collection_id"]
    return {"organization_id": organization, "owner_id": owner, "public_collection_ids": collections,
            "public_resource_ids": resources, "private_collection_id": private_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--fixture-file")
    args = parser.parse_args()
    dsn = os.environ.get("SCOPE_CORPUS_DATABASE_URL", "")
    if not dsn or not os.environ.get("SERVICE_TOKEN"):
        parser.error("SCOPE_CORPUS_DATABASE_URL and SERVICE_TOKEN are required")
    settings.database_url = dsn
    settings.service_token = os.environ["SERVICE_TOKEN"]
    db.reset_engine()
    async def prepare():
        fixture = await seed(args.organization, args.owner)
        await db.get_engine().dispose()
        db.reset_engine()
        return fixture
    fixture = asyncio.run(prepare())
    if args.fixture_file:
        Path(args.fixture_file).write_text(json.dumps(fixture), encoding="utf-8")
    print(json.dumps(fixture), flush=True)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
