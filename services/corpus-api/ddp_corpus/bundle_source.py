"""Read fixed original bytes without granting access from a digest or a locator."""
from fastapi.responses import Response
from minio.error import S3Error
from sqlalchemy import select

from ddp_core.bundle import MAX_FILE, MAX_MANIFEST, BundleError, digest, licence_valid_until, parse_json
from ddp_corpus.bundle_models import BundleReplica, replica_is_live
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


async def document_source_key(session, actor, document, storage, *, parse_job_id=None):
    """Resolve source bytes through the authorized logical version, not a shared hash."""
    from ddp_corpus.document_context import document_context
    from ddp_corpus.routers.bundles import _version

    context = await document_context(session, actor, document)
    if context.resource_id is None:
        return document.object_key
    version_id = context.version_id
    if parse_job_id is not None:
        version_id = await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.resource_id == context.resource_id,
            ResourceVersion.document_id == document.id,
            ResourceVersion.parse_job_id == parse_job_id,
            ResourceVersion.deleted_at.is_(None),
            *([ResourceVersion.id == actor.version_id] if actor.version_id else []),
        ).order_by(ResourceVersion.version_no.desc()).limit(1))
        if version_id is None:
            # Legacy native parse jobs can predate a fixed-version binding.
            return document.object_key
    resource, version = await _version(session, actor, context.resource_id, version_id)
    if version.bundle_prefix:
        key, _ = await licensed_source_binding(session, storage, resource, version)
        await _version(session, actor, resource.id, version.id)
        return key
    return document.object_key


async def source_response(resource_id, version_id, *, actor, session, storage, http):
    # Local imports avoid coupling router registration to this reusable reader.
    from ddp_corpus.routers.bundles import _bundle_error, _get_bytes, _version

    resource, version = await _version(session, actor, resource_id, version_id)
    document = await session.get(Document, version.document_id, populate_existing=True)
    if document is None or document.deleted_at is not None:
        raise _unavailable()
    expected_digest = "sha256:" + version.source_digest
    original_key, availability = document.object_key, "online"
    replica_binding = None
    try:
        if version.bundle_prefix:
            original_key, replica = await licensed_source_binding(session, storage, resource, version)
            availability = "offline_snapshot"
            replica_binding = (
                replica.id, replica.origin_node_id, replica.authority_node_id, replica.policy_revision,
                as_aware(replica.valid_until) if replica.valid_until else None,
            )
        if not original_key:
            raise _unavailable()
        content = await _get_bytes(storage, original_key, MAX_FILE)
        if len(content) != version.size_bytes or digest(content) != expected_digest:
            raise BundleError("bundle_storage_mismatch", "fixed original digest mismatch")
    except (KeyError, FileNotFoundError) as exc:
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
            or version.size_bytes != len(content)):
        raise _unavailable()
    if replica_binding is not None:
        current = await _licensed_replica(session, resource, version)
        if (current.id, current.origin_node_id, current.authority_node_id, current.policy_revision,
                as_aware(current.valid_until) if current.valid_until else None) != replica_binding:
            raise _unavailable()
    media = "application/pdf" if document.mime == "application/pdf" and content.startswith(b"%PDF-") else "application/octet-stream"
    return Response(content, media_type=media, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="source-{version.id}.bin"',
        "X-DDP-Source-Digest": expected_digest,
        "X-DDP-Source-Availability": availability,
        **({"X-DDP-Source-Licence-Valid-Until": replica_binding[-1].isoformat()}
           if replica_binding is not None and replica_binding[-1] is not None else {}),
    })
