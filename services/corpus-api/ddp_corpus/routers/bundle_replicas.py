"""Licensed-copy replica directory, licensed-source reads and replica revocation.

All three routes live under the fixed resource/version prefix and start from
the same strict resource authorization (`_version`): a caller without access
to the version sees `resource_not_found`, whether the version is missing or
merely unauthorized. Revocation is an empty-body POST with an Idempotency-Key;
a revoked or expired replica authorizes no new reads and no cache peeking, and
replicas never delete bytes themselves -- the shared reference-safe GC owns
snapshot lifetimes (same-hash snapshots under other logical resources are
untouched because every replica row is scoped to its own resource/version).
`licensed-source` reuses the single `bundle_source.source_response` reader.
"""
from fastapi import APIRouter, Depends, Header, Request
from ddp_contracts.enums import BundleReplicaAvailability
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.bundle_models import (
    BundleReplica,
    BundleReplicaRevokeKey,
    replica_is_live,
    replica_out,
    revoke_request_digest,
)
from ddp_corpus.bundle_source import source_response
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.models import utcnow

router = APIRouter()


def _availability(row: BundleReplica) -> BundleReplicaAvailability:
    return "licensed_copy" if replica_is_live(row, utcnow()) else "unavailable"


@router.get("/api/resources/{resource_id}/versions/{version_id}/bundle/replicas")
async def list_replicas(
    resource_id: str,
    version_id: str,
    actor: Actor = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
):
    from ddp_corpus.routers.bundles import _version

    resource, version = await _version(session, actor, resource_id, version_id)
    rows = list(
        (
            await session.execute(
                select(BundleReplica)
                .where(
                    BundleReplica.organization_id == resource.organization_id,
                    BundleReplica.resource_id == resource.id,
                    BundleReplica.source_version_id == version.id,
                    BundleReplica.owner_id == resource.owner_id,
                    BundleReplica.source_digest == version.source_digest,
                )
                .order_by(BundleReplica.created_at, BundleReplica.id)
            )
        ).scalars()
    )
    return JSONResponse(
        {"replicas": [replica_out(row, availability=_availability(row)) for row in rows]},
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/resources/{resource_id}/versions/{version_id}/bundle/licensed-source")
async def licensed_source(
    resource_id: str,
    version_id: str,
    request: Request,
    actor: Actor = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
    storage=Depends(get_storage),
):
    return await source_response(
        resource_id,
        version_id,
        actor=actor,
        session=session,
        storage=storage,
        http=request.app.state.http,
    )


@router.post("/api/resources/{resource_id}/versions/{version_id}/bundle/replicas/{replica_id}/revoke")
async def revoke_replica(
    resource_id: str,
    version_id: str,
    replica_id: str,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128),
    actor: Actor = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
):
    from ddp_corpus.routers.bundles import _version

    actor.require(actor.can_upload and actor.principal_id is not None, "撤销授权副本")
    body = await request.body()
    if body:
        raise APIError(
            400, "revoke takes an empty body", "invalid_request_error", "revoke_body_not_allowed"
        )
    resource, version = await _version(session, actor, resource_id, version_id)
    row = await session.scalar(
        select(BundleReplica)
        .where(
            BundleReplica.id == replica_id,
            BundleReplica.organization_id == resource.organization_id,
            BundleReplica.resource_id == resource.id,
            BundleReplica.source_version_id == version.id,
            BundleReplica.owner_id == resource.owner_id,
            BundleReplica.source_digest == version.source_digest,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        # Same shape as "no access to the version": unauthorized callers cannot
        # probe replica ids across resources.
        raise APIError(
            404, "resource version not found", "invalid_request_error", "resource_not_found"
        )
    key = BundleReplicaRevokeKey(
        organization_id=actor.organization_id,
        actor_id=actor.principal_id,
        idempotency_key=idempotency_key,
        replica_id=row.id,
        request_digest="",
    )
    prior = await session.scalar(
        select(BundleReplicaRevokeKey).where(
            BundleReplicaRevokeKey.organization_id == key.organization_id,
            BundleReplicaRevokeKey.actor_id == key.actor_id,
            BundleReplicaRevokeKey.idempotency_key == key.idempotency_key,
        )
    )
    if prior is not None:
        if prior.replica_id != row.id:
            raise APIError(
                409,
                "idempotency key already used with a different revocation",
                "invalid_request_error",
                "idempotency_conflict",
            )
        expected = revoke_request_digest(
            {
                "replica_id": row.id,
                "resource_id": resource.id,
                "source_version_id": version.id,
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            }
        )
        if prior.request_digest != expected:
            raise APIError(
                409,
                "idempotency key already used with a different revocation",
                "invalid_request_error",
                "idempotency_conflict",
            )
        await session.refresh(row)
        return JSONResponse(
            {"replica": replica_out(row, availability=_availability(row))},
            headers={"Cache-Control": "private, no-store"},
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        row.updated_at = row.revoked_at
        await session.flush()
    key.request_digest = revoke_request_digest(
        {
            "replica_id": row.id,
            "resource_id": resource.id,
            "source_version_id": version.id,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        }
    )
    session.add(key)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        await session.refresh(row)
        prior = await session.scalar(
            select(BundleReplicaRevokeKey).where(
                BundleReplicaRevokeKey.organization_id == key.organization_id,
                BundleReplicaRevokeKey.actor_id == key.actor_id,
                BundleReplicaRevokeKey.idempotency_key == key.idempotency_key,
            )
        )
        if prior is not None:
            if prior.replica_id != row.id:
                raise APIError(
                    409,
                    "idempotency key already used with a different revocation",
                    "invalid_request_error",
                    "idempotency_conflict",
                ) from exc
            expected = revoke_request_digest(
                {
                    "replica_id": row.id,
                    "resource_id": resource.id,
                    "source_version_id": version.id,
                    "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
                }
            )
            if prior.request_digest != expected:
                raise APIError(
                    409,
                    "idempotency key already used with a different revocation",
                    "invalid_request_error",
                    "idempotency_conflict",
                ) from exc
            return JSONResponse(
                {"replica": replica_out(row, availability=_availability(row))},
                headers={"Cache-Control": "private, no-store"},
            )
        raise APIError(
            409,
            "concurrent revocation; retry with the same idempotency key",
            "invalid_request_error",
            "bundle_revoke_retry",
        ) from exc
    await session.refresh(row)
    return JSONResponse(
        {"replica": replica_out(row, availability=_availability(row))},
        headers={"Cache-Control": "private, no-store"},
    )
