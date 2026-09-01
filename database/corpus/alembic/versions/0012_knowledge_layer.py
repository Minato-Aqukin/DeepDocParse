"""DDP-Graph v1：实体、边、wiki 与复核队列

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _index(table: str, *columns: str) -> None:
    op.create_index(f"ix_{table}_{'_'.join(columns)}", table, list(columns))


def upgrade() -> None:
    op.create_table(
        "knowledge_entities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False, unique=True),
        sa.Column("entity_type", sa.String(32), nullable=False, server_default="other"),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("merged_by", sa.String(16), nullable=False, server_default="none"),
        sa.Column("merge_confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("entity_merge_uncertain", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("split_from_id", sa.String(32),
                  sa.ForeignKey("knowledge_entities.id"), nullable=True),
        sa.Column("review_state", sa.String(16), nullable=False,
                  server_default="unreviewed"),
        sa.Column("provider", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    for column in ("canonical_name", "normalized_name", "entity_type", "merged_by",
                   "entity_merge_uncertain", "split_from_id", "review_state"):
        _index("knowledge_entities", column)

    op.create_table(
        "graph_edges",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("subject_id", sa.String(32),
                  sa.ForeignKey("knowledge_entities.id"), nullable=False),
        sa.Column("predicate", sa.String(96), nullable=False),
        sa.Column("object_id", sa.String(32),
                  sa.ForeignKey("knowledge_entities.id"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unsupported", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("review_state", sa.String(16), nullable=False,
                  server_default="unreviewed"),
        sa.Column("provider", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("subject_id", "predicate", "object_id",
                            name="uq_graph_edges_spo"),
    )
    for column in ("subject_id", "predicate", "object_id", "confidence", "unsupported",
                   "review_state"):
        _index("graph_edges", column)

    op.create_table(
        "wiki_entries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("entity_id", sa.String(32), sa.ForeignKey("knowledge_entities.id"),
                  nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("outline", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provider", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    _index("wiki_entries", "entity_id")
    _index("wiki_entries", "title")
    op.create_table(
        "wiki_sections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("entry_id", sa.String(32),
                  sa.ForeignKey("wiki_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heading", sa.String(255), nullable=False),
        sa.UniqueConstraint("entry_id", "position", name="uq_wiki_sections_entry_position"),
    )
    _index("wiki_sections", "entry_id")
    op.create_table(
        "wiki_sentences",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("section_id", sa.String(32),
                  sa.ForeignKey("wiki_sections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("unsupported", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("conflict_group", sa.String(32), nullable=True),
        sa.Column("review_state", sa.String(16), nullable=False,
                  server_default="unreviewed"),
        sa.Column("provider", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("section_id", "position",
                            name="uq_wiki_sentences_section_position"),
    )
    for column in ("section_id", "unsupported", "conflict_group", "review_state"):
        _index("wiki_sentences", column)

    op.create_table(
        "knowledge_reviews",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("target_kind", sa.String(24), nullable=False),
        # extract_field 的键是 `{item_id}:{field_name}`，不能按普通 UUID 限成 32。
        sa.Column("target_id", sa.String(320), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("exported_revision", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    for column in ("target_kind", "target_id", "action", "reviewer_id",
                   "exported_revision", "created_at"):
        _index("knowledge_reviews", column)


def downgrade() -> None:
    conn = op.get_bind()
    counts = {table: conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
              for table in ("graph_edges", "wiki_sentences", "wiki_sections", "wiki_entries",
                            "knowledge_reviews", "knowledge_entities")}
    if any(counts.values()):
        detail = ", ".join(f"{table}={count}" for table, count in counts.items())
        raise RuntimeError(
            "0012 cannot downgrade after knowledge data was created; export the graph/wiki/"
            f"review audit trail first ({detail}).")
    for table in ("knowledge_reviews", "wiki_sentences", "wiki_sections", "wiki_entries",
                  "graph_edges", "knowledge_entities"):
        op.drop_table(table)
