"""Reference-safe object collection with durable retry manifests.

Persist the exact deletion manifest before the first destructive call. Reacquire
the content row lock and recheck references after that commit, then hold it through
object deletion. A failed or interrupted sweep never loses its remaining keys.
"""

from datetime import timedelta

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ddp_corpus.config import settings
from ddp_corpus.models import (
    Citation,
    ClaimEvidenceBinding,
    DependencyManifest,
    Document,
    Evidence,
    ParseJob,
    Resource,
    ResourceVersion,
    Task,
    as_aware,
    utcnow,
)
from ddp_corpus.storage import Storage, job_result_prefix, prefix_of

ACTIVE_TASKS = ("queued", "claimed", "running")
ACTIVE_PARSES = ("pending", "running", "archiving")


def _live_versions(document_id):
    return exists(
        select(ResourceVersion.id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(
            ResourceVersion.document_id == document_id,
            ResourceVersion.deleted_at.is_(None),
            Resource.deleted_at.is_(None),
        )
    )


def _references(value, identities: set[str]) -> bool:
    if isinstance(value, str):
        return value in identities
    if isinstance(value, list):
        return any(_references(item, identities) for item in value)
    if isinstance(value, dict):
        return any(_references(item, identities) for item in value.values())
    return False


async def _protected(session, document, versions, jobs) -> bool:
    if await session.scalar(select(_live_versions(document.id))):
        return True
    if any(job.status in ACTIVE_PARSES for job in jobs):
        return True
    if await session.scalar(
        select(Citation.id)
        .join(Evidence, Evidence.id == Citation.evidence_id)
        .where(Evidence.document_id == document.id)
        .limit(1)
    ):
        return True
    # New Wiki revisions own references independently of the historical Citation table.
    if await session.scalar(
        select(DependencyManifest.id)
        .where(
            or_(
                DependencyManifest.document_id == document.id,
                DependencyManifest.source_version_id.in_([v.id for v in versions]),
            )
        )
        .limit(1)
    ):
        return True
    if await session.scalar(
        select(ClaimEvidenceBinding.id)
        .join(Evidence, Evidence.id == ClaimEvidenceBinding.evidence_id)
        .where(Evidence.document_id == document.id)
        .limit(1)
    ):
        return True
    identities = {
        document.id,
        *(v.id for v in versions),
        *(v.resource_id for v in versions),
        *(job.id for job in jobs),
    }
    tasks = (await session.execute(select(Task).where(Task.status.in_(ACTIVE_TASKS)))).scalars()
    for task in tasks:
        if task.kind == "gc":
            continue
        if _references(task.payload, identities):
            return True
        # Whole-corpus/unknown input scope cannot prove this document is unreferenced.
        if task.kind == "knowledge" and not task.payload.get("document_ids"):
            return True
        if not any(
            k in task.payload
            for k in (
                "document_id",
                "document_ids",
                "parse_job_id",
                "resource_version_id",
                "resource_version_ids",
            )
        ):
            return True
    return False


async def _load_locked(session, document_id):
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if document is None:
        return None, [], []
    versions = list(
        (
            await session.execute(
                select(ResourceVersion).where(ResourceVersion.document_id == document.id)
            )
        ).scalars()
    )
    jobs = list(
        (
            await session.execute(select(ParseJob).where(ParseJob.document_id == document.id))
        ).scalars()
    )
    return document, versions, jobs


async def _deletion_time(session, document, versions):
    stamps = [as_aware(document.deleted_at)] if document.deleted_at else []
    for version in versions:
        resource = await session.get(Resource, version.resource_id)
        ends = [
            as_aware(t)
            for t in (version.deleted_at, resource.deleted_at if resource else None)
            if t
        ]
        if ends:
            stamps.append(min(ends))
    return max(stamps) if stamps else None


async def _collect_keys(session, storage, document, versions, jobs):
    prefixes = {prefix for job in jobs for prefix in (prefix_of(job), job_result_prefix(job.id))}
    prefixes.update(v.bundle_prefix for v in versions if v.bundle_prefix == f"bundles/{v.id}/")
    other_jobs = list(
        (
            await session.execute(select(ParseJob).where(ParseJob.document_id != document.id))
        ).scalars()
    )
    shared_prefixes = {p for job in other_jobs for p in (prefix_of(job), job_result_prefix(job.id))}
    other_keys = set(
        (
            await session.execute(
                select(Document.object_key).where(
                    Document.id != document.id, Document.object_key != ""
                )
            )
        ).scalars()
    )
    keys = set(document.gc_pending_keys)
    for prefix in prefixes - shared_prefixes:
        if any(key.startswith(prefix) for key in other_keys):
            continue
        if (
            not prefix.startswith(("results/", "bundles/"))
            or not prefix.endswith("/")
            or ".." in prefix.split("/")
            or len(prefix.split("/")) != 3
        ):
            continue
        keys.update(key for key in await storage.list_prefix(prefix) if key.startswith(prefix))
    if document.object_key and document.object_key not in other_keys:
        keys.add(document.object_key)
    # Recheck old pending keys too: another document may now reference a formerly unique key.
    return sorted(
        key
        for key in keys
        if key not in other_keys and not any(key.startswith(prefix) for prefix in shared_prefixes)
    )


async def collect_deleted_objects(
    sessionmaker: async_sessionmaker, storage: Storage, limit: int = 20
) -> int:
    cleaned = 0
    cutoff = utcnow() - timedelta(seconds=settings.gc_grace_seconds)
    async with sessionmaker() as session:
        pending = func.json_array_length(Document.gc_pending_keys) > 0
        tombstoned = exists(
            select(ResourceVersion.id)
            .join(Resource)
            .where(
                ResourceVersion.document_id == Document.id,
                or_(ResourceVersion.deleted_at.is_not(None), Resource.deleted_at.is_not(None)),
            )
        )
        candidate_ids = list(
            (
                await session.execute(
                    select(Document.id)
                    .where(
                        or_(Document.object_key != "", pending),
                        or_(Document.deleted_at.is_not(None), tombstoned, pending),
                        ~_live_versions(Document.id),
                    )
                    .order_by(Document.created_at)
                    .limit(limit * 5)
                )
            ).scalars()
        )
        await session.rollback()
        for document_id in candidate_ids:
            if cleaned >= limit:
                break
            document, versions, jobs = await _load_locked(session, document_id)
            if document is None or await _protected(session, document, versions, jobs):
                await session.rollback()
                continue
            deleted_at = await _deletion_time(session, document, versions)
            if deleted_at is None or deleted_at > cutoff:
                await session.rollback()
                continue
            try:
                keys = await _collect_keys(session, storage, document, versions, jobs)
            except Exception as exc:
                document.gc_error = f"list_failed:{type(exc).__name__}"
                await session.commit()
                continue
            document.deleted_at = deleted_at
            # The exact old keys are now durable before object_key is cleared. A process
            # crash after any delete can repeat that idempotent delete on the next sweep.
            document.gc_pending_keys = keys
            document.gc_error = None
            document.object_key = ""
            await session.commit()

            # The manifest commit releases the row lock. A writer may have acquired a
            # new reference in that gap: recheck before deleting a single byte.
            document, versions, jobs = await _load_locked(session, document_id)
            if (
                document is None
                or document.deleted_at is None
                or await _protected(session, document, versions, jobs)
            ):
                await session.rollback()
                continue
            remaining = list(document.gc_pending_keys)
            for key in tuple(remaining):
                try:
                    await storage.delete(key)
                except Exception as exc:
                    document.gc_error = f"delete_failed:{type(exc).__name__}"
                    break
                remaining.remove(key)
            document.gc_pending_keys = remaining
            if not remaining:
                document.gc_error = None
                cleaned += 1
            await session.commit()
    return cleaned
