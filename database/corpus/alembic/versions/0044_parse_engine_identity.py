"""Preserve complete registry engine identities in parse history.

Registry keys and HTTP contracts have no 32-character restriction. The old
column rejected valid configured engines before a submitted document became
visible. Widen without rewriting any existing identifier or options hash.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("parse_jobs") as batch:
        batch.alter_column("engine", existing_type=sa.String(32),
                           type_=sa.Text(), existing_nullable=False)


def downgrade():
    oversized = op.get_bind().execute(sa.text(
        "SELECT 1 FROM parse_jobs WHERE length(engine) > 32 LIMIT 1"
    )).first()
    if oversized:
        raise RuntimeError(
            "refusing downgrade: parse history contains registry engine identities "
            "longer than 32 characters; do not truncate or rename historical identities"
        )
    with op.batch_alter_table("parse_jobs") as batch:
        batch.alter_column("engine", existing_type=sa.Text(),
                           type_=sa.String(32), existing_nullable=False)
