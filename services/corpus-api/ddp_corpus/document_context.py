"""Presentation metadata and parse selection belong to an authorized asset."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion
from ddp_corpus.policy import document_resource_id, resource_condition


@dataclass(frozen=True)
class DocumentContext:
    resource_id: str | None
    version_id: str | None
    filename: str
    created_at: datetime
    parse_job_id: str | None


def presentation(document: Document, version: ResourceVersion | None) -> DocumentContext:
    if version is None:
        return DocumentContext(None, None, document.filename, document.created_at,
                               document.current_job_id)
    return DocumentContext(version.resource_id, version.id, version.filename,
                           version.created_at, version.parse_job_id)


async def document_context(session: AsyncSession, actor: Actor,
                           document: Document) -> DocumentContext:
    resource_id = await document_resource_id(session, actor, document.id)
    version = None
    if resource_id:
        stmt = select(ResourceVersion).join(Resource).where(
            ResourceVersion.document_id == document.id,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.deleted_at.is_(None), resource_condition(actor))
        if getattr(actor, "version_id", None):
            stmt = stmt.where(ResourceVersion.id == actor.version_id)
        version = await session.scalar(stmt.order_by(ResourceVersion.version_no.desc()).limit(1))
        if version is None:
            raise APIError(404, "resource version not found", "invalid_request_error", "version_not_found")
    return presentation(document, version)


def scoped_jobs(document_id: str, context: DocumentContext):
    condition = ParseJob.document_id == document_id
    if context.resource_id:
        condition &= or_(ParseJob.resource_id == context.resource_id,
                         ParseJob.id == context.parse_job_id)
    return condition


async def search_contexts(session: AsyncSession, actor: Actor,
                          document_id: str | None = None, *,
                          version_ids: list[str] | None = None) -> dict[str, list[DocumentContext]]:
    """Allowed fixed parse IDs, used before ranking as well as for output metadata."""
    from sqlalchemy import exists
    from ddp_corpus.policy import visible_document_condition
    stmt = select(Document, ResourceVersion).join(ResourceVersion).join(Resource).where(
        Document.deleted_at.is_(None), ResourceVersion.deleted_at.is_(None),
        ResourceVersion.parse_job_id.is_not(None), resource_condition(actor))
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    if actor.resource_id:
        stmt = stmt.where(Resource.id == actor.resource_id)
    if getattr(actor, "version_id", None):
        stmt = stmt.where(ResourceVersion.id == actor.version_id)
    if version_ids is not None:
        stmt = stmt.where(ResourceVersion.id.in_(version_ids))
    contexts: dict[str, list[DocumentContext]] = {}
    for document, version in (await session.execute(stmt)).all():
        contexts.setdefault(version.parse_job_id, []).append(presentation(document, version))
    # Compatibility only for unmapped owner-bound rows, never as fallback for a
    # migrated asset whose frozen parse is missing or revoked.
    legacy = select(Document, ParseJob).join(ParseJob, ParseJob.document_id == Document.id).where(
        visible_document_condition(actor), ~exists(select(ResourceVersion.id).where(
            ResourceVersion.document_id == Document.id).correlate(Document)))
    if document_id:
        legacy = legacy.where(Document.id == document_id)
    if version_ids is None and not actor.resource_id and not getattr(actor, "version_id", None):
        for document, job in (await session.execute(legacy)).all():
            contexts[job.id] = [presentation(document, None)]
    return contexts
