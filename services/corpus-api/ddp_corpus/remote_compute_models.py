"""Persistent waiting-input remote file compute rows (corpus 0035).

The coordination record is written by corpus, never by the uploader. A record
starts in `waiting_input`: no real parse work is queued while input bytes are
still moving. Only a fully verified input digest (control finalize + streaming
sha256) may enqueue the existing real parse queue for the same actor/org with
the frozen input digest/plan digest/source identity. Temporary inputs never
enter the permanent public catalog; metadata pre-checks never impersonate
`content_verified`.

Outputs travel the existing Bundle/storage channel with a fixed manifest
(source/version/output hash). The local app downloads with resume, verifies,
atomically imports, then acks; lost/duplicate acks replay safely. TTL/cancel/
failure/ack states stay visible; cleanup of inputs and derived data keeps
every other live reference (reference-safe GC owns the deletion rule).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ddp_core.models import Base, as_aware, utcnow

# Execution completion is not delivery completion: succeeded retains its bytes
# until acknowledgement, cancellation, or the advertised expiry.
ACTIVE_REMOTE_COMPUTE = ("waiting_input", "content_verifying", "content_verified",
                         "running")
CLOSED_REMOTE_COMPUTE = ("failed", "expired", "cancelled", "acked")
UNCONFIRMED_REMOTE_COMPUTE = (*ACTIVE_REMOTE_COMPUTE, "succeeded")


def expire_if_due(row, *, at=None) -> bool:
    now = at or utcnow()
    if row.status in UNCONFIRMED_REMOTE_COMPUTE and row.expires_at is not None \
            and as_aware(row.expires_at) <= now:
        row.status = "expired"
        row.updated_at = now
        return True
    return False


class RemoteCompute(Base):
    """One idempotent waiting-input compute for an approved file plan."""

    __tablename__ = "remote_computes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="waiting_input", index=True)
    input_sha256: Mapped[str] = mapped_column(String(64))
    input_size: Mapped[int] = mapped_column(Integer, default=0)
    plan_digest: Mapped[str] = mapped_column(String(71), default="")
    source_identity: Mapped[dict] = mapped_column(JSON, default=dict)
    target_identity: Mapped[dict] = mapped_column(JSON, default=dict)
    retention: Mapped[str] = mapped_column(String(24), default="temporary")
    upload_id: Mapped[str | None] = mapped_column(String(128), default=None)
    parse_job_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    input_object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    manifest_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    output_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    request_digest: Mapped[str | None] = mapped_column(String(71), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       default=None)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key",
                         name="uq_remote_computes_org_idempotency"),
    )
