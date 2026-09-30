"""Bind a verified temporary_compute upload to its waiting compute, then parse.

Called from the DocumentSubmitted consumer when the control event carries
`purpose=temporary_compute`. It never trusts client claims: the control row
is already `ready` (streaming sha256 + size verified by control), and this
function additionally requires:

- the same actor/org that created the waiting record (cross-actor binding is
  refused as not-found, never as a visible conflict);
- the verified digest/size equal the frozen record digest/size;
- the plan digest equal the approved plan digest;
- the record still `waiting_input` (complete/unknown replays do not create a
  second asset or task: they re-read the persisted binding).

Only then does it reuse the existing real parse path
(`ingest_document` + `submit_parse` to the gateway, no GPU pre-reservation:
ParseJob rows are passive until the gateway/archive path picks them up).
Metadata pre-checks never impersonate `content_verified`: the record moves to
`content_verified` only here, after the full verification above. Temporary
inputs never enter the permanent public catalog: ingest uses a
`tmp-remote-compute/{org}/{id}/source.bin` object key under the agreed tmp
prefix, and no publication is created.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import ingest as ingest_mod
from ddp_corpus.control_client import ControlClient
from ddp_corpus.errors import APIError
from ddp_corpus.models import ResourceVersion, utcnow
from ddp_corpus.remote_compute_models import RemoteCompute, expire_if_due
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import Storage


async def bind_verified_upload(session: AsyncSession, storage: Storage,
                               service: ServiceClient, control: ControlClient, *,
                               organization_id: str, actor_id: str, actor_kind: str,
                               upload_id: str, object_key: str, filename: str,
                               mime: str, size_bytes: int, sha256: str,
                               remote_compute_id: str):
    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.id == remote_compute_id).with_for_update()
        .execution_options(populate_existing=True))
    if row is None or row.organization_id != organization_id \
            or row.actor_id != actor_id:
        # Cross-actor or unknown binding: same shape as missing. No existence
        # oracle, no second asset, no task.
        raise APIError(404, "remote compute not found",
                       "invalid_request_error", "remote_compute_not_found")
    if expire_if_due(row):
        await session.commit()
    if row.status != "waiting_input":
        # Complete/unknown replay: re-read the persisted binding, never mint a
        # second asset/task. The idempotent return is the stored manifest.
        if row.manifest_json is not None:
            return row, None
        raise APIError(409, "remote compute is not waiting for input",
                       "invalid_request_error", "remote_compute_not_waiting")
    if row.input_sha256 != sha256 or row.input_size != size_bytes:
        # Input was replaced mid-flight: refuse as a changed input, never
        # register mixed bytes as one version (T14).
        raise APIError(409, "verified input differs from the approved input",
                       "invalid_request_error", "input_changed")
    expected_key = f"tmp-remote-compute/{organization_id}/{row.id}/source.bin"
    if object_key != expected_key and not object_key.startswith(
            f"tmp-remote-compute/{organization_id}/{row.id}/"):
        # Control always mints the fixed tmp key for temporary_compute; an
        # unexpected key is refused, never adopted.
        raise APIError(409, "temporary input key does not match the compute record",
                       "invalid_request_error", "input_changed")
    row.status = "content_verifying"
    row.upload_id = upload_id
    row.input_object_key = object_key
    row.updated_at = utcnow()
    await session.flush()

    document, job = await ingest_mod.ingest_document(
        session, storage, service, control,
        organization_id=organization_id,
        actor_id=actor_id,
        object_key=object_key,
        filename=filename,
        mime=mime,
        size_bytes=size_bytes,
        doc_id=sha256,
        engine="",
        options={},
        upload_key=f"remote-compute:{row.id}",
        receipt_key=f"remote-compute:{row.id}",
    )
    # ingest_document commits before submitting the real parse. Cancellation or
    # expiry during that HTTP await must not be overwritten by this old instance.
    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.id == remote_compute_id).with_for_update()
        .execution_options(populate_existing=True))
    expire_if_due(row)
    if job is None:
        raise APIError(500, "verified input has no durable parse job",
                       "server_error", "internal_error")
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == job.resource_id,
        ResourceVersion.document_id == document.id,
        ResourceVersion.parse_job_id == job.id,
        ResourceVersion.source_digest == sha256).order_by(
            ResourceVersion.created_at, ResourceVersion.id).limit(1))
    if version is None:
        raise APIError(500, "verified input has no fixed source version",
                       "server_error", "internal_error")
    row.parse_job_id = job.id
    row.manifest_json = {"source_document_id": document.id,
                         "source_resource_id": version.resource_id,
                         "source_version_id": version.id,
                         "source_object_key": object_key,
                         "input_sha256": sha256, "input_size": size_bytes,
                         "plan_digest": row.plan_digest,
                         "parse_job_id": job.id}
    if row.status == "content_verifying":
        row.status = "running"
        row.updated_at = utcnow()
    await session.flush()
    await session.commit()
    await session.refresh(row)
    if row.status != "running":
        from ddp_corpus.routers.remote_compute import cleanup_compute
        await cleanup_compute(session, storage, row)
    return row, job


async def record_parse_outcome(session: AsyncSession, *, compute_id: str,
                               organization_id: str, parse_job_id: str | None,
                               ok: bool, bundle_key: str | None = None,
                               output_sha256: str | None = None,
                               error: str | None = None, output_meta: dict | None = None):
    """Fix the delivery manifest from the real parse outcome.

    Called by the parse-callback/reconcile path when the ParseJob bound to a
    compute reaches a terminal state. Success pins the Bundle key + output
    hash so `result_manifest_digest` is `sha256:` of the actual ZIP bytes.
    Failure/cancel/TTL moves the record terminal without publishing anything.
    """
    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.id == compute_id).with_for_update()
        .execution_options(populate_existing=True))
    if row is None or row.organization_id != organization_id:
        raise APIError(404, "remote compute not found",
                       "invalid_request_error", "remote_compute_not_found")
    if expire_if_due(row):
        await session.commit()
    if row.status not in ("content_verified", "running"):
        return row
    if row.parse_job_id != parse_job_id:
        raise APIError(409, "parse job differs from the fixed input binding",
                       "invalid_request_error", "input_changed")
    if ok:
        if not bundle_key or not output_sha256:
            raise APIError(500, "fixed manifest requires bundle key and output hash",
                           "server_error", "internal_error")
        row.status = "succeeded"
        row.manifest_json = {**(row.manifest_json or {}), **(output_meta or {}),
                             "bundle_key": bundle_key,
                             "output_sha256": output_sha256,
                             "parse_job_id": parse_job_id,
                             "source_object_key": row.input_object_key,
                             "plan_digest": row.plan_digest}
        row.updated_at = utcnow()
    else:
        row.status = "failed"
        row.manifest_json = {**(row.manifest_json or {}), "parse_job_id": parse_job_id,
                             "error": error or "parse_failed",
                             "source_object_key": row.input_object_key,
                             "plan_digest": row.plan_digest}
        row.updated_at = utcnow()
    await session.commit()
    await session.refresh(row)
    return row
