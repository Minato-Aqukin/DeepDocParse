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
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_service_client, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.models import new_id, utcnow
from ddp_corpus.remote_compute_models import RemoteCompute
from ddp_corpus.storage import Storage

router = APIRouter(prefix="/api/v1/remote-compute")

#: Temporary input key space (agreed with GC owner). Only keys under this
#: prefix may be deleted by remote-compute cleanup, and only for this record
#: id. Anything else keeps its live references.
TMP_PREFIX = "tmp-remote-compute/"
#: Post-terminal grace: inputs/derived data of terminal records stay until
#: this age so resume/ack replays and GC grace windows can still observe them.
TERMINAL_GRACE_SECONDS = 3600
#: Waiting-record TTL when no verified input ever arrives.
WAITING_TTL_SECONDS = 86400

ACTIVE = ("waiting_input", "content_verifying", "content_verified", "running")
TERMINAL = ("succeeded", "failed", "expired", "cancelled", "acked")


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


async def _get(session: AsyncSession, actor: Actor, compute_id: str) -> RemoteCompute:
    row = await session.get(RemoteCompute, compute_id)
    if row is None:
        raise APIError(404, "remote compute not found",
                       "invalid_request_error", "remote_compute_not_found")
    return _owned(row, actor)


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
                       session: AsyncSession = Depends(get_session)):
    row = await _get(session, actor, compute_id)
    _expire_if_past(session, row)
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.post("/{compute_id}/cancel")
async def cancel_compute(compute_id: str, request: Request,
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage)):
    actor.require(actor.can_upload and actor.principal_id is not None, "取消远端计算")
    row = await _get(session, actor, compute_id)
    if row.status == "acked":
        raise APIError(409, "acknowledged compute cannot be cancelled",
                       "invalid_request_error", "already_acked")
    if row.status not in TERMINAL:
        row.status = "cancelled"
        row.updated_at = utcnow()
        await session.commit()
    # Cleanup keeps every other live reference; only this record's tmp prefix.
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
    row = await _get(session, actor, compute_id)
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
async def download_bundle(compute_id: str, actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session),
                          storage: Storage = Depends(get_storage)):
    """Fixed-manifest output bytes with resume; hashing happens on the host.

    Returns the actual ZIP bytes (`application/zip`) of the fixed Bundle so
    the host can stream with Range/resume and hash the complete bytes. The
    manifest digest is `sha256:` of these exact bytes (never a JSON receipt).
    Reading is not confirmation.
    """
    from fastapi.responses import Response as RawResponse

    row = await _get(session, actor, compute_id)
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else None
    if row.status not in ("succeeded", "acked") or not manifest \
            or not isinstance(manifest.get("bundle_key"), str):
        raise APIError(404, "remote compute output not available",
                       "invalid_request_error", "result_unavailable")
    try:
        data = await storage.get(manifest["bundle_key"])
    except Exception:
        raise APIError(502, "stored bundle unreadable", "server_error",
                       "bundle_storage_mismatch")
    actual = hashlib.sha256(data).hexdigest()
    if actual != manifest.get("output_sha256"):
        raise APIError(502, "stored bundle failed digest verification",
                       "server_error", "bundle_storage_mismatch")
    return RawResponse(data, media_type="application/zip",
                       headers={"Cache-Control": "no-store",
                                "X-Output-SHA256": actual})


def _expire_if_past(session: AsyncSession, row: RemoteCompute) -> None:
    if row.status in TERMINAL:
        return
    if row.expires_at is not None and row.expires_at <= utcnow():
        row.status = "expired"
        row.updated_at = utcnow()


async def get_storage_from_app(session: AsyncSession):
    # Resolved by the caller app state; kept as a function so worker/sweep
    # paths can reuse cleanup without importing the FastAPI app.
    from ddp_corpus.db import get_sessionmaker  # noqa: F401
    raise RuntimeError("storage must be passed by the HTTP caller")


async def cleanup_compute(session: AsyncSession, storage: Storage,
                          row: RemoteCompute) -> list[str]:
    """Delete only this record's tmp prefix; keep every other live reference.

    Called after ack/cancel/failure/TTL. Prefix-scoped: only keys starting
    with `tmp-remote-compute/{org}/{id}/` are listed, and each listed key is
    rechecked against other live references (other computes' input keys and
    permanent Document.object_key values) before deletion.
    """
    prefix = f"{TMP_PREFIX}{row.organization_id}/{row.id}/"
    try:
        keys = [k for k in await storage.list_prefix(prefix) if k.startswith(prefix)]
    except Exception:
        return []
    if not keys:
        return []
    protected: set[str] = set()
    others = (await session.execute(select(RemoteCompute.input_object_key).where(
        RemoteCompute.id != row.id,
        RemoteCompute.input_object_key.is_not(None)))).scalars()
    protected.update(k for k in others if isinstance(k, str) and k)
    from ddp_corpus.models import Document
    docs = (await session.execute(select(Document.object_key).where(
        Document.object_key != ""))).scalars()
    for key in docs:
        if isinstance(key, str) and key and not key.startswith(TMP_PREFIX):
            # Temporary compute inputs live under this record's own tmp prefix
            # by design (see `bind_verified_upload`): they are the bytes under
            # test here, not permanent references that pin them.
            protected.add(key)
    removed = []
    for key in keys:
        if key in protected:
            continue
        try:
            await storage.delete(key)
        except Exception:
            continue
        removed.append(key)
    return removed


async def sweep_remote_computes(session: AsyncSession, storage: Storage,
                                limit: int = 50) -> dict:
    """Expire past-due records and cleanup terminal inputs past the grace age."""
    now = utcnow()
    expired = 0
    cleaned = 0
    rows = list((await session.execute(select(RemoteCompute).where(
        RemoteCompute.status.notin_(TERMINAL),
        RemoteCompute.expires_at.is_not(None),
        RemoteCompute.expires_at <= now).limit(limit))).scalars())
    for row in rows:
        row.status = "expired"
        row.updated_at = now
        expired += 1
    await session.commit()
    old = now - timedelta(seconds=TERMINAL_GRACE_SECONDS)
    terminal = list((await session.execute(select(RemoteCompute).where(
        RemoteCompute.status.in_(TERMINAL),
        RemoteCompute.updated_at <= old).limit(limit))).scalars())
    for row in terminal:
        removed = await cleanup_compute(session, storage, row)
        cleaned += len(removed)
    await session.commit()
    return {"expired": expired, "cleaned_keys": cleaned}
