"""Explicit publication; metadata-only fixed directories with live authorization checks."""
from datetime import timedelta
import hashlib
import json

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.exc import IntegrityError

from ddp_corpus.collection_models import (Collection, CollectionCatalogPage,
    CollectionCatalogSnapshot, CollectionCatalogView, CollectionMember, CollectionReceipt)
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.models import Chunk, Document, ParseJob, Resource, ResourceVersion, as_aware, new_id, utcnow
from ddp_corpus.policy import resource_condition

PUBLIC = Actor(id="", kind="service", organization_id="", role="viewer")
MAX_COLLECTIONS = 10000
MAX_SNAPSHOTS = 32
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
#: Peer directory reads are node-scoped, not caller-scoped. The scope and caller
#: digest are fixed server-side so a peer can never choose whose view it reads.
PEER_DIRECTORY_SCOPE = "peer-directory"
PEER_DIRECTORY_CALLER = "sha256:" + hashlib.sha256(b"ddp-peer-directory").hexdigest()


def error(code="collection_not_found", status=404):
    return APIError(status, code.replace("_", " "), "invalid_request_error", code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def actor_binding(actor):
    return [actor.organization_id, actor.kind, actor.id, actor.principal_id, actor.role]


async def lock_key(session, key):
    # Serialize receipt/view creation across processes, including rows that do not exist yet.
    if session.bind.dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def manageable(actor, collection):
    return (actor.principal_id is not None and actor.organization_id == collection.organization_id
            and (actor.principal_id == collection.owner_id or actor.can_manage))


async def require_collection(session, actor, collection_id, *, write=False):
    row = await session.scalar(select(Collection).where(Collection.id == collection_id,
        Collection.organization_id == actor.organization_id).execution_options(populate_existing=True))
    if row is None or (write and not manageable(actor, row)):
        raise error()
    if not manageable(actor, row) and row.publication != "published":
        raise error()
    return row


async def members_state(session, row, actor, *, public=False):
    """Pins are revalidated, including copied-source ancestry, before any metadata escapes."""
    members = list((await session.scalars(select(CollectionMember).where(
        CollectionMember.collection_id == row.id).order_by(CollectionMember.version_id))).all())
    if not members:
        raise error()
    versions = (await session.execute(select(ResourceVersion, Document, ParseJob, Resource,
        exists(select(Chunk.id).where(Chunk.parse_job_id == ParseJob.id).correlate(ParseJob)))
        .select_from(ResourceVersion).join(Resource, Resource.id == ResourceVersion.resource_id)
        .join(Document, Document.id == ResourceVersion.document_id)
        .outerjoin(ParseJob, ParseJob.id == ResourceVersion.parse_job_id).where(
            ResourceVersion.id.in_([m.version_id for m in members]),
            ResourceVersion.deleted_at.is_(None), Document.deleted_at.is_(None),
            resource_condition(PUBLIC if public else actor))
        .execution_options(populate_existing=True))).all()
    allowed = {v.id: (v, d, p, resource, chunks) for v, d, p, resource, chunks in versions}
    parts, ready = [], True
    for member in members:
        values = allowed.get(member.version_id)
        if values is None:
            raise error()
        version, document, job, resource, chunks = values
        if (version.resource_id, version.document_id, version.parse_job_id, version.source_digest) != (
                member.resource_id, member.document_id, member.parse_job_id, member.source_digest):
            raise error()
        available = bool(job and job.document_id == document.id and job.status == "succeeded"
            and job.index_status == "ready" and chunks and document.origin == "web"
            and document.object_key and not version.bundle_prefix and len(version.source_digest) == 64
            and version.source_digest == document.doc_id
            and all(char in "0123456789abcdef" for char in version.source_digest))
        ready = ready and available
        # Retain source publication epochs: withdraw then republish must not revive an
        # older scope silently, even when no observer queried during the private interval.
        ancestry, seen, ancestor = [], set(), resource
        while ancestor is not None:
            if ancestor.id in seen or len(seen) >= 100:
                raise error()
            seen.add(ancestor.id)
            ancestry.append([ancestor.id, ancestor.publication,
                as_aware(ancestor.updated_at).isoformat(), bool(ancestor.deleted_at)])
            parent = ancestor.copied_from
            ancestor = await session.scalar(select(Resource).where(Resource.id == parent)
                .execution_options(populate_existing=True)) if parent else None
            if parent and ancestor is None:
                raise error()
        parts.append([member.version_id, member.parse_job_id, member.source_digest,
            job.index_generation if job else None, job.index_status if job else None,
            job.status if job else None, job.compile_fingerprint if job else None, available, ancestry])
    return members, "sha256:"+digest(parts), "ready" if ready else "unavailable"


async def collection_output(session, actor, row, *, operation_revision=None):
    public = not manageable(actor, row)
    members, index_revision, readiness = await members_state(session, row, actor, public=public)
    result = {"collection_id": row.id, "organization_id": row.organization_id,
        "owner_id": row.owner_id, "name": row.name, **row.metadata_json,
        "publication": row.publication, "revision": row.revision,
        "version_ids": [m.version_id for m in members], "index_revision": index_revision,
        "index_readiness": readiness, "created_at": row.created_at, "updated_at": row.updated_at}
    if operation_revision is not None:
        result["operation_revision"] = operation_revision
    return result


def cache_revision(row, index_revision: str) -> str:
    """集合投影的缓存有效性指纹：集合修订 + 发布状态 + 成员索引修订。

    复用本模块的 `digest`（与快照 `fingerprint`、成员状态同一份规范 JSON
    摘要）—— **不要另写一份指纹实现**：两份实现会让"集合已经变了"与
    "缓存键没变"各说各话。`index_revision` 由 `members_state` 给出，
    已经覆盖固定成员、parse revision 与索引世代；这里再绑住集合自身的
    revision/publication，让重命名、替换成员、撤回都改变指纹。

    撤回的集合不进指纹路径：它必须被显式失效（见 `mutate` 的 withdraw 分支），
    而不是靠"下一版指纹不同"来碰运气。
    """
    return "sha256:" + digest([row.id, row.revision, row.publication, index_revision])


async def pin_members(session, actor, collection_id, version_ids):
    versions = list((await session.scalars(select(ResourceVersion).join(Resource).join(Document,
        Document.id == ResourceVersion.document_id).where(ResourceVersion.id.in_(version_ids),
            ResourceVersion.deleted_at.is_(None), Document.deleted_at.is_(None),
            resource_condition(actor)))).all())
    if len(versions) != len(version_ids):
        raise error("collection_source_not_found", 404)
    await session.execute(delete(CollectionMember).where(CollectionMember.collection_id == collection_id))
    session.add_all([CollectionMember(collection_id=collection_id, version_id=v.id,
        resource_id=v.resource_id, document_id=v.document_id, parse_job_id=v.parse_job_id,
        source_digest=v.source_digest) for v in versions])
    await session.flush()


async def mutate(session, actor, operation, body, key, collection_id=None):
    actor.require(actor.principal_id is not None and actor.kind in ("user", "api_key"), "管理集合")
    if operation == "create":
        actor.require(actor.can_upload, "创建集合")
    key_hash = digest([actor_binding(actor)[:4], operation, collection_id, key])
    request_digest = digest(body)
    await lock_key(session, "collection-write:"+key_hash)
    previous = await session.get(CollectionReceipt, key_hash)
    if previous:
        if previous.request_digest != request_digest:
            raise error("idempotency_conflict", 409)
        row = await require_collection(session, actor, previous.collection_id, write=True)
        # Withdraw replay need not regain a source's permissions merely to report the receipt.
        if operation == "withdraw":
            return {"collection_id": row.id, "revision": row.revision,
                    "publication": row.publication, "operation_revision": previous.revision}
        return await collection_output(session, actor, row, operation_revision=previous.revision)
    now = utcnow()
    if operation == "create":
        row = Collection(id=new_id(), organization_id=actor.organization_id, owner_id=actor.principal_id,
            name=body["name"], metadata_json={k: body[k] for k in ("licence", "languages", "topics", "time_range") if body.get(k) is not None},
            publication="draft", revision=1, created_at=now, updated_at=now)
        session.add(row)
        await session.flush()
        await pin_members(session, actor, row.id, body["version_ids"])
    else:
        row = await require_collection(session, actor, collection_id, write=True)
        # Lock the existing object before checking sources; CAS also fences SQLite races.
        await session.scalar(select(Collection).where(Collection.id == row.id).with_for_update()
                             .execution_options(populate_existing=True))
        if row.revision != body["expected_revision"]:
            raise error("collection_revision_conflict", 409)
        values = {"revision": row.revision+1, "updated_at": now}
        if operation == "replace":
            values.update(name=body["name"], metadata_json={k: body[k] for k in
                ("licence", "languages", "topics", "time_range") if body.get(k) is not None}, publication="draft")
        elif operation == "publish":
            try:
                _, _, readiness = await members_state(session, row, actor, public=True)
                if readiness != "ready":
                    raise error()
            except APIError:
                raise error("collection_not_publishable", 409) from None
            values["publication"] = "published"
        elif operation == "withdraw":
            values["publication"] = "withdrawn"
        else:
            raise error("invalid_collection_operation", 400)
        changed = await session.execute(update(Collection).where(Collection.id == row.id,
            Collection.revision == body["expected_revision"]).values(**values))
        if changed.rowcount != 1:
            raise error("collection_revision_conflict", 409)
        if operation == "replace":
            await pin_members(session, actor, row.id, body["version_ids"])
        if operation == "withdraw":
            # P6：撤回必须在同一个事务里清掉该集合的缓存投影。只靠"下一版
            # revision 不同"不够 —— 撤回的对象**任何版本都不该再被提供**，
            # 而不是"提供一个旧版本"。惰性 import 避免 catalog <-> cache 环。
            from ddp_corpus import cache
            await cache.invalidate(session, scope_key=cache.collection_scope(row.id))
        await session.refresh(row)
    session.add(CollectionReceipt(key_hash=key_hash, request_digest=request_digest,
        collection_id=row.id, revision=row.revision))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise error("collection_write_conflict", 409) from None
    if operation == "withdraw":
        return {"collection_id": row.id, "revision": row.revision, "publication": row.publication,
                "operation_revision": row.revision}
    return await collection_output(session, actor, row, operation_revision=row.revision)


def descriptor(row, origin, index_revision, valid_until):
    return {"schema": "ddp-discovery/1#CollectionDescriptor", "origin_node_id": origin,
        "collection_id": row.id, **row.metadata_json, "index_revision": index_revision,
        "revision": row.revision, "valid_until": valid_until.isoformat()}


async def visible_catalog(session, actor, origin, valid_until):
    rows = await session.stream_scalars(select(Collection).where(
        Collection.organization_id == actor.organization_id, Collection.publication == "published")
        .order_by(Collection.id).execution_options(yield_per=100))
    descriptors, readiness, byte_size = [], {}, 0
    async for row in rows:
        try:
            _, index_revision, state = await members_state(session, row, actor, public=True)
        except APIError:
            continue
        item = descriptor(row, origin, index_revision, valid_until)
        byte_size += len(json.dumps(item, ensure_ascii=False).encode())
        descriptors.append(item)
        readiness[row.id] = state
        if len(descriptors) > MAX_COLLECTIONS or byte_size > MAX_SNAPSHOT_BYTES:
            raise error("catalog_too_large", 507)
    return descriptors, readiness


async def new_snapshot(session, actor, scope, caller_scope, origin, binding, limit):
    await lock_key(session, "collection-view:"+binding)
    now, valid_until = utcnow(), utcnow()+timedelta(minutes=5)
    expired = select(CollectionCatalogSnapshot.id).where(CollectionCatalogSnapshot.binding == binding,
        CollectionCatalogSnapshot.valid_until <= now)
    await session.execute(delete(CollectionCatalogPage).where(CollectionCatalogPage.snapshot_id.in_(expired)))
    await session.execute(delete(CollectionCatalogSnapshot).where(CollectionCatalogSnapshot.id.in_(expired)))
    if await session.scalar(select(func.count()).select_from(CollectionCatalogSnapshot).where(
            CollectionCatalogSnapshot.binding == binding)) >= MAX_SNAPSHOTS:
        raise error("catalog_snapshot_limit", 429)
    descriptors, readiness = await visible_catalog(session, actor, origin, valid_until)
    fingerprint = digest([{k: v for k, v in item.items() if k != "valid_until"} for item in descriptors])
    view = await session.get(CollectionCatalogView, binding)
    if view is None:
        view = CollectionCatalogView(binding=binding, revision=1, fingerprint=fingerprint)
        session.add(view)
    elif view.fingerprint != fingerprint:
        view.revision += 1
        view.fingerprint = fingerprint
    snap = CollectionCatalogSnapshot(id=new_id(), binding=binding, scope_id=scope,
        caller_scope_hash=caller_scope, origin_node_id=origin, revision=view.revision, page_size=limit,
        descriptors=descriptors, index_readiness=readiness, created_at=now, valid_until=valid_until)
    session.add(snap)
    await session.flush()
    offsets = list(range(0, len(descriptors), limit)) + [len(descriptors)]
    pages = [CollectionCatalogPage(cursor=new_id(), snapshot_id=snap.id, offset=offset) for offset in offsets]
    session.add_all(pages)
    await session.commit()
    return snap, pages[0].cursor


async def snapshot_page(session, actor, scope, caller_scope, origin, snapshot_id, cursor, limit=None):
    binding = digest([actor_binding(actor), caller_scope, origin])
    if not snapshot_id:
        if cursor:
            raise error("invalid_catalog_request", 400)
        snap, cursor = await new_snapshot(session, actor, scope, caller_scope, origin, binding, limit or 100)
    else:
        snap = await session.get(CollectionCatalogSnapshot, snapshot_id)
        if not snap or (snap.binding, snap.scope_id) != (binding, scope):
            raise error("catalog_snapshot_invalid", 410)
    pages = list((await session.scalars(select(CollectionCatalogPage).where(
        CollectionCatalogPage.snapshot_id == snap.id).order_by(CollectionCatalogPage.offset))).all())
    page = next((p for p in pages if p.cursor == cursor), None)
    if page is None:
        raise error("catalog_snapshot_invalid", 410)
    # Cursor ownership is part of the scope binding. A guessed/wrong cursor must
    # not distinguish an expired snapshot from any other inaccessible snapshot.
    if as_aware(snap.valid_until) <= utcnow():
        raise error("catalog_snapshot_expired", 410)
    if limit is not None and limit != snap.page_size:
        raise error("invalid_catalog_request", 400)
    # Terminal proof also rechecks the entire original inventory, never only its empty page.
    revoked, changed = [], False
    for item in snap.descriptors:
        row = await session.scalar(select(Collection).where(Collection.id == item["collection_id"],
            Collection.organization_id == actor.organization_id).execution_options(populate_existing=True))
        if row is None or row.publication != "published":
            revoked.append(item["collection_id"])
            continue
        try:
            _, index_revision, _ = await members_state(session, row, actor, public=True)
        except APIError:
            revoked.append(item["collection_id"])
            continue
        if row.revision != item["revision"] or index_revision != item["index_revision"]:
            changed = True
    if revoked:
        invalid = error("catalog_snapshot_invalid", 410)
        invalid.revoked_collection_ids = revoked
        raise invalid
    if changed:
        raise error("catalog_snapshot_changed", 409)
    index = pages.index(page)
    terminal = index == len(pages)-1
    items = [] if terminal else snap.descriptors[page.offset:page.offset+snap.page_size]
    return {"snapshot_id": snap.id, "scope_id": snap.scope_id, "caller_scope_hash": snap.caller_scope_hash,
        "origin_node_id": snap.origin_node_id, "registry_revision": snap.revision,
        "created_at": as_aware(snap.created_at), "valid_until": as_aware(snap.valid_until),
        "first_cursor": pages[0].cursor, "terminal_cursor": pages[-1].cursor,
        "total": len(snap.descriptors), "collections": items,
        "index_readiness": {item["collection_id"]: snap.index_readiness[item["collection_id"]] for item in items},
        "next_cursor": None if terminal else pages[index+1].cursor,
        "complete": terminal, "content_snapshot_complete": False}


async def published_snapshot_page(session, actor, snapshot_id, cursor, limit=None):
    """One stable page of this node's explicitly published collections.

    This is the peer-directory producer: a service-only route with **no
    caller-supplied scope**. The fixed scope and caller digest live here, and the
    origin is this node's persistent identity. An unset identity fails closed
    instead of emitting origin-less descriptors that could never be validated.
    """
    origin = (settings.bundle_node_id or "").strip()
    if not origin:
        raise error("node_identity_unavailable", 503)
    if actor.kind != "service" or not actor.organization_id:
        raise error("service_identity_required", 403)
    return await snapshot_page(session, actor, PEER_DIRECTORY_SCOPE,
        PEER_DIRECTORY_CALLER, origin, snapshot_id, cursor, limit)
