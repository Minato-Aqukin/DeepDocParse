"""Authorization-aware client metadata windows; never a source-text cache or command queue."""
import hashlib
import json
import secrets
from datetime import timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from ddp_corpus.client_models import ClientPage, ClientReceipt, ClientSnapshot, ClientView
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion, Task, as_aware, utcnow
from ddp_corpus.policy import resource_condition

PAGE_ITEMS = 100
PAGE_BYTES = 512 * 1024
MAX_ITEMS = 20000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_SCOPE_BYTES = 64 * 1024 * 1024
TTL_SECONDS = 900


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def error(code="cursor_expired", status=410):
    return APIError(status, "client projection unavailable", "invalid_request_error", code)


def scope_for(actor, credential_scope):
    if actor.principal_id is None or len(credential_scope) != 71 or not credential_scope.startswith("sha256:"):
        raise error("client_scope_required", 401)
    try:
        bytes.fromhex(credential_scope[7:])
    except ValueError:
        raise error("client_scope_required", 401) from None
    return digest([actor.organization_id, actor.principal_id, actor.kind, actor.id,
                   actor.api_key_id, actor.role, credential_scope])


def bindings_statement(actor):
    return select(Resource.id, ResourceVersion.id, ResourceVersion.parse_job_id).join(
        ResourceVersion, ResourceVersion.resource_id == Resource.id).join(
        Document, Document.id == ResourceVersion.document_id).where(
        resource_condition(actor), ResourceVersion.deleted_at.is_(None), Document.deleted_at.is_(None))


async def ensure_authorized(session, actor, snapshot):
    # A frozen metadata page is not a permission grant. Check all frozen bindings,
    # so revocation of an already cached first page also invalidates later pages.
    current = {tuple(row) for row in (await session.execute(bindings_statement(actor))).all()}
    if any(tuple(binding) not in current for binding in snapshot.bindings):
        raise error()


async def collect_state(session, actor, capabilities):
    rows = (await session.execute(select(Resource, ResourceVersion, ParseJob).join(
        ResourceVersion, ResourceVersion.resource_id == Resource.id).join(
        Document, Document.id == ResourceVersion.document_id).outerjoin(
        ParseJob, ParseJob.id == ResourceVersion.parse_job_id).where(
        resource_condition(actor), ResourceVersion.deleted_at.is_(None), Document.deleted_at.is_(None))
        .order_by(Resource.id, ResourceVersion.version_no).limit(MAX_ITEMS + 1))).all()
    if len(rows) > MAX_ITEMS:
        raise error("projection_too_large", 507)
    resources, tasks, bindings = [], {}, []
    for resource, version, job in rows:
        bindings.append([resource.id, version.id, version.parse_job_id])
        resources.append({"id": resource.id, "resource_id": resource.id, "version_id": version.id,
            "version_no": version.version_no, "parse_revision": version.parse_job_id,
            "document_id": version.document_id, "name": resource.display_name,
            "filename": version.filename, "publication": resource.publication,
            "owner_id": resource.owner_id, "source_digest": version.source_digest,
            "size_bytes": version.size_bytes, "index_status": job.index_status if job else "none"})
        if job:
            task = tasks.setdefault(job.id, {"id": job.id, "kind": "doc.parse", "status": job.status,
                "engine": job.engine, "parse_revision": job.id, "version_ids": [],
                "index_status": job.index_status, "compile_status": job.compile_status,
                "error_code": "parse_failed" if job.error else None})
            task["version_ids"].append(version.id)
    # Only job-bound queue records in the authenticated organization are projected.
    # Payloads, worker identity, lease heartbeats and raw errors never leave the service.
    if tasks:
        queued = (await session.execute(select(Task).where(Task.organization_id == actor.organization_id,
            Task.payload["job_id"].as_string().in_(list(tasks))).order_by(Task.id).limit(MAX_ITEMS + 1))).scalars().all()
        if len(queued) > MAX_ITEMS:
            raise error("projection_too_large", 507)
        for task in queued:
            tasks["queue:" + task.id] = {"id": "queue:" + task.id, "kind": task.kind,
                "status": task.status, "parse_revision": task.payload["job_id"],
                "error_code": "task_failed" if task.error else None, "degraded": task.degraded}
    values = {"resources": resources, "tasks": list(tasks.values()), **capabilities}
    if len(encoded(values)) > MAX_SNAPSHOT_BYTES:
        raise error("projection_too_large", 507)
    return values, bindings


def chunks(items):
    pages, page, size = [], [], 0
    for item in items:
        length = len(encoded(item))
        if length > PAGE_BYTES:
            raise error("projection_item_too_large", 507)
        if page and (len(page) == PAGE_ITEMS or size + length > PAGE_BYTES):
            pages.append(page)
            page, size = [], 0
        page.append(item)
        size += length
    pages.append(page)
    return pages


def make_snapshot(scope, sequence, values, bindings):
    now, cursor = utcnow(), secrets.token_hex(24)
    state = {k: v for k, v in values.items() if k not in ("resources", "tasks")}
    state.update({"snapshot_id": cursor, "windows": {}, "snapshot_complete": True,
        "projection_scope":{"resources":"authorized_fixed_versions",
                            "tasks":"fixed_parse_and_job_bound_queue", "all_task_kinds":False}})
    pages = []
    for kind in ("resources", "tasks"):
        groups = chunks(values[kind])
        cursors = [secrets.token_hex(24) for _ in groups]
        for index, items in enumerate(groups):
            next_cursor = cursors[index + 1] if index + 1 < len(groups) else None
            body = {"snapshot_id": cursor, "sequence": sequence, "kind": kind,
                "items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None,
                "visible_total": len(values[kind]), "items_loaded": len(items), "page_index": index}
            pages.append(ClientPage(cursor=cursors[index], snapshot_id=cursor, kind=kind, body=body))
        state[kind] = groups[0]
        state["windows"][kind] = {"visible_total": len(values[kind]), "items_loaded": len(groups[0]),
            "has_more": len(groups) > 1, "next_cursor": cursors[1] if len(groups) > 1 else None}
    state["cache_complete"] = all(not w["has_more"] for w in state["windows"].values())
    size = len(encoded(state)) + sum(len(encoded(p.body)) for p in pages) + len(encoded(bindings))
    if size > MAX_SNAPSHOT_BYTES or len(encoded(state)) > 3 * 1024 * 1024:
        raise error("projection_too_large", 507)
    return ClientSnapshot(id=cursor, scope=scope, sequence=sequence, state=state, bindings=bindings,
        byte_size=size, created_at=now, expires_at=now + timedelta(seconds=TTL_SECONDS)), pages


def frame(snapshot, previous=None):
    result = {"cursor": snapshot.id, "sequence": snapshot.sequence, "state": snapshot.state}
    if previous is not None:
        result["previous_sequence"] = previous
    return result


async def observe(session, actor, scope, capabilities, after=None):
    for attempt in range(5):
        try:
            dialect = session.bind.dialect.name
            if dialect == "postgresql":
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            else:
                await session.execute(text("BEGIN IMMEDIATE"))
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            await session.execute(insert(ClientView).values(scope=scope, sequence=0, fingerprint="", cursor="")
                                  .on_conflict_do_nothing(index_elements=["scope"]))
            view = await session.scalar(select(ClientView).where(ClientView.scope == scope).with_for_update()
                                        .execution_options(populate_existing=True))
            previous = None
            if after is not None:
                previous = await session.scalar(select(ClientSnapshot).where(
                    ClientSnapshot.id == after, ClientSnapshot.scope == scope))
                if previous is None or as_aware(previous.expires_at) <= utcnow() or previous.sequence > view.sequence:
                    raise error()
            values, bindings = await collect_state(session, actor, capabilities)
            fingerprint = digest(values)
            current = await session.get(ClientSnapshot, view.cursor) if view.cursor else None
            if current is None or fingerprint != view.fingerprint or as_aware(current.expires_at) <= utcnow():
                view.sequence += 1
                current, pages = make_snapshot(scope, view.sequence, values, bindings)
                session.add(current)
                await session.flush()
                session.add_all(pages)
                view.cursor, view.fingerprint = current.id, fingerprint
                # Bound persisted history; expired/evicted cursors explicitly require a snapshot.
                retained = (await session.execute(select(ClientSnapshot).where(ClientSnapshot.scope == scope)
                    .order_by(ClientSnapshot.sequence.desc()))).scalars().all()
                total = 0
                for index, old in enumerate(retained):
                    total += old.byte_size
                    if index >= 16 or total > MAX_SCOPE_BYTES or as_aware(old.expires_at) <= utcnow():
                        await session.execute(delete(ClientPage).where(ClientPage.snapshot_id == old.id))
                        await session.delete(old)
            await session.commit()
            # Recheck under a fresh read after releasing the serialization transaction.
            await ensure_authorized(session, actor, current)
            result = frame(current, previous.sequence if previous else None)
            return {"events": [result]} if after is not None else result
        except DBAPIError as exc:
            await session.rollback()
            if getattr(exc.orig, "sqlstate", None) not in ("40001", "40P01") or attempt == 4:
                raise
        except Exception:
            await session.rollback()
            raise


async def page(session, actor, scope, snapshot_id, cursor, kind):
    snapshot = await session.scalar(select(ClientSnapshot).where(
        ClientSnapshot.id == snapshot_id, ClientSnapshot.scope == scope))
    if snapshot is None or as_aware(snapshot.expires_at) <= utcnow():
        raise error()
    found = await session.scalar(select(ClientPage).where(ClientPage.cursor == cursor,
        ClientPage.snapshot_id == snapshot.id, ClientPage.kind == kind))
    if found is None:
        raise error()
    await ensure_authorized(session, actor, snapshot)
    return found.body


def receipt_hash(organization_id, principal_id, operation_key):
    return digest([organization_id, principal_id, operation_key])


async def record_upload_receipt(session, *, organization_id, principal_id, operation_key,
                                request_digest, resource_id, version_id, parse_job_id):
    key = receipt_hash(organization_id, principal_id, operation_key)
    existing = await session.get(ClientReceipt, key)
    if existing:
        if existing.request_digest != request_digest or (existing.resource_id, existing.version_id, existing.parse_job_id) != (resource_id, version_id, parse_job_id):
            raise error("idempotency_conflict", 409)
        return
    session.add(ClientReceipt(key_hash=key, organization_id=organization_id, principal_id=principal_id,
        request_digest=request_digest, resource_id=resource_id, version_id=version_id, parse_job_id=parse_job_id))
    await session.flush()


async def receipt(session, actor, operation_key):
    row = await session.get(ClientReceipt, receipt_hash(actor.organization_id, actor.principal_id, operation_key))
    if row is None:
        raise error("receipt_not_found", 404)
    binding = await session.execute(bindings_statement(actor).where(Resource.id == row.resource_id,
        ResourceVersion.id == row.version_id, ResourceVersion.parse_job_id == row.parse_job_id))
    if binding.first() is None:
        raise error("receipt_not_found", 404)
    return {"operation_key": operation_key, "accepted": True, "operation": "document.upload",
        "resource_id": row.resource_id, "version_id": row.version_id, "parse_revision": row.parse_job_id,
        "task_id": row.parse_job_id, "request_digest": row.request_digest,
        "accepted_at": as_aware(row.accepted_at).isoformat()}
