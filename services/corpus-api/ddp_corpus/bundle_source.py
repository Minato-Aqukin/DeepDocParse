"""Authorize fixed originals, then hand out short-lived direct-read URLs.

Licensed originals never travel through the app process (invariant 6): this module
authorizes against the fixed version + replica ledger, verifies size/digest with
HEAD/ranged reads, and returns a presigned redirect capped by the licence term.
"""
from fastapi.responses import RedirectResponse
from minio.error import S3Error
from sqlalchemy import select

from ddp_core.bundle import MAX_FILE, MAX_MANIFEST, BundleError, digest, licence_valid_until, parse_json
from ddp_corpus.bundle_models import BundleReplica, replica_is_live
from ddp_corpus.config import settings
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, ParseJob, ResourceVersion, as_aware, utcnow

def _unavailable():
    return APIError(410, "fixed original is unavailable", "invalid_request_error", "source_unavailable")


async def _licensed_replica(session, resource, version):
    row = await session.scalar(select(BundleReplica).where(
        BundleReplica.organization_id == resource.organization_id,
        BundleReplica.resource_id == resource.id,
        BundleReplica.source_version_id == version.id,
        BundleReplica.document_id == version.document_id,
        BundleReplica.owner_id == resource.owner_id,
        BundleReplica.source_digest == version.source_digest,
    ).execution_options(populate_existing=True))
    if row is None or not replica_is_live(row, utcnow()):
        raise _unavailable()
    return row


async def licensed_source_binding(session, storage, resource, version):
    """Authorize a snapshot's original, including its immutable source-issued term."""
    from ddp_corpus.routers.bundles import _bundle_error, _get_bytes

    prefix = f"bundles/{version.id}/"
    if version.bundle_prefix != prefix:
        raise _bundle_error(BundleError("bundle_binding_invalid", "invalid stored bundle binding"))
    replica = await _licensed_replica(session, resource, version)
    try:
        manifest = parse_json(await _get_bytes(storage, prefix + "manifest.json", MAX_MANIFEST))
        source = manifest.get("source") if isinstance(manifest, dict) else None
        if not isinstance(source, dict) or source.get("original") != "present":
            raise _unavailable()
        job = await session.get(ParseJob, version.parse_job_id, populate_existing=True)
        options = job.options if job is not None else {}
        if (source.get("source_digest") != "sha256:" + version.source_digest
                or source.get("origin_node_id") != replica.origin_node_id
                or source.get("authority_node_id") != replica.authority_node_id
                or source.get("policy_revision") != replica.policy_revision
                or source.get("resource_id") != options.get("source_resource_id")
                or source.get("source_version_id") != options.get("source_version_id")
                or licence_valid_until(source.get("licence_valid_until")) !=
                (as_aware(replica.valid_until) if replica.valid_until else None)):
            raise BundleError("bundle_binding_invalid", "licensed source binding changed")
    except (KeyError, FileNotFoundError) as exc:
        raise _unavailable() from exc
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise _unavailable() from exc
        raise APIError(503, "original storage is unavailable", "server_error", "source_unavailable") from exc
    except BundleError as exc:
        raise _bundle_error(exc) from exc
    # The storage read is not a grant cache; a revoke/expiry during it wins.
    replica = await _licensed_replica(session, resource, version)
    return prefix + "source.bin", replica


async def licensed_version(session, version):
    """The imported snapshot version whose licence governs `version`'s original bytes.

    A local reparse selected through `current-job` creates a new version of the same
    resource and document without a bundle prefix; its original is still the licensed
    copy. Soft-deleted snapshot versions still bind it (fail closed). None = native.
    """
    if version.bundle_prefix:
        return version
    return await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == version.resource_id,
        ResourceVersion.document_id == version.document_id,
        ResourceVersion.source_digest == version.source_digest,
        # Native versions store an empty prefix, not NULL.
        ResourceVersion.bundle_prefix.is_not(None), ResourceVersion.bundle_prefix != "",
    ).order_by(ResourceVersion.version_no).limit(1).execution_options(populate_existing=True))


async def live_source_key(session, resource, version, document):
    """Original key for background processing (indexing, compile vision) of `version`.

    Native: the document's object. Licensed copy (or a reparse of one): the snapshot's own
    `source.bin`, only while its replica is live — 410 otherwise. DB-only on purpose: this
    runs before every model request of a compile, and manifest binding is checked on the
    byte-serving paths.
    """
    snapshot = await licensed_version(session, version)
    if snapshot is None:
        return document.object_key
    if snapshot.bundle_prefix != f"bundles/{snapshot.id}/":
        raise _unavailable()
    await _licensed_replica(session, resource, snapshot)
    return snapshot.bundle_prefix + "source.bin"



async def document_source(session, actor, document, storage, *, parse_job_id=None):
    """`(object key, licence deadline)` of the authorized logical version's original.

    The deadline is the licensed replica's `valid_until` (None for native originals or
    unlimited licences); callers that hand out URLs must not let them outlive it.
    """
    from ddp_corpus.document_context import document_context
    from ddp_corpus.routers.bundles import _version

    context = await document_context(session, actor, document)
    if context.resource_id is None:
        return document.object_key, None
    version_id = context.version_id
    if parse_job_id is not None:
        bound = await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.resource_id == context.resource_id,
            ResourceVersion.document_id == document.id,
            ResourceVersion.parse_job_id == parse_job_id,
            ResourceVersion.deleted_at.is_(None),
            *([ResourceVersion.id == actor.version_id] if actor.version_id else []),
        ).order_by(ResourceVersion.version_no.desc()).limit(1))
        # A parse without its own fixed version (legacy, or a reparse not selected yet)
        # reads the same original as the authorized context version, licence included.
        version_id = bound or version_id
    resource, version = await _version(session, actor, context.resource_id, version_id)
    snapshot = await licensed_version(session, version)
    if snapshot is None:
        return document.object_key, None
    key, replica = await licensed_source_binding(session, storage, resource, snapshot)
    await _version(session, actor, resource.id, version.id)
    return key, as_aware(replica.valid_until) if replica.valid_until else None


async def document_source_key(session, actor, document, storage, *, parse_job_id=None):
    """Resolve source bytes through the authorized logical version, not a shared hash."""
    key, _ = await document_source(session, actor, document, storage, parse_job_id=parse_job_id)
    return key


def licence_ttl(deadline, cap_seconds: int) -> int:
    """Lifetime for a URL to a licensed original: never past the licence term."""
    if deadline is None:
        return cap_seconds
    remaining = int((deadline - utcnow()).total_seconds())
    if remaining < 1:
        raise _unavailable()
    return min(cap_seconds, remaining)


async def source_response(resource_id, version_id, *, actor, session, storage):
    # Local imports avoid coupling router registration to this reusable reader.
    from ddp_corpus.routers.bundles import _bundle_error, _version

    resource, version = await _version(session, actor, resource_id, version_id)
    document = await session.get(Document, version.document_id, populate_existing=True)
    if document is None or document.deleted_at is not None:
        raise _unavailable()
    expected_digest = "sha256:" + version.source_digest
    original_key, availability = document.object_key, "online"
    replica_binding = None
    deadline = None
    snapshot = await licensed_version(session, version)
    try:
        if snapshot is not None:
            original_key, replica = await licensed_source_binding(session, storage, resource, snapshot)
            availability = "offline_snapshot"
            deadline = as_aware(replica.valid_until) if replica.valid_until else None
            replica_binding = (
                replica.id, replica.origin_node_id, replica.authority_node_id, replica.policy_revision,
                deadline,
            )
        if not original_key:
            raise _unavailable()
        # 不变式 6：原件不进应用进程。HEAD 验大小；摘要整份验只在 MAX_FILE 以内做，
        # 更大的原件以 control 签发时校验过的大小为准（上传即验，见 ingest）。
        size = await storage.stat_size(original_key)
        if size != version.size_bytes:
            raise BundleError("bundle_storage_mismatch", "fixed original size mismatch")
        if size <= MAX_FILE:
            content = await storage.get_limited(original_key, MAX_FILE)
            if len(content) != version.size_bytes or digest(content) != expected_digest:
                raise BundleError("bundle_storage_mismatch", "fixed original digest mismatch")
            pdf_hint = content.startswith(b"%PDF-")
        else:
            pdf_hint = (await storage.get_range(original_key, 0, 5)) == b"%PDF-"
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise _unavailable() from exc
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise _unavailable() from exc
        raise APIError(503, "original storage is unavailable", "server_error", "source_unavailable") from exc
    except BundleError as exc:
        raise _bundle_error(exc) from exc

    # Storage I/O is an authorization race boundary, not a grant cache.
    await session.refresh(resource)
    await session.refresh(version)
    await session.refresh(document)
    await _version(session, actor, resource_id, version_id)
    if (document.deleted_at is not None or version.source_digest != expected_digest[7:]
            or version.size_bytes != size):
        raise _unavailable()
    if replica_binding is not None:
        current = await _licensed_replica(session, resource, snapshot)
        if (current.id, current.origin_node_id, current.authority_node_id, current.policy_revision,
                as_aware(current.valid_until) if current.valid_until else None) != replica_binding:
            raise _unavailable()
    media = "application/pdf" if document.mime == "application/pdf" and pdf_hint else "application/octet-stream"
    url = await storage.presigned_get(
        original_key, expires_seconds=licence_ttl(deadline, settings.source_url_ttl_seconds),
        filename=f"source-{version.id}.bin", content_type=media)
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "X-DDP-Source-Digest": expected_digest,
        "X-DDP-Source-Availability": availability,
        **({"X-DDP-Source-Licence-Valid-Until": replica_binding[-1].isoformat()}
           if replica_binding is not None and replica_binding[-1] is not None else {}),
    }
    return RedirectResponse(url, status_code=302, headers=headers)
