"""Versioned Wiki workflow and immutable source manifests.

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.String(32), primary_key=True)


def _text(name, size=32, nullable=False):
    return sa.Column(name, sa.String(size), nullable=nullable)


def _json(name, default):
    return sa.Column(name, sa.JSON(), nullable=False, server_default=default)


def _revision():
    return sa.Column("revision_id", sa.String(32), sa.ForeignKey("wiki_revisions.id"), nullable=False)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    op.create_table("wikis", _id(), _text("organization_id"), _text("owner_id"),
                    _text("title", 255), _text("current_revision_id", nullable=True),
                    _text("published_revision_id", nullable=True), _created())
    op.create_table("wiki_revisions", _id(),
                    sa.Column("wiki_id", sa.String(32), sa.ForeignKey("wikis.id"), nullable=False),
                    _text("base_revision_id", nullable=True), _text("kind", 16),
                    _text("title", 255), _text("created_by"), _json("provider", "{}"),
                    _json("limits", "{}"), _json("merge_conflicts", "[]"), _created())
    op.create_table("wiki_pages", _id(), _revision(), _text("page_key", 64),
                    sa.Column("position", sa.Integer(), nullable=False), _text("title", 255),
                    _json("generated_sections", "[]"), _json("human_paragraphs", "[]"),
                    sa.UniqueConstraint("revision_id", "page_key", name="uq_wiki_pages_key"))
    op.create_table("wiki_dependencies", _id(), _revision(), _text("page_key", 64),
                    _text("resource_id"), _text("source_version_id"), _text("document_id"),
                    _text("source_digest", 64), _text("parse_revision"), _text("evidence_id"),
                    _text("excerpt_digest", 64), _json("locator", "{}"),
                    sa.UniqueConstraint("revision_id", "page_key", "resource_id", "evidence_id",
                                        name="uq_wiki_dependencies_binding"))
    op.create_table("wiki_claim_bindings", _id(), _revision(), _text("page_key", 64),
                    _text("claim_id"), _text("evidence_id"), _text("excerpt_digest", 64),
                    sa.UniqueConstraint("revision_id", "claim_id", "evidence_id",
                                        name="uq_wiki_claim_evidence"))
    op.create_table("wiki_human_edits", _id(), _revision(), _text("base_revision_id"),
                    _text("page_key", 64), _text("actor_id"), _json("before", "[]"),
                    _json("after", "[]"), _created())
    op.create_table("wiki_write_keys", _id(), _text("organization_id"), _text("actor_id"),
                    _text("idempotency_key", 128), _text("request_digest", 64), _revision(),
                    sa.UniqueConstraint("organization_id", "actor_id", "idempotency_key",
                                        name="uq_wiki_write_keys_actor_key"))
    for table, columns in {
        "wikis": ["organization_id", "owner_id"], "wiki_revisions": ["wiki_id"],
        "wiki_pages": ["revision_id"], "wiki_dependencies": ["revision_id", "resource_id", "evidence_id"],
        "wiki_claim_bindings": ["revision_id"], "wiki_human_edits": ["revision_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT COUNT(*) FROM wiki_revisions")).scalar():
        raise RuntimeError("0016 cannot downgrade with Wiki revisions; export the immutable audit first")
    for table in ("wiki_write_keys", "wiki_human_edits", "wiki_claim_bindings", "wiki_dependencies",
                   "wiki_pages", "wiki_revisions", "wikis"):
        op.drop_table(table)
