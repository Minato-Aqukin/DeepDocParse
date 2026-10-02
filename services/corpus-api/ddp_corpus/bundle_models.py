"""Authorized Bundle replicas: the licensed-copy ledger, not a new permission.

A replica row is created for an import the caller already owns: it records
the source authority/origin identity, the fixed local version/digest it
snapshots, the owning actor, and the source policy revision plus the
term/revocation state. Reads reuse the fixed version authorization and the
stored snapshot bytes — a replica never invents a permission of its own.
Revoked or expired replicas block reads; an offline original stays readable
only through the fixed snapshot of a still-valid replica.

Tables live in `database/corpus/alembic/versions/0034_bundle_replicas.py`.
Wiki model/routers are untouched here: imported Wiki stays a draft and is
materialized by the federated Wiki path (`federated_wiki.py`), never by the
replica ledger.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ddp_contracts.enums import BundleReplicaAvailability
from ddp_core.models import Base, as_aware, new_id, utcnow


class BundleReplica(Base):
    """One authorized copy of a fixed local resource version."""

    __tablename__ = "bundle_replicas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    resource_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    source_version_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("resource_versions.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    created_by: Mapped[str] = mapped_column(String(32))
    origin_node_id: Mapped[str] = mapped_column(String(64))
    authority_node_id: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    policy_revision: Mapped[str] = mapped_column(String(512))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "source_version_id", "owner_id",
                         name="uq_bundle_replicas_version_owner"),
    )


class BundleReplicaRevokeKey(Base):
    """Idempotency ledger for `POST .../replicas/{id}/revoke`."""

    __tablename__ = "bundle_replica_revoke_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    replica_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bundle_replicas.id", ondelete="CASCADE"), index=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "actor_id", "idempotency_key",
                         name="uq_bundle_revoke_actor_key"),
    )


#: Revocation is also an event payload, not only a row mutation.
REVOKE_EVENT_KIND = "bundle_replica_revoked"

#: Placeholder rows are never created: a revoke key without a replica id is a bug.
REVOKE_KEY_DIGEST_FIELDS = ("replica_id", "resource_id", "source_version_id", "revoked_at")


def revoke_request_digest(payload: dict) -> str:
    """Stable digest binding the revoke idempotency key to its exact request."""
    import hashlib
    import json

    payload = {key: payload.get(key) for key in REVOKE_KEY_DIGEST_FIELDS}
    if payload["revoked_at"] is not None:
        payload["revoked_at"] = as_aware(datetime.fromisoformat(payload["revoked_at"])).isoformat()
    return hashlib.sha256(json.dumps(
        payload,
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def replica_out(row: BundleReplica, *, availability: BundleReplicaAvailability) -> dict:
    """Public licensed-copy shape with timezone-stable terms."""
    return {
        "replica_id": row.id,
        "resource_id": row.resource_id,
        "source_version_id": row.source_version_id,
        "authority_node_id": row.authority_node_id,
        "origin_node_id": row.origin_node_id,
        "source_digest": "sha256:" + row.source_digest,
        "policy_revision": row.policy_revision,
        "owner_id": row.owner_id,
        "valid_until": as_aware(row.valid_until).isoformat() if row.valid_until else None,
        "revoked_at": as_aware(row.revoked_at).isoformat() if row.revoked_at else None,
        "availability": availability,
    }


def replica_is_live(row: BundleReplica, now: datetime) -> bool:
    """A replica authorizes new reads only while unrevoked and unexpired."""
    if row.revoked_at is not None:
        return False
    valid_until = row.valid_until
    if valid_until is not None and getattr(valid_until, "tzinfo", None) is None:
        from datetime import timezone
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    return valid_until is None or valid_until > now


def physical_delete_guard(row: BundleReplica) -> None:
    """Physical deletion must keep every actually referenced snapshot.

    The replica ledger never deletes snapshot bytes itself: the shared
    reference-safe GC owns that rule. Callers check live versions, active
    tasks and dependency manifests before removing anything.
    """
    if row.revoked_at is None:
        raise ValueError("a live replica cannot be physically deleted")
