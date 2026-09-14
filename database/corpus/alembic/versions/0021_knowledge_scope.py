"""Isolate legacy graph entities by author, organization and explicit source domain."""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_entities", sa.Column("scope_key", sa.String(64), nullable=False,
                                                  server_default="legacy"))
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("knowledge_entities"):
        if constraint["column_names"] == ["normalized_name"]:
            op.drop_constraint(constraint["name"], "knowledge_entities", type_="unique")
    for index in inspector.get_indexes("knowledge_entities"):
        if index["column_names"] == ["normalized_name"] and index.get("unique"):
            op.drop_index(index["name"], table_name="knowledge_entities")
            op.create_index(index["name"], "knowledge_entities", ["normalized_name"])
    op.create_index("ix_knowledge_entities_scope_key", "knowledge_entities", ["scope_key"])
    op.create_unique_constraint("uq_knowledge_entities_scope_name", "knowledge_entities",
                               ["scope_key", "normalized_name"])


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM knowledge_entities WHERE scope_key != 'legacy'"
    )).scalar()
    if count:
        raise RuntimeError("0021 cannot collapse owned knowledge scopes; export their audit first")
    op.drop_constraint("uq_knowledge_entities_scope_name", "knowledge_entities", type_="unique")
    op.drop_index("ix_knowledge_entities_scope_key", table_name="knowledge_entities")
    op.drop_column("knowledge_entities", "scope_key")
    op.create_unique_constraint("knowledge_entities_normalized_name_key", "knowledge_entities",
                               ["normalized_name"])
