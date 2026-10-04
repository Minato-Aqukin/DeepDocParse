"""Recursive delegation share/report accounting and coverage provenance."""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("coverage_entries", sa.Column("reported_by", sa.String(64), nullable=True))
    op.create_table(
        "federation_delegation_consumption",
        sa.Column("root_task_id", sa.String(64), primary_key=True),
        sa.Column("step_id", sa.String(256), primary_key=True),
        sa.Column("reserved_json", sa.JSON(), nullable=False),
        sa.Column("reported_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("federation_delegation_consumption")
    op.drop_column("coverage_entries", "reported_by")
