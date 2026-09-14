"""Separate parse attempts and revocable grants by logical asset.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("parse_jobs") as batch:
        batch.drop_constraint("uq_parse_jobs_doc_options", type_="unique")
        batch.create_unique_constraint("uq_parse_jobs_resource_options",
                                       ["document_id", "resource_id", "options_hash"])

def downgrade():
    duplicates = op.get_bind().execute(sa.text("""
        SELECT 1 FROM parse_jobs GROUP BY document_id, options_hash HAVING COUNT(*) > 1 LIMIT 1
    """)).first()
    if duplicates:
        raise RuntimeError("resource parse attempts cannot be collapsed safely; restore a pre-upgrade backup")
    with op.batch_alter_table("parse_jobs") as batch:
        batch.drop_constraint("uq_parse_jobs_resource_options", type_="unique")
        batch.create_unique_constraint("uq_parse_jobs_doc_options", ["document_id", "options_hash"])
