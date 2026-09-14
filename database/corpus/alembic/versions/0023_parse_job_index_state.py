"""Own index state and fencing leases per fixed parse revision."""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    for name, type_, default, nullable in (
        ("index_status", sa.String(16), "none", False),
        ("index_error", sa.Text(), None, True),
        ("index_generation", sa.Integer(), "0", False),
        ("index_lease_until", sa.DateTime(timezone=True), None, True),
        ("compile_status", sa.String(16), "none", False),
        ("compile_degraded", sa.JSON(), "[]", False),
        ("compile_fingerprint", sa.String(64), "", False),
        ("layout_version", sa.String(32), "", False),
        ("code_detection", sa.String(16), "unavailable", False),
    ):
        op.add_column("parse_jobs", sa.Column(name, type_, server_default=default, nullable=nullable))
    for name in ("index_status", "index_lease_until", "compile_status", "compile_fingerprint", "code_detection"):
        op.create_index(f"ix_parse_jobs_{name}", "parse_jobs", [name])
    # Only the selected current job may inherit Document's prior cache state.
    op.execute(sa.text("""
        UPDATE parse_jobs AS j SET
          index_status = d.index_status, index_error = d.index_error,
          index_generation = d.index_generation, index_lease_until = d.index_lease_until,
          compile_status = d.compile_status, compile_degraded = d.compile_degraded,
          compile_fingerprint = d.compile_fingerprint, layout_version = d.layout_version,
          code_detection = d.code_detection
        FROM documents AS d WHERE d.current_job_id = j.id
    """))
    # Previously noncurrent completions were never scheduled. Recover them, without
    # claiming they were compiled or indexed merely because another job was ready.
    op.execute(sa.text("""
        UPDATE parse_jobs SET index_status = 'pending', compile_status = 'pending'
        WHERE status = 'succeeded' AND result_prefix IS NOT NULL
          AND index_status = 'none'
    """))


def downgrade():
    op.execute(sa.text("""
        UPDATE documents AS d SET
          index_status = j.index_status, index_error = j.index_error,
          index_generation = j.index_generation, index_lease_until = j.index_lease_until,
          compile_status = j.compile_status, compile_degraded = j.compile_degraded,
          compile_fingerprint = j.compile_fingerprint, layout_version = j.layout_version,
          code_detection = j.code_detection
        FROM parse_jobs AS j WHERE d.current_job_id = j.id
    """))
    for name in ("index_status", "index_lease_until", "compile_status", "compile_fingerprint", "code_detection"):
        op.drop_index(f"ix_parse_jobs_{name}", table_name="parse_jobs")
    for name in ("index_status", "index_error", "index_generation", "index_lease_until", "compile_status",
                 "compile_degraded", "compile_fingerprint", "layout_version", "code_detection"):
        op.drop_column("parse_jobs", name)
