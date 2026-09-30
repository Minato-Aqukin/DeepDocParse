"""Persistent file-compute coordination: waiting input -> verified parse -> delivery.

Not the retrieve/answer federation root-task path. A record is created
idempotently (`POST /api/v1/remote-compute`) fixing input sha256/size, plan
digest and source/target identity for one actor/org with a temporary
retention. The input itself travels the existing `/api/uploads`
multipart+reconcile+finalize channel with purpose=`temporary_compute`; no
parse work is queued while the upload is still waiting. Only the fully
verified input digest (control streaming sha256 + size, bound to the same
actor) enqueues the existing real parse path (`ingest.ingest_document` +
`submit_parse` to the gateway, no GPU pre-reservation: ParseJob rows are
passive until the gateway/archive path picks them up). Metadata pre-checks
never impersonate `content_verified`; temporary inputs never enter the
permanent public catalog.

Outputs travel the existing Bundle/storage channel. The fixed manifest pins
source/version/output hash where `result_manifest_digest` is `sha256:` of the
actual ZIP bytes (never a JSON receipt hashed as ZIP). The local app streams
the ZIP with resume, hashes the complete bytes, atomically imports, then acks
(`POST /{id}/ack`); lost/duplicate acks replay with the same key. TTL, cancel,
failure and ack states stay visible; cleanup of inputs and derived data keeps
every other live reference (reference-safe GC owns the deletion rule).
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.bundle import MAX_ARCHIVE
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion, as_aware, new_id, utcnow
from ddp_corpus.remote_compute_models import (
    CLOSED_REMOTE_COMPUTE, UNCONFIRMED_REMOTE_COMPUTE, RemoteCompute, expire_if_due,
)
from ddp_corpus.storage import Storage

router = APIRouter(prefix="/api/v1/remote-compute")

#: Temporary input key space (agreed with GC owner). Only keys under this
#: prefix may be deleted by remote-compute cleanup, and only for this record
#: id. Anything else keeps its live references.
TMP_PREFIX = "tmp-remote-compute/"
#: Closed delivery grace; succeeded remains available until its own expiry.
TERMINAL_GRACE_SECONDS = 3600
#: Approved temporary input/output lifetime.
WAITING_TTL_SECONDS = 86400



def _actor(actor: Actor) -> tuple[str, str]:
    principal = actor.principal_id or actor.id
    if not principal:
        raise APIError(403, "anonymous compute not allowed",
                       "permission_error", "insufficient_role")
    return principal, actor.organization_id


def _principal_of(actor: Actor) -> str:
    principal, _ = _actor(actor)
    return principal


def _hex64(value, field) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise APIError(400, f"bad {field}", "invalid_request_error", "bad_digest")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise APIError(400, f"bad {field}", "invalid_request_error", "bad_digest")
    return value.lower()


def _plan_digest(value) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise APIError(400, "bad plan_digest", "invalid_request_error", "bad_digest")
    _hex64(value[7:], "plan_digest")
    return value.lower()


def _request_digest(body: dict) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _tmp_key(organization_id: str, compute_id: str) -> str:
    return f"{TMP_PREFIX}{organization_id}/{compute_id}/source.bin"


def _owned(row: RemoteCompute, actor: Actor) -> RemoteCompute:
    principal, org = _actor(actor)
    if row.organization_id != org or row.actor_id != principal:
        # Same shape as missing: cross-actor binding is not observable.
        raise APIError(404, "remote compute not found",
                       "invalid_request_error", "remote_compute_not_found")
    return row


def _out(row: RemoteCompute) -> dict:
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else None
    digest = None
    if manifest and isinstance(manifest.get("output_sha256"), str):
        digest = "sha256:" + manifest["output_sha256"]
    return {
        "id": row.id,
        "status": row.status,
        "input_sha256": row.input_sha256,
        "input_size": row.input_size,
        "plan_digest": row.plan_digest,
        "source_identity": row.source_identity,
        "target_identity": row.target_identity,
        "retention": row.retention,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "upload_id": row.upload_id,
        "manifest": manifest,
        "result_manifest_digest": digest,
    }


async def _get(session: AsyncSession, actor: Actor, compute_id: str, storage: Storage) -> RemoteCompute:
    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.id == compute_id).with_for_update()
        .execution_options(populate_existing=True))
    if row is None:
        raise APIError(404, "remote compute not found",
                       "invalid_request_error", "remote_compute_not_found")
    _owned(row, actor)
    if expire_if_due(row):
        await session.commit()
        await session.refresh(row)
    if row.status in CLOSED_REMOTE_COMPUTE:
        await cleanup_compute(session, storage, row)
    return row


@router.post("", status_code=201)
async def create_compute(request: Request,
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session),
                         idempotency_key: str = Header(min_length=1, max_length=128)):
    actor.require(actor.can_upload and actor.principal_id is not None, "创建远端计算")
    body = await request.json()
    if not isinstance(body, dict):
        raise APIError(400, "bad compute request", "invalid_request_error", "bad_request")
    input_sha256 = _hex64(body.get("input_sha256"), "input_sha256")
    input_size = body.get("input_size")
    if type(input_size) is not int or input_size < 1:
        raise APIError(400, "bad input_size", "invalid_request_error", "bad_upload")
    plan_digest = _plan_digest(body.get("plan_digest"))
    source_identity = body.get("source_identity")
    target_identity = body.get("target_identity")
    if not isinstance(source_identity, dict) or not isinstance(target_identity, dict):
        raise APIError(400, "fixed source/target identity required",
                       "invalid_request_error", "bad_identity")
    retention = body.get("retention", "temporary")
    if retention not in ("temporary", "task_pinned"):
        raise APIError(400, "retention must stay temporary",
                       "invalid_request_error", "bad_retention")
    principal, org = _actor(actor)
    canonical = {"input_sha256": input_sha256, "input_size": input_size,
                 "plan_digest": plan_digest, "source_identity": source_identity,
                 "target_identity": target_identity, "retention": retention}
    digest = _request_digest(canonical)
    now = utcnow()
    prior = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.organization_id == org,
        RemoteCompute.idempotency_key == idempotency_key))
    if prior is not None:
        _owned(prior, actor)
        if prior.request_digest != digest:
            raise APIError(409, "idempotency key reused with different input",
                           "invalid_request_error", "idempotency_conflict")
        return JSONResponse(_out(prior), status_code=200)
    row = RemoteCompute(
        id=new_id(), organization_id=org, actor_id=principal,
        actor_kind=actor.kind, status="waiting_input",
        input_sha256=input_sha256, input_size=input_size, plan_digest=plan_digest,
        source_identity=source_identity, target_identity=target_identity,
        retention=retention, idempotency_key=idempotency_key, request_digest=digest,
        expires_at=now + timedelta(seconds=WAITING_TTL_SECONDS),
        created_at=now, updated_at=now)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        prior = await session.scalar(select(RemoteCompute).where(
            RemoteCompute.organization_id == org,
            RemoteCompute.idempotency_key == idempotency_key))
        if prior is None:
            raise
        _owned(prior, actor)
        if prior.request_digest != digest:
            raise APIError(409, "idempotency key reused with different input",
                           "invalid_request_error", "idempotency_conflict")
        return JSONResponse(_out(prior), status_code=200)
    await session.refresh(row)
    return JSONResponse(_out(row), status_code=201)


@router.get("/{compute_id}")
async def read_compute(compute_id: str, actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session),
                       storage: Storage = Depends(get_storage)):
    row = await _get(session, actor, compute_id, storage)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/{compute_id}/cancel")
async def cancel_compute(compute_id: str, request: Request,
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage)):
    actor.require(actor.can_upload and actor.principal_id is not None, "取消远端计算")
    row = await _get(session, actor, compute_id, storage)
    if row.status == "acked":
        raise APIError(409, "acknowledged compute cannot be cancelled",
                       "invalid_request_error", "already_acked")
    if row.status in UNCONFIRMED_REMOTE_COMPUTE:
        row.status = "cancelled"
        row.updated_at = utcnow()
        await session.commit()
    # Revoke only this compute's resource; generic GC retains shared originals.
    await cleanup_compute(session, storage, row)
    await session.refresh(row)
    return _out(row)


@router.post("/{compute_id}/ack")
async def ack_compute(compute_id: str, request: Request,
                      actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session),
                      storage: Storage = Depends(get_storage)):
    actor.require(actor.can_upload and actor.principal_id is not None, "确认远端计算")
    body = await request.json()
    if not isinstance(body, dict):
        raise APIError(400, "bad ack request", "invalid_request_error", "bad_request")
    output = _hex64(body.get("output_sha256"), "output_sha256")
    row = await _get(session, actor, compute_id, storage)
    if row.status == "acked":
        # Lost/duplicate ack replays safely with the same digest.
        if (row.output_sha256 or "") != output:
            raise APIError(409, "ack digest differs from the confirmed output",
                           "invalid_request_error", "idempotency_conflict")
        return _out(row)
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else None
    if row.status != "succeeded" or not manifest \
            or manifest.get("output_sha256") != output:
        # Hash mismatch or not-yet-delivered never imports: refuse before ack.
        raise APIError(409, "output hash does not match the fixed manifest",
                       "invalid_request_error", "input_not_verified")
    row.status = "acked"
    row.output_sha256 = output
    row.updated_at = utcnow()
    await session.commit()
    await cleanup_compute(session, storage, row)
    await session.refresh(row)
    return _out(row)

@router.get("/{compute_id}/bundle")
async def download_bundle(compute_id: str, request: Request,
                          actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session),
                          storage: Storage = Depends(get_storage)):
    """Fixed-manifest output bytes with resume; hashing happens on the host.

    Returns the actual ZIP bytes (`application/zip`) of the fixed Bundle so
    the host can stream with Range/resume and hash the complete bytes. The
    manifest digest is `sha256:` of these exact bytes (never a JSON receipt).
    Reading is not confirmation.

    Contract (frozen): single `Range: bytes=start-end` (inclusive, `start=0`
    allowed, `end` clamps at EOF) with `If-Match: "<bare 64hex output_sha256>"`.
    A 206 chunk declares the fixed manifest digest in `ETag`/`X-Output-SHA256`;
    the client rehash of the reassembled bytes is the integrity authority, so
    range reads never download/rehash the whole object. Only the no-Range 200
    keeps the prior full digest verification (bounded to `MAX_ARCHIVE`).
    """
    row = await _get(session, actor, compute_id, storage)
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else None
    if row.status not in ("succeeded", "acked") or row.cleaned_at is not None or not manifest \
            or not isinstance(manifest.get("bundle_key"), str):
        raise APIError(404, "remote compute output not available",
                       "invalid_request_error", "result_unavailable")
    bundle_key = manifest["bundle_key"]
    expected = manifest.get("output_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise APIError(502, "stored bundle failed digest verification",
                       "server_error", "bundle_storage_mismatch")
    try:
        bytes.fromhex(expected)
    except ValueError:
        raise APIError(502, "stored bundle failed digest verification",
                       "server_error", "bundle_storage_mismatch")
    expected = expected.lower()
    # Fixed-digest precondition before any object metadata leaves the server.
    if_match = request.headers.get("if-match")
    if if_match is not None and if_match.strip() != "" and if_match.strip() != "*":
        raw = if_match.strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            token = raw[1:-1].strip()
        else:
            token = raw
        valid = len(token) == 64
        if valid:
            try:
                bytes.fromhex(token)
            except ValueError:
                valid = False
        if not valid or token.lower() != expected:
            raise APIError(412, "output digest does not match the fixed manifest",
                           "invalid_request_error", "precondition_failed")
    try:
        total = await storage.stat_size(bundle_key)
    except Exception:
        raise APIError(502, "stored bundle unreadable", "server_error",
                       "bundle_storage_mismatch")
    if not isinstance(total, int) or isinstance(total, bool) \
            or total <= 0 or total > MAX_ARCHIVE:
        raise APIError(502, "stored bundle exceeds size limit", "server_error",
                       "bundle_storage_mismatch")
    range_header = request.headers.get("range")
    base_headers = {"Cache-Control": "no-store",
                    "Accept-Ranges": "bytes",
                    "ETag": f'"{expected}"',
                    "X-Output-SHA256": expected}
    if range_header is None:
        try:
            data = await storage.get_limited(bundle_key, MAX_ARCHIVE)
        except ValueError:
            raise APIError(502, "stored bundle exceeds size limit", "server_error",
                           "bundle_storage_mismatch")
        except Exception:
            raise APIError(502, "stored bundle unreadable", "server_error",
                           "bundle_storage_mismatch")
        if len(data) != total:
            raise APIError(502, "stored bundle failed digest verification",
                           "server_error", "bundle_storage_mismatch")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise APIError(502, "stored bundle failed digest verification",
                           "server_error", "bundle_storage_mismatch")
        return Response(data, media_type="application/zip", headers=base_headers)
    # Single bytes=start-end only; suffix/open/multi forms are 416, never silent.
    text = range_header.strip()
    start: int | None = None
    end: int | None = None
    if text[:6].lower() == "bytes=":
        spec = text[6:].strip()
        if "," not in spec and spec.count("-") == 1:
            left, right = spec.split("-", 1)
            left, right = left.strip(), right.strip()
            # HTTP bounds are ASCII digits only: int() would also accept
            # underscores, signs, whitespace and Unicode decimals.
            if (left.isascii() and left.isdigit()
                    and right.isascii() and right.isdigit()
                    and len(left) <= 20 and len(right) <= 20):
                try:
                    start, end = int(left), int(right)
                except ValueError:
                    start, end = None, None
    if start is None or end is None or start < 0 or end < 0 or end < start:
        raise APIError(416, "invalid range", "invalid_request_error", "invalid_range",
                       headers={"Content-Range": f"bytes */{total}"})
    if start >= total:
        raise APIError(416, "range not satisfiable", "invalid_request_error",
                       "range_not_satisfiable",
                       headers={"Content-Range": f"bytes */{total}"})
    if end >= total:
        end = total - 1
    length = end - start + 1
    try:
        chunk = await storage.get_range(bundle_key, start, length)
    except ValueError:
        raise APIError(502, "stored bundle failed digest verification",
                       "server_error", "bundle_storage_mismatch")
    except Exception:
        raise APIError(502, "stored bundle unreadable", "server_error",
                       "bundle_storage_mismatch")
    if len(chunk) != length:
        raise APIError(502, "stored bundle failed digest verification",
                       "server_error", "bundle_storage_mismatch")
    return Response(chunk, status_code=206, media_type="application/zip",
                    headers={**base_headers,
                             "Content-Range": f"bytes {start}-{end}/{total}",
                             "Content-Length": str(length)})




async def cleanup_compute(session: AsyncSession, storage: Storage,
                          row: RemoteCompute) -> list[str]:
    """Revoke the owned resource; GC owns originals, this sweep owns delivery ZIPs."""
    from ddp_corpus.resources import tombstone_resource

    if row.status not in CLOSED_REMOTE_COMPUTE or row.cleaned_at is not None:
        return []
    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.id == row.id).with_for_update()
        .execution_options(populate_existing=True))
    if row.status not in CLOSED_REMOTE_COMPUTE or row.cleaned_at is not None:
        return []
    closed_at = as_aware(row.updated_at)
    job = await session.get(ParseJob, row.parse_job_id) if row.parse_job_id else None
    if job is not None and job.resource_id:
        # Use the established Document -> Resource lock order. A user-appended
        # version or explicit publication is another reference, not task garbage.
        await session.execute(select(Document.id).where(
            Document.id == job.document_id).with_for_update())
        resource = await session.scalar(select(Resource).where(
            Resource.id == job.resource_id).with_for_update()
            .execution_options(populate_existing=True))
        if (resource is not None and resource.deleted_at is None
                and resource.owner_id == row.actor_id
                and resource.organization_id == row.organization_id
                and resource.publication == "private"):
            versions = list((await session.execute(select(ResourceVersion).where(
                ResourceVersion.resource_id == resource.id,
                ResourceVersion.deleted_at.is_(None)))).scalars())
            if (len(versions) == 1 and versions[0].parse_job_id == job.id
                    and versions[0].document_id == job.document_id
                    and versions[0].source_digest == row.input_sha256):
                await tombstone_resource(session, resource)
    await session.commit()
    if closed_at > utcnow() - timedelta(seconds=TERMINAL_GRACE_SECONDS):
        return []
    # An accepted parse may finish after cancellation and still write derived
    # files. Its durable job remains the GC protection until that work settles.
    if job is not None:
        await session.refresh(job)
        if job.status in ("pending", "running", "archiving"):
            return []
    prefix = f"{TMP_PREFIX}{row.organization_id}/{row.id}/"
    try:
        keys = {key for key in await storage.list_prefix(prefix) if key.startswith(prefix)}
    except Exception:
        return []
    # Historical fixed delivery locations are reclaimed by exact binding only;
    # no general bundles/ prefix walk and no arbitrary manifest-provided path.
    bundle_key = (row.manifest_json or {}).get("bundle_key")
    if isinstance(bundle_key, str) and bundle_key.startswith(f"bundles/remote-compute/{row.id}/"):
        keys.add(bundle_key)
    removed, complete = [], True
    for key in sorted(keys):
        # Every Document key, including temporary originals, belongs to durable
        # reference-safe GC. Never bypass citations or a separate live resource.
        document_ref = await session.scalar(select(Document.id).where(
            Document.object_key == key).limit(1))
        if document_ref is not None:
            continue
        other_compute = await session.scalar(select(RemoteCompute.id).where(
            RemoteCompute.id != row.id,
            (RemoteCompute.input_object_key == key)
            | (RemoteCompute.manifest_json["bundle_key"].as_string() == key)).limit(1))
        if other_compute is not None:
            continue
        try:
            await storage.delete(key)
        except Exception:
            complete = False
            continue
        removed.append(key)
    if complete:
        row.cleaned_at = utcnow()
        await session.commit()
    return removed


async def sweep_remote_computes(session: AsyncSession, storage: Storage,
                                limit: int = 50) -> dict:
    """Expire unconfirmed outputs and retry only unfinished closed cleanup."""
    now = utcnow()
    rows = list((await session.execute(select(RemoteCompute).where(
        RemoteCompute.status.in_(UNCONFIRMED_REMOTE_COMPUTE),
        RemoteCompute.expires_at.is_not(None), RemoteCompute.expires_at <= now)
        .order_by(RemoteCompute.expires_at, RemoteCompute.id).limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True))).scalars())
    expired = sum(expire_if_due(row, at=now) for row in rows)
    await session.commit()
    for row in rows:
        await cleanup_compute(session, storage, row)
    old = now - timedelta(seconds=TERMINAL_GRACE_SECONDS)
    terminal = list((await session.execute(select(RemoteCompute).where(
        RemoteCompute.status.in_(CLOSED_REMOTE_COMPUTE),
        RemoteCompute.cleaned_at.is_(None), RemoteCompute.updated_at <= old)
        .order_by(RemoteCompute.updated_at, RemoteCompute.id).limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True))).scalars())
    cleaned = 0
    for row in terminal:
        cleaned += len(await cleanup_compute(session, storage, row))
    await session.commit()
    return {"expired": expired, "cleaned_keys": cleaned}
