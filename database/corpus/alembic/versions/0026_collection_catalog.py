"""Explicit collections and caller-bound stable catalog pages; no private backfill."""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("collections", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("metadata_json", sa.JSON, nullable=False),
        sa.Column("publication", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    for name in ("organization_id", "owner_id", "publication"):
        op.create_index("ix_collections_"+name, "collections", [name])
    op.create_table("collection_members",
        sa.Column("collection_id", sa.String(32), sa.ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version_id", sa.String(32), sa.ForeignKey("resource_versions.id"), primary_key=True),
        sa.Column("resource_id", sa.String(32), sa.ForeignKey("resources.id"), nullable=False),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("parse_job_id", sa.String(32)), sa.Column("source_digest", sa.String(64), nullable=False))
    op.create_table("collection_receipts", sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("collection_id", sa.String(32), sa.ForeignKey("collections.id"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("collection_catalog_views", sa.Column("binding", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False))
    op.create_table("collection_catalog_snapshots", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("binding", sa.String(64), nullable=False), sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("caller_scope_hash", sa.String(71), nullable=False),
        sa.Column("origin_node_id", sa.String(64), nullable=False), sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("page_size", sa.Integer, nullable=False), sa.Column("descriptors", sa.JSON, nullable=False),
        sa.Column("index_readiness", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False))
    for name in ("binding", "valid_until"):
        op.create_index("ix_collection_catalog_snapshots_"+name, "collection_catalog_snapshots", [name])
    op.create_table("collection_catalog_pages", sa.Column("cursor", sa.String(32), primary_key=True),
        sa.Column("snapshot_id", sa.String(32), sa.ForeignKey("collection_catalog_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("offset", sa.Integer, nullable=False))
    op.create_index("ix_collection_catalog_pages_snapshot_id", "collection_catalog_pages", ["snapshot_id"])


def downgrade():
    for name in ("collection_catalog_pages", "collection_catalog_snapshots", "collection_catalog_views",
                 "collection_receipts", "collection_members", "collections"):
        op.drop_table(name)
