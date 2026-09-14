#!/usr/bin/env python3
"""Validate an operator-reviewed historical parse binding manifest; --apply commits.

This is an offline migration command, not an HTTP authorization endpoint. Operators
must first corroborate ownership using archived upload, membership and task records.
It never changes initiated_by, API key identity, billing, or existing fixed versions.
See docs/refactor/HISTORICAL-PARSE-RECOVERY-v3.md for the manifest and procedure.
"""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion, utcnow
from sqlalchemy import func, select


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {"version", "reviewed_by", "bindings"}:
        raise ValueError("manifest requires version, reviewed_by and bindings")
    if manifest["version"] != "ddp-parse-binding-recovery/1":
        raise ValueError("unsupported manifest version")
    reviewer = manifest["reviewed_by"]
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or len(reviewer) > 128
        or any(ord(char) < 32 for char in reviewer)
    ):
        raise ValueError("reviewed_by must identify the operator who verified the records")
    rows = manifest["bindings"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("bindings must contain 1–10000 records")
    keys = {
        "resource_id",
        "source_version_id",
        "parse_job_id",
        "document_id",
        "source_digest",
        "owner_id",
        "organization_id",
    }
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError("binding fields do not match the recovery contract")
        for field in keys - {"source_digest"}:
            value = row[field]
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,31}", value)
                or value.startswith("migration:")
            ):
                raise ValueError(f"invalid {field}")
        if not isinstance(row["source_digest"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", row["source_digest"]
        ):
            raise ValueError("source_digest must be the verified source SHA-256")
        if row["parse_job_id"] in seen:
            raise ValueError("a parse job may appear only once in a manifest")
        seen.add(row["parse_job_id"])
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def restore_manifest(manifest, *, apply=False, sessionmaker=None):
    digest = validate_manifest(manifest)
    maker = sessionmaker or get_sessionmaker()
    restored = existing = 0
    async with maker() as session:
        # Deterministic locks and one transaction serialize competing manifests
        # with resource registration and prevent partially applied recovery.
        for row in sorted(
            manifest["bindings"],
            key=lambda x: (x["document_id"], x["resource_id"], x["parse_job_id"]),
        ):
            document = await session.scalar(
                select(Document).where(Document.id == row["document_id"]).with_for_update()
            )
            resource = await session.scalar(
                select(Resource).where(Resource.id == row["resource_id"]).with_for_update()
            )
            job = await session.scalar(
                select(ParseJob).where(ParseJob.id == row["parse_job_id"]).with_for_update()
            )
            source = await session.get(ResourceVersion, row["source_version_id"])
            if (
                document is None
                or document.deleted_at is not None
                or document.origin != "web"
                or document.doc_id != row["source_digest"]
            ):
                raise ValueError("source document or digest does not match")
            if (
                resource is None
                or resource.deleted_at is not None
                or resource.copied_from
                or resource.owner_id != row["owner_id"]
                or resource.organization_id != row["organization_id"]
                or resource.organization_id.startswith("migration:")
            ):
                raise ValueError("resource ownership must be resolved before parse recovery")
            if (
                source is None
                or source.deleted_at is not None
                or source.resource_id != resource.id
                or source.document_id != document.id
                or source.source_digest != row["source_digest"]
            ):
                raise ValueError(
                    "fixed source version does not match the claimed resource and bytes"
                )
            if (
                job is None
                or job.document_id != document.id
                or job.status != "succeeded"
                or job.archived_at is None
                or not job.result_prefix
                or job.resource_id not in (None, resource.id)
                or job.initiated_by not in (None, resource.owner_id)
            ):
                raise ValueError("parse identity, archived state or recorded initiator conflicts")
            bindings = list(
                await session.scalars(
                    select(ResourceVersion).where(ResourceVersion.parse_job_id == job.id)
                )
            )
            previous = next(
                (
                    version
                    for version in bindings
                    if version.resource_id == resource.id
                    and version.deleted_at is None
                    and (version.binding_provenance or {}).get("method") == "operator_manifest"
                    and version.binding_provenance.get("manifest_digest") == digest
                ),
                None,
            )
            if previous:
                existing += 1
                continue
            if bindings:
                raise ValueError(
                    "parse already has a fixed binding; recovery cannot replace its provenance"
                )
            restored += 1
            if apply:
                number = (
                    await session.scalar(
                        select(func.max(ResourceVersion.version_no)).where(
                            ResourceVersion.resource_id == resource.id
                        )
                    )
                    or 0
                ) + 1
                stamp = utcnow()
                session.add(
                    ResourceVersion(
                        resource_id=resource.id,
                        version_no=number,
                        document_id=document.id,
                        source_digest=source.source_digest,
                        filename=source.filename,
                        size_bytes=source.size_bytes,
                        parse_job_id=job.id,
                        binding_provenance={
                            "method": "operator_manifest",
                            "manifest_digest": digest,
                            "claimed_actor_id": row["owner_id"],
                            "reviewed_by": manifest["reviewed_by"],
                            "recorded_at": stamp.isoformat(),
                            **row,
                        },
                    )
                )
                job.resource_id = resource.id
                await session.flush()
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return {
        "mode": "applied" if apply else "dry_run",
        "manifest_digest": digest,
        "new_bindings": restored,
        "existing_bindings": existing,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.manifest.stat().st_size > 5 * 1024 * 1024:
        parser.error("manifest exceeds 5 MiB")
    result = asyncio.run(restore_manifest(json.loads(args.manifest.read_text()), apply=args.apply))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
