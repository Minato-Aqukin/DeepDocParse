"""Once-per-plan-step root allowances (0039, after remote compute cleanup).

No foreign key to federation_requests: the independent spend transaction must
not acquire KEY SHARE on a request locked by the coordinator. The composite
primary key arbitrates retries and concurrent coordinators; reservation insert
and conditional root-ledger increment commit or roll back together.
Existing cost history is not reset or refunded. Historical charges cannot be
assigned to steps reliably, so there is no synthetic reservation backfill.
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "federation_root_reservations",
        sa.Column("root_task_id", sa.String(64), primary_key=True),
        sa.Column("reservation_key", sa.String(256), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM federation_root_reservations")).scalar()
    if count:
        raise RuntimeError(
            "0039 cannot downgrade with federation root reservations; "
            "export the reservation audit first")
    op.drop_table("federation_root_reservations")
