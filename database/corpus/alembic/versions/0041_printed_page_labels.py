"""Preserve PDF /PageLabels beside immutable physical evidence locators."""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chunks", sa.Column("printed_page_label", sa.Text(), nullable=True))
    op.add_column("evidence", sa.Column("printed_page_label", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("evidence", "printed_page_label")
    op.drop_column("chunks", "printed_page_label")
