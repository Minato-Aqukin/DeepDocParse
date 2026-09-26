"""Remote file compute coordination rows (0035, after ResourceBundle 0034).

A persistent waiting-input compute record is created idempotently before any
byte moves. The input itself travels the existing /api/uploads multipart +
reconcile + finalize channel with purpose='temporary_compute'. Nothing is
queued for real parsing while the input is still being uploaded; only a fully
verified input digest enqueues the existing real parse queue. Temporary inputs
never enter the permanent public catalog. Outputs travel the existing
Bundle/storage channel; the local app verifies, atomically imports, then acks.
TTL/cancel/failure/ack states stay visible; cleanup of inputs and derived data
keeps every other live reference (reference-safe GC owns the deletion rule).

No backfill: rows are only written by the new remote-compute endpoints.
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remote_computes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False,
                  server_default="waiting_input"),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("input_size", sa.Integer, nullable=False),
        sa.Column("plan_digest", sa.String(71), nullable=False),
        sa.Column("source_identity", sa.JSON, nullable=False),
        sa.Column("target_identity", sa.JSON, nullable=False),
        sa.Column("retention", sa.String(24), nullable=False,
                  server_default="temporary"),
        sa.Column("upload_id", sa.String(128)),
        sa.Column("parse_job_id", sa.String(64)),
        sa.Column("input_object_key", sa.String(512)),
        sa.Column("manifest_json", sa.JSON),
        sa.Column("output_sha256", sa.String(64)),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_digest", sa.String(71)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "idempotency_key",
                            name="uq_remote_computes_org_idempotency"),
    )
    op.create_index("ix_remote_computes_org_status",
                    "remote_computes", ["organization_id", "status"])
    op.create_index("ix_remote_computes_expires_at",
                    "remote_computes", ["expires_at"])
    op.create_index("ix_remote_computes_parse_job",
                    "remote_computes", ["parse_job_id"])


def downgrade():
    op.drop_index("ix_remote_computes_parse_job", table_name="remote_computes")
    op.drop_index("ix_remote_computes_expires_at", table_name="remote_computes")
    op.drop_index("ix_remote_computes_org_status", table_name="remote_computes")
    op.drop_table("remote_computes")
