"""Scope bundle-origin document reuse to one organization.

`(doc_id, origin)` was globally unique, so a bundle import in organization B whose
source digest matched organization A's document had to reuse (and repoint) A's row.
The key now includes `organization_id`; every existing row already satisfies the
wider key because the narrower one was unique.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_documents_doc_origin", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_doc_origin_org", "documents", ["doc_id", "origin", "organization_id"])


def downgrade():
    # Restoring the global key fails on rows that are only unique per organization.
    # Refuse explicitly instead of letting ADD CONSTRAINT abort halfway through.
    shared = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM (SELECT 1 FROM documents GROUP BY doc_id, origin "
        "HAVING count(*) > 1) AS duplicated")).scalar_one()
    if shared:
        raise RuntimeError(
            f"refusing downgrade: {shared} (doc_id, origin) keys are now held by more than one "
            "organization; resolve them before restoring uq_documents_doc_origin")
    op.drop_constraint("uq_documents_doc_origin_org", "documents", type_="unique")
    op.create_unique_constraint("uq_documents_doc_origin", "documents", ["doc_id", "origin"])
