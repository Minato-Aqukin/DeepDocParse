"""Freeze logical source authorization for old document-based workflows.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("resource_id", sa.String(32), nullable=True))
    op.add_column("parse_jobs", sa.Column("resource_id", sa.String(32), nullable=True))
    op.add_column(
        "extraction_runs",
        sa.Column("resource_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    backfill_contexts(op.get_bind())


def backfill_contexts(bind):
    """Only a unique original owner's asset can repair a historical binding.

    Public copies are not provenance. Ambiguous and quarantined rows remain
    unbound and the API fails closed until an audited migration supplies context.
    """
    metadata = sa.MetaData()
    tables = {
        name: sa.Table(name, metadata, autoload_with=bind)
        for name in (
            "resources",
            "resource_versions",
            "documents",
            "conversations",
            "parse_jobs",
            "extraction_runs",
            "extraction_items",
        )
    }
    r, v, d = (tables[name] for name in ("resources", "resource_versions", "documents"))

    def original_asset(document_id, actor_id, organization_id, created_at):
        ids = list(
            bind.scalars(
                sa.select(r.c.id)
                .join(v, v.c.resource_id == r.c.id)
                .where(
                    v.c.document_id == document_id,
                    r.c.owner_id == actor_id,
                    r.c.organization_id == organization_id,
                    r.c.organization_id != "migration:unresolved",
                    r.c.copied_from.is_(None),
                    r.c.created_at <= created_at,
                )
                .distinct()
            )
        )
        return ids[0] if len(ids) == 1 else None

    c = tables["conversations"]
    for row in bind.execute(sa.select(c).where(c.c.resource_id.is_(None))).mappings():
        rid = original_asset(
            row["document_id"], row["actor_id"], row["organization_id"], row["created_at"]
        )
        if rid:
            bind.execute(c.update().where(c.c.id == row["id"]).values(resource_id=rid))
    j = tables["parse_jobs"]
    for row in bind.execute(
        sa.select(j, d.c.uploaded_by, d.c.organization_id)
        .join(d, d.c.id == j.c.document_id)
        .where(j.c.resource_id.is_(None))
    ).mappings():
        # Only first-uploader jobs have a historically recorded organization.
        # 0006 merged other uploaders' jobs while leaving their initiator NULL.
        # The surviving document uploader is therefore never a fallback owner.
        actor_id = row["initiated_by"]
        if not actor_id or actor_id != row["uploaded_by"]:
            continue
        rid = original_asset(
            row["document_id"], actor_id, row["organization_id"], row["created_at"]
        )
        if rid:
            bind.execute(j.update().where(j.c.id == row["id"]).values(resource_id=rid))
    runs, items = tables["extraction_runs"], tables["extraction_items"]
    for row in bind.execute(sa.select(runs)).mappings():
        context = dict(row["resource_context"] or {})
        resources = dict(context.get("resources", {}))
        for document_id in bind.scalars(
            sa.select(items.c.document_id).where(items.c.run_id == row["id"]).distinct()
        ):
            rid = original_asset(
                document_id, row["actor_id"], row["organization_id"], row["created_at"]
            )
            if rid:
                resources.setdefault(document_id, rid)
        if resources:
            context.update(principal_id=row["actor_id"], resources=resources)
        bind.execute(runs.update().where(runs.c.id == row["id"]).values(resource_context=context))


def downgrade():
    op.drop_column("extraction_runs", "resource_context")
    op.drop_column("parse_jobs", "resource_id")
    op.drop_column("conversations", "resource_id")
