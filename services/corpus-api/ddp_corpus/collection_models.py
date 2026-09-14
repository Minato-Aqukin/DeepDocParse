"""Corpus-owned explicit collection publication and scoped directory snapshots."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ddp_core.models import Base, new_id, utcnow


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    publication: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionMember(Base):
    __tablename__ = "collection_members"
    collection_id: Mapped[str] = mapped_column(String(32), ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(32), ForeignKey("resource_versions.id"), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(32), ForeignKey("resources.id"))
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"))
    parse_job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    source_digest: Mapped[str] = mapped_column(String(64))


class CollectionReceipt(Base):
    __tablename__ = "collection_receipts"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    collection_id: Mapped[str] = mapped_column(String(32), ForeignKey("collections.id"))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionCatalogView(Base):
    __tablename__ = "collection_catalog_views"
    binding: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    fingerprint: Mapped[str] = mapped_column(String(64))


class CollectionCatalogSnapshot(Base):
    __tablename__ = "collection_catalog_snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    binding: Mapped[str] = mapped_column(String(64), index=True)
    scope_id: Mapped[str] = mapped_column(String(128))
    caller_scope_hash: Mapped[str] = mapped_column(String(71))
    origin_node_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    page_size: Mapped[int] = mapped_column(Integer)
    descriptors: Mapped[list] = mapped_column(JSON)
    index_readiness: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CollectionCatalogPage(Base):
    __tablename__ = "collection_catalog_pages"
    cursor: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(String(32), ForeignKey("collection_catalog_snapshots.id", ondelete="CASCADE"), index=True)
    offset: Mapped[int] = mapped_column(Integer)
