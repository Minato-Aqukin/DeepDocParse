"""Resource authorization shared by HTTP, generation, evidence and bundle adapters."""
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, DocumentUpload, Resource, ResourceVersion


def resource_condition(actor: Actor, *, write: bool = False):
    owner = and_(Resource.organization_id == actor.organization_id,
                 Resource.owner_id == actor.principal_id, actor.principal_id is not None)
    # Reachable publication roots exclude withdrawn ancestry, missing origins and cycles.
    # A copied resource cannot reopen publication after an ancestor revokes it.
    public = select(Resource.id.label("id")).where(
        Resource.publication == "published", Resource.deleted_at.is_(None),
        Resource.copied_from.is_(None)).correlate(None).cte(recursive=True)
    child = aliased(Resource)
    public = public.union_all(select(child.id).join(public, child.copied_from == public.c.id)
        .where(child.publication == "published", child.deleted_at.is_(None)))
    permitted = owner if write else or_(owner, Resource.id.in_(select(public.c.id)))
    return and_(Resource.deleted_at.is_(None),
                Resource.publication.in_(("private", "draft", "published", "withdrawn")), permitted)


def visible_document_condition(actor: Actor):
    """SQL predicate; apply before ranking/limiting, never filter a sampled candidate list."""
    mapped = exists(select(ResourceVersion.id).where(
        ResourceVersion.document_id == Document.id).correlate(Document))
    authorized = exists(select(ResourceVersion.id).join(
        Resource, Resource.id == ResourceVersion.resource_id).where(
            ResourceVersion.document_id == Document.id,
            ResourceVersion.deleted_at.is_(None), resource_condition(actor)).correlate(Document))
    legacy_owner = and_(Document.organization_id == actor.organization_id,
        actor.principal_id is not None,
        or_(Document.uploaded_by == actor.principal_id, exists(select(DocumentUpload.id).where(
            DocumentUpload.document_id == Document.id, DocumentUpload.user_id == actor.principal_id).correlate(Document))))
    return and_(Document.deleted_at.is_(None), or_(authorized, and_(~mapped, legacy_owner)))


async def require_resource(session: AsyncSession, actor: Actor, resource_id: str,
                           *, write: bool = False) -> Resource:
    row = await session.scalar(select(Resource).where(
        Resource.id == resource_id, resource_condition(actor, write=write))
        .execution_options(populate_existing=True))
    if row is None:
        raise APIError(404, "resource not found", "invalid_request_error", "resource_not_found")
    return row


async def require_document(session: AsyncSession, actor: Actor, document_id: str,
                           *, resource_id: str | None = None) -> Document:
    resource_id = resource_id or actor.resource_id
    row = await session.scalar(select(Document).where(
        Document.id == document_id, visible_document_condition(actor))
        .execution_options(populate_existing=True))
    if row is None:
        raise APIError(404, "document not found", "invalid_request_error", "document_not_found")
    bindings = list((await session.execute(select(Resource.id).join(
        ResourceVersion, ResourceVersion.resource_id == Resource.id).where(
            ResourceVersion.document_id == document_id,
            ResourceVersion.deleted_at.is_(None), resource_condition(actor)).distinct()
    )).scalars())
    if resource_id and resource_id not in bindings:
        raise APIError(404, "document not found", "invalid_request_error", "document_not_found")
    if not resource_id and len(bindings) > 1:
        raise APIError(409, "resource_id is required for this legacy document",
                       "invalid_request_error", "resource_context_required")
    return row


async def authorized_document_ids(session: AsyncSession, actor: Actor) -> list[str]:
    return list((await session.execute(select(Document.id).where(
        visible_document_condition(actor)))).scalars())


async def document_resource_id(session: AsyncSession, actor: Actor, document_id: str) -> str | None:
    await require_document(session, actor, document_id)
    if actor.resource_id:
        return actor.resource_id
    return await session.scalar(select(Resource.id).join(ResourceVersion).where(
        ResourceVersion.document_id == document_id, ResourceVersion.deleted_at.is_(None),
        resource_condition(actor)).distinct())


async def require_mutable_document(session: AsyncSession, actor: Actor, document_id: str) -> Document:
    """Legacy shared state may change only for one owner-bound, unshared asset.

    The document lock serializes this decision with asset registration and GC.
    Public retrieval authorization cannot authorize changing every owner's current index.
    """
    await require_document(session, actor, document_id)
    document = await session.scalar(select(Document).where(Document.id == document_id)
        .with_for_update().execution_options(populate_existing=True))
    resource_id = await document_resource_id(session, actor, document_id)
    if resource_id is None:
        raise APIError(409, "resource binding is required before changing legacy document state",
                       "invalid_request_error", "resource_context_required")
    await require_resource(session, actor, resource_id, write=True)
    other_asset = await session.scalar(select(Resource.id).join(ResourceVersion).where(
        ResourceVersion.document_id == document_id, ResourceVersion.deleted_at.is_(None),
        Resource.deleted_at.is_(None), Resource.id != resource_id).limit(1))
    if other_asset is not None:
        raise APIError(409, "shared content requires a resource-specific execution",
                       "invalid_request_error", "shared_document_write_unsupported")
    return document


async def require_history_document(session: AsyncSession, actor: Actor, document_id: str,
                                   *, resource_id: str | None) -> Document:
    """A migrated document does not supply the missing provenance of an old result."""
    if resource_id is None and await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.document_id == document_id).limit(1)):
        raise APIError(404, "historical resource context is unavailable",
                       "invalid_request_error", "historical_resource_context_missing")
    # Caller query parameters cannot retroactively repair a historical source binding.
    from dataclasses import replace
    return await require_document(session, replace(actor, resource_id=None, version_id=None), document_id,
                                  resource_id=resource_id)


async def require_document_parse(session: AsyncSession, actor: Actor, document_id: str,
                                 parse_job_id: str) -> Document:
    """A readable content hash does not authorize another asset's parse artifacts."""
    from ddp_corpus.document_context import search_contexts
    document = await require_document(session, actor, document_id)
    if parse_job_id not in await search_contexts(session, actor, document_id):
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    return document
