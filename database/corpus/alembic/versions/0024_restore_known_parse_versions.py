"""Recover only historically proven parse ownership as immutable asset versions."""

import hashlib
import json
import re
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "resource_versions",
        sa.Column("binding_provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    restore_known_versions(op.get_bind())


def restore_known_versions(bind):
    """Append; never infer ownership from current_job or mutate an old version."""
    metadata = sa.MetaData()
    r, v, d, j = [
        sa.Table(name, metadata, autoload_with=bind)
        for name in ("resources", "resource_versions", "documents", "parse_jobs")
    ]
    resources = (
        bind.execute(
            sa.select(r)
            .where(
                r.c.deleted_at.is_(None),
                r.c.copied_from.is_(None),
                r.c.organization_id != "",
                ~r.c.organization_id.like("migration:%"),
            )
            .order_by(r.c.id)
        )
        .mappings()
        .all()
    )
    stamp = datetime.now(UTC)
    for resource in resources:
        # Follow normal asset registration's document -> resource lock order.
        document_ids = list(
            bind.scalars(
                sa.select(v.c.document_id)
                .where(v.c.resource_id == resource["id"])
                .distinct()
                .order_by(v.c.document_id)
            )
        )
        list(
            bind.execute(
                sa.select(d.c.id).where(d.c.id.in_(document_ids)).order_by(d.c.id).with_for_update()
            )
        )
        bind.execute(sa.select(r.c.id).where(r.c.id == resource["id"]).with_for_update())
        versions = (
            bind.execute(
                sa.select(v).where(v.c.resource_id == resource["id"]).order_by(v.c.version_no)
            )
            .mappings()
            .all()
        )
        already_bound = {row["parse_job_id"] for row in versions if row["parse_job_id"]}
        originals = {
            row["document_id"]: row
            for row in versions
            if row["deleted_at"] is None and row["parse_job_id"] is None
        }
        selected_document_id = next(
            (row["document_id"] for row in reversed(versions) if row["deleted_at"] is None), None
        )
        candidates = []
        for document_id, source in originals.items():
            document = (
                bind.execute(
                    sa.select(d).where(
                        d.c.id == document_id, d.c.deleted_at.is_(None), d.c.origin == "web"
                    )
                )
                .mappings()
                .first()
            )
            if (
                not document
                or resource["owner_id"] != document["uploaded_by"]
                or resource["organization_id"] != document["organization_id"]
                or not re.fullmatch(r"[a-f0-9]{64}", source["source_digest"] or "")
                or source["source_digest"] != document["doc_id"]
            ):
                continue
            jobs = (
                bind.execute(
                    sa.select(j)
                    .where(
                        j.c.document_id == document_id,
                        j.c.resource_id == resource["id"],
                        j.c.initiated_by == resource["owner_id"],
                        j.c.status == "succeeded",
                        j.c.archived_at.is_not(None),
                        j.c.result_prefix.is_not(None),
                        j.c.result_prefix != "",
                        j.c.created_at >= resource["created_at"],
                    )
                    .order_by(j.c.created_at, j.c.id)
                )
                .mappings()
                .all()
            )
            for job in jobs:
                candidates.append(
                    (
                        job,
                        source,
                        document_id == selected_document_id
                        and job["id"] == document["current_job_id"],
                    )
                )
        missing = [
            (job, source, current)
            for job, source, current in candidates
            if job["id"] not in already_bound
        ]
        if not missing:
            continue
        # Preserve a known selected revision even when it already had a version:
        # adding an older historical job must not silently change the default.
        selected = next((item for item in candidates if item[2]), None)
        if selected and selected[0]["id"] in already_bound:
            missing.append(selected)
        missing.sort(key=lambda item: (item[2], item[0]["created_at"], item[0]["id"]))
        next_no = max((row["version_no"] for row in versions), default=0)
        for job, source, current in missing:
            next_no += 1
            identity = {
                "resource_id": resource["id"],
                "parse_job_id": job["id"],
                "document_id": source["document_id"],
                "source_digest": source["source_digest"],
                "owner_id": resource["owner_id"],
                "organization_id": resource["organization_id"],
            }
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            bind.execute(
                v.insert().values(
                    id=hashlib.sha256(
                        f"0024:{resource['id']}:{job['id']}:{next_no}".encode()
                    ).hexdigest()[:32],
                    resource_id=resource["id"],
                    version_no=next_no,
                    document_id=source["document_id"],
                    source_digest=source["source_digest"],
                    filename=source["filename"],
                    size_bytes=source["size_bytes"],
                    parse_job_id=job["id"],
                    bundle_prefix="",
                    created_at=stamp,
                    binding_provenance={
                        "method": "migration:0024",
                        "manifest_digest": digest,
                        "claimed_actor_id": resource["owner_id"],
                        "reviewed_by": "migration:0024",
                        "recorded_at": stamp.isoformat(),
                        "preserved_current": current,
                        **identity,
                    },
                )
            )


def downgrade():
    # Removing this audit would erase the reason historical access was granted.
    # An operator must explicitly archive/reconcile the recovered versions first.
    bind = op.get_bind()
    metadata = sa.MetaData()
    versions = sa.Table("resource_versions", metadata, autoload_with=bind)
    if any(
        row and row.get("method") for row in bind.scalars(sa.select(versions.c.binding_provenance))
    ):
        raise RuntimeError("recovered version provenance exists; downgrade would erase its audit")
    op.drop_column("resource_versions", "binding_provenance")
