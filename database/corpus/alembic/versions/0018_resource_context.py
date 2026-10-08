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

# 回填分批：三张源表整表读会把宽行全装进内存；按主键 keyset 翻页，
# 每行 UPDATE 幂等（resource_id 已有值的不再覆盖），重跑收敛到同一结果。
_BATCH = 500


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

    total = 0
    last_id = ""
    c = tables["conversations"]
    while True:
        batch = bind.execute(
            sa.select(c).where(c.c.resource_id.is_(None), c.c.id > last_id)
            .order_by(c.c.id).limit(_BATCH)).mappings().all()
        if not batch:
            break
        for row in batch:
            rid = original_asset(
                row["document_id"], row["actor_id"], row["organization_id"], row["created_at"]
            )
            if rid:
                bind.execute(c.update().where(c.c.id == row["id"]).values(resource_id=rid))
                total += 1
        last_id = batch[-1]["id"]
        print(f"[0018] conversations 已回填 {total} 条")
    total = 0
    last_id = ""
    j = tables["parse_jobs"]
    while True:
        batch = bind.execute(
            sa.select(j, d.c.uploaded_by, d.c.organization_id)
            .join(d, d.c.id == j.c.document_id)
            .where(j.c.resource_id.is_(None), j.c.id > last_id)
            .order_by(j.c.id).limit(_BATCH)).mappings().all()
        if not batch:
            break
        for row in batch:
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
                total += 1
        last_id = batch[-1]["id"]
        print(f"[0018] parse_jobs 已回填 {total} 条")
    runs, items = tables["extraction_runs"], tables["extraction_items"]
    scanned = 0
    last_id = ""
    while True:
        batch = bind.execute(
            sa.select(runs).where(runs.c.id > last_id)
            .order_by(runs.c.id).limit(_BATCH)).mappings().all()
        if not batch:
            break
        for row in batch:
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
        scanned += len(batch)
        last_id = batch[-1]["id"]
        print(f"[0018] extraction_runs 已回填 {scanned} 条")


def downgrade():
    op.drop_column("extraction_runs", "resource_context")
    op.drop_column("parse_jobs", "resource_id")
    op.drop_column("conversations", "resource_id")
