"""Read fixed original bytes without granting access from a digest or a locator."""
from fastapi.responses import Response
from minio.error import S3Error
from sqlalchemy import select

from ddp_core.bundle import MAX_FILE, MAX_MANIFEST, BundleError, digest, parse_json
from ddp_corpus.bundle_models import BundleReplica, replica_is_live
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, utcnow


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
            prefix = f"bundles/{version.id}/"
            if version.bundle_prefix != prefix:
                raise BundleError("bundle_binding_invalid", "invalid stored bundle binding")
            replica = await _licensed_replica(session, resource, version)
            replica_binding = (replica.id, replica.origin_node_id, replica.authority_node_id, replica.policy_revision)
            manifest = parse_json(await _get_bytes(storage, prefix + "manifest.json", MAX_MANIFEST))
            source = manifest.get("source") if isinstance(manifest, dict) else None
            if not isinstance(source, dict) or source.get("original") != "present":
                raise _unavailable()
            if (source.get("source_digest") != expected_digest
                    or source.get("origin_node_id") != replica.origin_node_id
                    or source.get("authority_node_id") != replica.authority_node_id
                    or source.get("policy_revision") != replica.policy_revision):
                raise BundleError("bundle_binding_invalid", "licensed source binding changed")
            original_key, availability = prefix + "source.bin", "licensed_copy"
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
        if (current.id, current.origin_node_id, current.authority_node_id, current.policy_revision) != replica_binding:
            raise _unavailable()
    media = "application/pdf" if document.mime == "application/pdf" and content.startswith(b"%PDF-") else "application/octet-stream"
    return Response(content, media_type=media, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="source-{version.id}.bin"',
        "X-DDP-Source-Digest": expected_digest,
        "X-DDP-Source-Availability": availability,
    })
