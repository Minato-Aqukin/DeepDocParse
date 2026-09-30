"""Persist completed temporary-compute cleanup without mutating the fixed manifest."""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("remote_computes", sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("remote_computes", "cleaned_at")
