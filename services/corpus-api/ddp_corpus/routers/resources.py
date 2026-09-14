"""Owner-isolated resources over the existing deduplicated document store."""
from dataclasses import replace
from typing import Literal
import hashlib
import json
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.document_context import document_context
from ddp_corpus.errors import APIError
from ddp_corpus.models import Chunk, Document, ParseJob, Resource, ResourceVersion, UploadEvent
from ddp_corpus.policy import (
    document_resource_id, public_viewer, require_document, require_resource, resource_condition,
)
from ddp_corpus.resources import create_asset, scoped_upload_key, tombstone_resource

router = APIRouter()

class CreateResource(BaseModel):
    document_id: str
    display_name: str = Field(default="", max_length=255)
    copied_from: str | None = None

class UpdateResource(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    publication: Literal["private", "draft", "published", "withdrawn"] | None = None


def version_out(v):
    return {"id": v.id, "resource_id": v.resource_id, "version_no": v.version_no,
            "document_id": v.document_id, "source_digest": v.source_digest,
            "source_digest_verified": bool(v.source_digest),
            "filename": v.filename, "size_bytes": v.size_bytes,
            "parse_job_id": getattr(v, "parse_job_id", None), "created_at": v.created_at}

async def resource_out(session, row):
    versions = (await session.execute(select(ResourceVersion).where(
        ResourceVersion.resource_id == row.id, ResourceVersion.deleted_at.is_(None))
        .order_by(ResourceVersion.version_no))).scalars().all()
    return {"id": row.id, "organization_id": row.organization_id,
            "owner_id": row.owner_id, "uploader_ref": {"issuer": row.organization_id,
            "subject": row.uploaded_by}, "display_name": row.display_name,
            "publication": row.publication, "copied_from": row.copied_from,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "versions": [version_out(v) for v in versions]}

@router.get("")
async def list_resources(scope: Literal["mine", "site_public"] = "mine",
                         limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0),
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session)):
    # site_public 是**本组织**的公开目录（企业边界 8），不是跨租户目录。
    viewer = actor if scope == "mine" else public_viewer(actor.organization_id)
    stmt = select(Resource).where(resource_condition(viewer, write=scope == "mine"))
    if scope == "site_public":
        stmt = stmt.where(Resource.publication == "published", Resource.id.in_(
            select(ResourceVersion.resource_id).join(Document)
            .join(ParseJob, ParseJob.id == ResourceVersion.parse_job_id).where(
                ResourceVersion.deleted_at.is_(None), Document.deleted_at.is_(None),
                Document.origin == "web", ParseJob.index_status == "ready",
                Document.object_key != "", ParseJob.status == "succeeded",
                exists(select(Chunk.id).where(Chunk.parse_job_id == ParseJob.id).correlate(ParseJob)))))
    rows = list((await session.execute(stmt.order_by(Resource.created_at.desc(), Resource.id)
        .offset(offset).limit(limit + 1))).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [await resource_out(session, row) for row in rows],
            "offset": offset, "limit": limit, "has_more": more,
            "coverage": {"scope": scope, "complete": not more,
                         "watermark": max((r.updated_at.isoformat() for r in rows), default=None),
                         "snapshot_complete": False}}

async def _create(body, actor, session, key, resource=None):
    actor.require(actor.can_upload and actor.principal_id is not None, "创建资源")
    payload = {**body.model_dump(), "target_resource": resource.id if resource else None,
               "source_resource_context": actor.resource_id, "source_version_context": actor.version_id}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = await session.scalar(select(UploadEvent).where(
        UploadEvent.actor_id == actor.principal_id,
        UploadEvent.idempotency_key == scoped_upload_key(actor.organization_id, "api:" + key)))
    if previous:
        if previous.request_digest != digest:
            raise APIError(409, "idempotency key reused with different input",
                           "invalid_request_error", "idempotency_conflict")
        version = await session.get(ResourceVersion, previous.resource_version_id)
        row = await require_resource(session, actor, version.resource_id, write=True)
        return await resource_out(session, row)
    # The client may assert the source context, but never decide whether lineage exists.
    if body.copied_from and actor.resource_id and body.copied_from != actor.resource_id:
        raise APIError(409, "conflicting resource source contexts",
                       "invalid_request_error", "resource_context_conflict")
    source_id = body.copied_from or actor.resource_id
    document = await require_document(session, actor, body.document_id, resource_id=source_id)
    if source_id is None:
        source_id = await document_resource_id(session, actor, document.id)
    if source_id is None:
        raise APIError(409, "resource binding is required for metadata-only copies",
                       "invalid_request_error", "resource_context_required")
    await require_resource(session, actor, source_id)
    source = await document_context(session, replace(actor, resource_id=source_id), document)
    try:
        row, version, created = await create_asset(session, document=document, actor_id=actor.principal_id,
            organization_id=actor.organization_id, idempotency_key="api:" + key,
            filename=body.display_name or source.filename, resource=resource,
            copied_from=source_id, request_payload=payload)
        if created:
            version.parse_job_id = source.parse_job_id
        await session.commit()
    except APIError:
        await session.rollback()
        raise
    except IntegrityError:
        await session.rollback()
        raise APIError(409, "concurrent resource write; retry the same key",
                       "invalid_request_error", "resource_write_conflict")
    return await resource_out(session, row)

@router.post("", status_code=201)
async def create_resource(body: CreateResource, actor: Actor = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str = Header(min_length=1, max_length=128)):
    return await _create(body, actor, session, idempotency_key)

@router.get("/{resource_id}")
async def get_resource(resource_id: str, actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session)):
    return await resource_out(session, await require_resource(session, actor, resource_id))

@router.get("/{resource_id}/versions")
async def list_versions(resource_id: str, actor: Actor = Depends(current_actor),
                        session: AsyncSession = Depends(get_session)):
    row = await require_resource(session, actor, resource_id)
    return (await resource_out(session, row))["versions"]

@router.get("/{resource_id}/versions/{version_id}")
async def get_version(resource_id: str, version_id: str,
                      actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session)):
    await require_resource(session, actor, resource_id)
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.id == version_id, ResourceVersion.resource_id == resource_id,
        ResourceVersion.deleted_at.is_(None)))
    if version is None:
        raise APIError(404, "version not found", "invalid_request_error", "resource_version_not_found")
    return version_out(version)

@router.post("/{resource_id}/versions", status_code=201)
async def add_version(resource_id: str, body: CreateResource,
    actor: Actor = Depends(current_actor), session: AsyncSession = Depends(get_session),
    idempotency_key: str = Header(min_length=1, max_length=128)):
    row = await require_resource(session, actor, resource_id, write=True)
    return await _create(body, actor, session, idempotency_key, resource=row)

@router.patch("/{resource_id}")
async def update_resource(resource_id: str, body: UpdateResource,
    actor: Actor = Depends(current_actor), session: AsyncSession = Depends(get_session)):
    row = await require_resource(session, actor, resource_id, write=True)
    if body.publication == "published":
        versions = (await session.execute(select(ResourceVersion, Document, ParseJob).select_from(ResourceVersion)
            .join(Document, Document.id == ResourceVersion.document_id)
            .outerjoin(ParseJob, ParseJob.id == ResourceVersion.parse_job_id).where(
            ResourceVersion.resource_id == row.id, ResourceVersion.deleted_at.is_(None)))).all()
        jobs_with_chunks = set((await session.execute(select(Chunk.parse_job_id).where(
            Chunk.parse_job_id.in_([v.parse_job_id for v, _, _ in versions if v.parse_job_id])))).scalars())
        if not versions or any(d.origin != "web" or not d.object_key or d.deleted_at is not None
                               or job is None or job.index_status != "ready" or job.status != "succeeded"
                               or job.id not in jobs_with_chunks for _, d, job in versions):
            raise APIError(409, "only ready persistent resources can be published",
                           "invalid_request_error", "resource_not_publishable")
    if body.publication == "published" and row.copied_from:
        source = await session.scalar(select(Resource).where(
            Resource.id == row.copied_from,
            resource_condition(public_viewer(row.organization_id))))
        if source is None:
            raise APIError(403, "source does not permit publication", "permission_error",
                           "derived_publication_denied")
    for name, value in body.model_dump(exclude_none=True).items():
        setattr(row, name, value)
    await session.commit()
    return await resource_out(session, row)

@router.delete("/{resource_id}", status_code=204)
async def delete_resource(resource_id: str, actor: Actor = Depends(current_actor),
    session: AsyncSession = Depends(get_session)):
    await tombstone_resource(session, await require_resource(session, actor, resource_id, write=True))
    await session.commit()
