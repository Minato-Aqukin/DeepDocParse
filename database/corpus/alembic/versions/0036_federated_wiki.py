"""Federated Wiki origin columns + 128-char cross-node identities (0036).

`wiki_dependencies` gains the foreign evidence envelope identity: origin,
authority, true source evidence id (`source_evidence_id`, the origin's own
id; `evidence_id` carries the stable internal `source_ref`), publication,
policy revision, derivative grant, retrieval receipt. Local rows keep these
columns NULL and keep using the existing bindings; foreign rows store the
envelope verbatim and MUST NOT invent a local `source_version_id`. Identity
cells carry the claimed foreign identity opaque at up to 128 chars (protocol
bound); only origin-local rows participate in local liveness joins.
Identity widening (32 -> 128, this revision, before any 0036 apply):
`wiki_dependencies.resource_id/source_version_id/document_id/parse_revision/
evidence_id` and `wiki_claim_bindings.claim_id/evidence_id` go 32 -> 128 so a
legal foreign id is never truncated into somebody else's identity.
`wiki_revisions.root_task_id` + `relations` and `wiki_write_keys.root_task_id`
bind a federated commit to its coordinator root task.
Backfill: existing rows get NULLs (local) — no invented origins. Downgrade
refuses when any federated column/root/relations is populated or any widened
cell exceeds 32 chars.
"""
import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


_WIDEN = (
    ("wiki_dependencies", "resource_id"),
    ("wiki_dependencies", "source_version_id"),
    ("wiki_dependencies", "document_id"),
    ("wiki_dependencies", "parse_revision"),
    ("wiki_dependencies", "evidence_id"),
    ("wiki_claim_bindings", "claim_id"),
    ("wiki_claim_bindings", "evidence_id"),
)


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table("wiki_dependencies") as batch:
        batch.add_column(sa.Column("origin_node_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("authority_node_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_evidence_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("source_publication", sa.String(16), nullable=True))
        batch.add_column(sa.Column("policy_revision", sa.String(128), nullable=True))
        batch.add_column(sa.Column("derivative_grant", sa.String(128), nullable=True))
        batch.add_column(sa.Column("retrieval_receipt_ref", sa.String(128), nullable=True))
    with op.batch_alter_table("wiki_revisions") as batch:
        batch.add_column(sa.Column("root_task_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("relations", sa.JSON(), nullable=True))
    with op.batch_alter_table("wiki_write_keys") as batch:
        batch.add_column(sa.Column("root_task_id", sa.String(64), nullable=True))
    for table, column in _WIDEN:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.String(32),
                               type_=sa.String(128), existing_nullable=False)
    # Existing rows are local: backfill is NULLs, not invented origins.
    if _has_table(bind, "wiki_dependencies"):
        bind.execute(sa.text(
            "UPDATE wiki_dependencies SET origin_node_id=NULL WHERE origin_node_id IS NULL"))
    op.create_index("ix_wiki_dependencies_origin", "wiki_dependencies", ["origin_node_id"])
    op.create_index("ix_wiki_revisions_root_task", "wiki_revisions", ["root_task_id"])
    op.create_index("ix_wiki_write_keys_root_task", "wiki_write_keys", ["root_task_id"])


def downgrade():
    bind = op.get_bind()
    if _has_table(bind, "wiki_dependencies"):
        federated = bind.execute(sa.text(
            "SELECT COUNT(*) FROM wiki_dependencies WHERE origin_node_id IS NOT NULL"
        )).scalar()
        if federated:
            raise RuntimeError(
                "0036 cannot downgrade with federated Wiki dependencies; "
                "export the original foreign proof first")
    if _has_table(bind, "wiki_revisions"):
        rooted = bind.execute(sa.text(
            "SELECT COUNT(*) FROM wiki_revisions WHERE root_task_id IS NOT NULL")).scalar()
        if rooted:
            raise RuntimeError(
                "0036 cannot downgrade with federated Wiki revisions; "
                "export the coordinator binding first")
    if _has_table(bind, "wiki_revisions"):
        related = bind.execute(sa.text(
            "SELECT COUNT(*) FROM wiki_revisions WHERE relations IS NOT NULL "
            "AND CAST(relations AS TEXT) NOT IN ('null', '[]', '{}', '')")).scalar()
        if related:
            raise RuntimeError(
                "0036 cannot downgrade with federated Wiki relations; "
                "export the coordinator binding first")
    if _has_table(bind, "wiki_write_keys"):
        rooted_keys = bind.execute(sa.text(
            "SELECT COUNT(*) FROM wiki_write_keys WHERE root_task_id IS NOT NULL")).scalar()
        if rooted_keys:
            raise RuntimeError(
                "0036 cannot downgrade with federated Wiki write keys; "
                "export the coordinator binding first")
    for table, column in _WIDEN:
        overlong = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM {table} WHERE LENGTH({column}) > 32")).scalar()
        if overlong:
            raise RuntimeError(
                f"0036 cannot downgrade with {table}.{column} values over 32 chars; "
                "narrowing would truncate cross-node identities")
    op.drop_index("ix_wiki_write_keys_root_task", table_name="wiki_write_keys")
    op.drop_index("ix_wiki_revisions_root_task", table_name="wiki_revisions")
    op.drop_index("ix_wiki_dependencies_origin", table_name="wiki_dependencies")
    for table, column in _WIDEN:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.String(128),
                               type_=sa.String(32), existing_nullable=False)
    with op.batch_alter_table("wiki_write_keys") as batch:
        batch.drop_column("root_task_id")
    with op.batch_alter_table("wiki_revisions") as batch:
        batch.drop_column("relations")
        batch.drop_column("root_task_id")
    with op.batch_alter_table("wiki_dependencies") as batch:
        batch.drop_column("retrieval_receipt_ref")
        batch.drop_column("derivative_grant")
        batch.drop_column("policy_revision")
        batch.drop_column("source_publication")
        batch.drop_column("source_evidence_id")
        batch.drop_column("authority_node_id")
        batch.drop_column("origin_node_id")
