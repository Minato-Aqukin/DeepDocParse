"""Corpus-owned, bounded client projections and accepted-operation receipts."""
from datetime import datetime
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from ddp_core.models import Base, utcnow


class ClientView(Base):
    __tablename__ = "client_views"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    cursor: Mapped[str] = mapped_column(String(64), default="")


class ClientSnapshot(Base):
    __tablename__ = "client_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[dict] = mapped_column(JSON)
    bindings: Mapped[list] = mapped_column(JSON)
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ClientPage(Base):
    __tablename__ = "client_pages"
    cursor: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), ForeignKey("client_snapshots.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[dict] = mapped_column(JSON)


class ClientReceipt(Base):
    __tablename__ = "client_receipts"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(32))
    principal_id: Mapped[str] = mapped_column(String(32))
    request_digest: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(32))
    version_id: Mapped[str] = mapped_column(String(32))
    parse_job_id: Mapped[str] = mapped_column(String(32))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
