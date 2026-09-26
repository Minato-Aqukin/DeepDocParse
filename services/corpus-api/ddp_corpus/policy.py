"""Resource authorization shared by HTTP, generation, evidence and bundle adapters."""
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, DocumentUpload, Resource, ResourceVersion


def public_viewer(organization_id: str) -> Actor:
    """An observer that owns nothing: only the published path of `resource_condition` remains.

    Used to ask "is this published inside that organization?" (catalog pins, derived
    publication checks, the site_public listing). The organization is required, never
    blank: publication is organization-scoped (enterprise boundary 8).
    """
    return Actor(id="", kind="service", organization_id=organization_id, role="viewer")


PUBLICATIONS = ("private", "draft", "published", "withdrawn")


def resource_condition(actor: Actor, *, write: bool = False):
    def owned(table):
        return and_(table.owner_id == actor.principal_id, actor.principal_id is not None)

    live = and_(Resource.deleted_at.is_(None), Resource.publication.in_(PUBLICATIONS),
                Resource.organization_id == actor.organization_id)
    if write:
        # Writes stay with the owner even after a source ancestor is revoked: the owner must
        # still be able to rename, withdraw or delete what they hold. Reads below do not.
        return and_(live, owned(Resource))
    # Readable set, built top-down from lineage roots: live, in the caller's organization,
    # owned by the caller or published, and the `copied_from` parent readable too. The
    # published-only path is the public closure (withdrawn ancestry, missing origins and cycles
    # never grant access). The owner path stops at a revoked or deleted foreign ancestor as
    # well: a metadata copy made while the source was public kept the withdrawn source's
    # evidence, crops and original readable to the copier (2026-09-24, phase E). Local parents
    # are tombstoned, never removed, so a parent with no local row at all is a placeholder
    # (a bundle's `remote:` origin): it keeps publication closed but is the owner's own root;
    # its revocation belongs to the replica ledger, not to this predicate.
    # Publication and lineage are organization-scoped (enterprise boundary 8): a single-
    # organization deployment sees no difference; without it a multi-organization deployment
    # would serve one tenant's published chunks, evidence and bundles to every other tenant.
    root, parent = aliased(Resource), aliased(Resource)
    placeholder_parent = and_(root.copied_from.is_not(None),
                              ~exists(select(parent.id).where(parent.id == root.copied_from)))
    readable = select(root.id.label("id")).where(
        root.deleted_at.is_(None), root.organization_id == actor.organization_id,
        or_(and_(root.copied_from.is_(None), or_(root.publication == "published", owned(root))),
            and_(placeholder_parent, owned(root)))).correlate(None).cte(recursive=True)
    child = aliased(Resource)
    readable = readable.union_all(select(child.id).join(readable, child.copied_from == readable.c.id).where(
        child.deleted_at.is_(None), child.organization_id == actor.organization_id,
        or_(child.publication == "published", owned(child))))
    return and_(live, Resource.id.in_(select(readable.c.id)))


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
