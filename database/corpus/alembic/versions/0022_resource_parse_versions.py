"""Allow immutable parse revisions of the same source bytes within one asset."""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_resources_origin_binding", table_name="resource_versions")


def downgrade():
    # Downgrade refuses conflicting history instead of deleting fixed revisions.
    op.create_index("uq_resources_origin_binding", "resource_versions",
                    ["document_id", "resource_id"], unique=True)
