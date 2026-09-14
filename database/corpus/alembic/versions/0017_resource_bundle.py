"""Freeze resource parse revisions and retain verified Bundle snapshots.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resource_versions", sa.Column("parse_job_id", sa.String(32), nullable=True))
    op.add_column("resource_versions", sa.Column("bundle_prefix", sa.String(512),
                                                 nullable=False, server_default=""))
    op.create_index("ix_resource_versions_parse_job_id", "resource_versions", ["parse_job_id"])
    # Existing versions had no frozen parse identity. Do not invent one from a mutable
    # documents.current_job_id; their exports explicitly report parse_revision_unbound.


def downgrade() -> None:
    op.drop_index("ix_resource_versions_parse_job_id", table_name="resource_versions")
    op.drop_column("resource_versions", "bundle_prefix")
    op.drop_column("resource_versions", "parse_job_id")
