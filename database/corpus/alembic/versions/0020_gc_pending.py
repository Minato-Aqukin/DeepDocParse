"""Persist object collection retry manifests separately from content/index state.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("gc_pending_keys", sa.JSON(), nullable=False,
                                        server_default=sa.text("'[]'")))
    op.add_column("documents", sa.Column("gc_error", sa.Text(), nullable=True))


def downgrade() -> None:
    # Refuse to erase a live retry manifest: downgrade would permanently lose cleanup work.
    pending = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM documents WHERE json_array_length(gc_pending_keys) > 0"
    )).scalar_one()
    if pending:
        raise RuntimeError("finish pending object collection before downgrading 0020")
    op.drop_column("documents", "gc_error")
    op.drop_column("documents", "gc_pending_keys")
