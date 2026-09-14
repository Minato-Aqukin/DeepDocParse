"""Asset mutations; callers own transaction boundaries."""
import hashlib
import json

from sqlalchemy import func, select
from ddp_corpus import cache, wiki
from ddp_corpus.collection_models import CollectionMember
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, Resource, ResourceVersion, UploadEvent, new_id, utcnow


def scoped_upload_key(organization_id: str, key: str) -> str:
    return hashlib.sha256(json.dumps([organization_id, key]).encode()).hexdigest()


async def create_asset(session, *, document, actor_id: str, organization_id: str,
                       idempotency_key: str, filename: str, resource=None,
                       copied_from: str | None = None, request_payload: dict | None = None):
    document = await session.scalar(select(Document).where(Document.id == document.id)
        .with_for_update().execution_options(populate_existing=True))
    if document is None or document.deleted_at is not None:
        raise APIError(404, "document not found", "invalid_request_error", "document_not_found")
    if document.origin == "web" and not document.object_key:
        raise APIError(409, "original file is no longer available", "invalid_request_error", "source_missing")
    key = scoped_upload_key(organization_id, idempotency_key)
    payload = request_payload or {"document_id": document.id, "filename": filename,
                                 "resource_id": resource.id if resource else None,
                                 "copied_from": copied_from}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = await session.scalar(select(UploadEvent).where(
        UploadEvent.actor_id == actor_id, UploadEvent.idempotency_key == key))
    if previous:
        if previous.request_digest != digest:
            raise APIError(409, "idempotency key reused with different input",
                           "invalid_request_error", "idempotency_conflict")
        version = await session.get(ResourceVersion, previous.resource_version_id)
        return await session.get(Resource, version.resource_id), version, False
    if resource is None:
        resource = Resource(id=new_id(), organization_id=organization_id, owner_id=actor_id,
                            uploaded_by=actor_id, display_name=filename, copied_from=copied_from)
        session.add(resource)
        await session.flush()
    else:
        # Parent lock serializes version numbering on PostgreSQL.
        resource = await session.scalar(select(Resource).where(Resource.id == resource.id)
            .with_for_update().execution_options(populate_existing=True))
        if (resource is None or resource.deleted_at is not None or resource.owner_id != actor_id
                or resource.organization_id != organization_id):
            raise APIError(404, "resource not found", "invalid_request_error", "resource_not_found")
    existing = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == resource.id, ResourceVersion.document_id == document.id)
        .order_by(ResourceVersion.version_no.desc()).limit(1))
    if existing:
        raise APIError(409, "content already has a fixed version in this resource",
                       "invalid_request_error", "resource_version_exists")
    # Record metadata-only version dependencies before adding the new version. The
    # single-parent model deliberately rejects mixed lineage rather than replacing it.
    if copied_from:
        seen = {resource.id}
        ancestor_id = copied_from
        while ancestor_id:
            if ancestor_id in seen:
                raise APIError(409, "resource lineage would contain a cycle",
                               "invalid_request_error", "resource_lineage_cycle")
            seen.add(ancestor_id)
            ancestor = await session.scalar(select(Resource).where(Resource.id == ancestor_id)
                .execution_options(populate_existing=True))
            if ancestor is None or ancestor.deleted_at is not None:
                raise APIError(404, "source resource not found", "invalid_request_error", "resource_not_found")
            ancestor_id = ancestor.copied_from
        if resource.copied_from and resource.copied_from != copied_from:
            raise APIError(409, "multiple source parents require a new resource",
                           "invalid_request_error", "resource_lineage_conflict")
        resource.copied_from = copied_from
    number = (await session.scalar(select(func.max(ResourceVersion.version_no)).where(
        ResourceVersion.resource_id == resource.id)) or 0) + 1
    version = ResourceVersion(id=new_id(), resource_id=resource.id, version_no=number,
        document_id=document.id, source_digest=document.doc_id if document.origin == "web" else "", filename=filename,
        size_bytes=document.size_bytes)
    if hasattr(ResourceVersion, "parse_job_id"):
        version.parse_job_id = document.current_job_id
    session.add(version)
    await session.flush()
    session.add(UploadEvent(id=new_id(), resource_version_id=version.id, actor_id=actor_id,
                           idempotency_key=key, request_digest=digest))
    await session.flush()
    return resource, version, True


async def tombstone_resource(session, resource):
    from sqlalchemy import update
    document_ids = list((await session.execute(select(ResourceVersion.document_id).where(
        ResourceVersion.resource_id == resource.id))).scalars())
    version_ids = list((await session.execute(select(ResourceVersion.id).where(
        ResourceVersion.resource_id == resource.id))).scalars())
    await session.execute(select(Document.id).where(Document.id.in_(document_ids)).with_for_update())
    stamp = utcnow()
    resource.deleted_at = stamp
    resource.publication = "withdrawn"
    resource.updated_at = stamp
    await session.execute(update(ResourceVersion).where(
        ResourceVersion.resource_id == resource.id,
        ResourceVersion.deleted_at.is_(None)).values(deleted_at=stamp))

    await session.flush()
    for document_id in document_ids:
        live = await session.scalar(select(ResourceVersion.id).join(Resource).where(
            ResourceVersion.document_id == document_id, ResourceVersion.deleted_at.is_(None),
            Resource.deleted_at.is_(None)).limit(1))
        if live is None:
            await session.execute(update(Document).where(Document.id == document_id).values(
                deleted_at=stamp, index_status="none", index_generation=Document.index_generation + 1,
                index_lease_until=None))

    # P6：撤销/删除必须在同一个事务里做过期，缓存里撤回前的投影与依赖它的
    # Wiki 发布指针都不能活过这次提交。读路径仍会独立复核来源可用性
    # （`wiki.dependency_state` / 检索授权），这里清的是"当前指针"与缓存投影。
    await wiki.invalidate_dependents(session, resource_id=resource.id)
    await cache.invalidate(session, scope_key=cache.resource_scope(resource.id))
    for version_id in version_ids:
        await cache.invalidate(session, scope_key=cache.version_scope(version_id))
    collection_ids = list((await session.execute(select(CollectionMember.collection_id).where(
        CollectionMember.version_id.in_(version_ids)).distinct())).scalars())
    for collection_id in collection_ids:
        await cache.invalidate(session, scope_key=cache.collection_scope(collection_id))
