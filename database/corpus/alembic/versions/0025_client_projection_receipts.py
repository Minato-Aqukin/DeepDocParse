"""Persistent caller-scoped client windows and accepted upload receipts."""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("client_views", sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer, nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("cursor", sa.String(64), nullable=False))
    op.create_table("client_snapshots", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(64), nullable=False), sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("state", sa.JSON, nullable=False), sa.Column("bindings", sa.JSON, nullable=False),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_client_snapshots_scope", "client_snapshots", ["scope"])
    op.create_index("ix_client_snapshots_expires_at", "client_snapshots", ["expires_at"])
    op.create_table("client_pages", sa.Column("cursor", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("client_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("body", sa.JSON, nullable=False))
    op.create_index("ix_client_pages_snapshot_id", "client_pages", ["snapshot_id"])
    op.create_table("client_receipts", sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False), sa.Column("principal_id", sa.String(32), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False), sa.Column("resource_id", sa.String(32), nullable=False),
        sa.Column("version_id", sa.String(32), nullable=False), sa.Column("parse_job_id", sa.String(32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    for name in ("client_receipts", "client_pages", "client_snapshots", "client_views"):
        op.drop_table(name)
