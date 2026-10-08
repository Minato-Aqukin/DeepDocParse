"""Reference-safe object collection with durable retry manifests.

Persist the exact deletion manifest before the first destructive call, claimed
with a conditional UPDATE (deleted_at IS NOT NULL) so a concurrent revival wins
the race. Reacquire the content row lock and recheck references after that
commit, then hold it through object deletion. A failed or interrupted sweep
never loses its remaining keys.
"""

from datetime import timedelta

from sqlalchemy import String, and_, cast, delete, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ddp_corpus.config import settings
from ddp_corpus.bundle_models import BundleReplica, BundleReplicaRevokeKey, replica_is_live
from ddp_corpus.models import (
    Chunk,
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

# Temporary compute originals use the same durable, reference-safe collector;
# the compute owner only reclaims otherwise unreferenced delivery artifacts.
REMOTE_COMPUTE_TMP_PREFIX = "tmp-remote-compute/"


def _dialect_name(session) -> str:
    bind = session.bind
    engine = getattr(bind, "sync_engine", None) or bind
    return getattr(getattr(engine, "dialect", None), "name", "") or ""


async def collect_terminal_upload(session, storage, *, object_key, eligible_at) -> bool:
    """Collect only an original with no corpus reference; control owns its claim.

    PostgreSQL fences *all* writers of original references with a table lock.
    Without that lock a concurrent bundle-import or compute-bind can land
    between the reference checks and the delete and lose live bytes, so
    non-PostgreSQL backends refuse loudly instead of deleting unfenced.
    """
    from ddp_corpus.errors import APIError
    from ddp_corpus.remote_compute_models import RemoteCompute

    if as_aware(eligible_at) > utcnow() - timedelta(seconds=settings.gc_grace_seconds):
        return False
    if await session.scalar(
        select(Document.id).where(Document.object_key == object_key).limit(1)
    ):
        return False
    if await session.scalar(
        select(RemoteCompute.id)
        .where(RemoteCompute.input_object_key == object_key).limit(1)
    ):
        return False
    # Pending manifests also protect keys after object_key has been cleared.
    # Do not require a tombstone: interrupted or revived rows can still own one.
    pending_keys = (await session.execute(
        select(Document.gc_pending_keys)
        .where(func.json_array_length(Document.gc_pending_keys) > 0)
    )).scalars()
    if any(object_key in keys for keys in pending_keys):
        return False
    # PostgreSQL fences *all* writers of original references with a table
    # lock before the destructive delete. Other backends have no equivalent
    # fence: the checks above are safe to run anywhere (they only refuse),
    # but deleting on their word alone would race a concurrent
    # bundle-import or compute-bind and lose live bytes — so refuse loudly.
    if _dialect_name(session) != "postgresql":
        raise APIError(409, "upload reclamation requires PostgreSQL table fencing",
                       "invalid_request_error", "reclamation_unsupported_dialect")
    await session.execute(text(
        "LOCK TABLE documents, remote_computes IN SHARE ROW EXCLUSIVE MODE"))
    # Recheck under the lock: a writer may have landed between the pre-checks
    # and the fence. Only the fenced verdict authorizes the delete below.
    if await session.scalar(
        select(Document.id).where(Document.object_key == object_key).limit(1)
    ):
        return False
    if await session.scalar(
        select(RemoteCompute.id)
        .where(RemoteCompute.input_object_key == object_key).limit(1)
    ):
        return False
    pending_keys = (await session.execute(
        select(Document.gc_pending_keys)
        .where(func.json_array_length(Document.gc_pending_keys) > 0)
    )).scalars()
    if any(object_key in keys for keys in pending_keys):
        return False
    # Arbitrary nested JSON references have no portable indexed membership
    # operator. Exclude null/empty candidates in SQL and project just the trees;
    # even closed computes can retain a source/output reference.
    trees = await session.execute(
        select(RemoteCompute.manifest_json, RemoteCompute.source_identity).where(
            or_(
                cast(RemoteCompute.manifest_json, String).not_in(("null", "{}")),
                cast(RemoteCompute.source_identity, String).not_in(("null", "{}")),
            )
        )
    )
    identities = {object_key}
    if any(_references(manifest, identities) or _references(source, identities)
           for manifest, source in trees):
        return False
    # Parse/result and fixed-version bundle prefixes are results/ and bundles/:
    # neither can reference the uploads/ or tmp-remote-compute/ keys admitted here.
    await storage.delete(object_key)
    return True


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


async def _remote_compute_protected(session, document) -> bool:
    """Temporary inputs stay while their compute is active or in grace window."""
    from ddp_corpus.remote_compute_models import (
        CLOSED_REMOTE_COMPUTE, UNCONFIRMED_REMOTE_COMPUTE, RemoteCompute,
    )
    from ddp_corpus.routers.remote_compute import TERMINAL_GRACE_SECONDS
    rows = (await session.execute(select(RemoteCompute).where(
        RemoteCompute.input_object_key == document.object_key))).scalars().all()
    if not rows:
        return False
    now = utcnow()
    for row in rows:
        if row.status in UNCONFIRMED_REMOTE_COMPUTE:
            return True
        if row.status in CLOSED_REMOTE_COMPUTE and row.cleaned_at is None and row.updated_at is not None:
            if (now - as_aware(row.updated_at)).total_seconds() < TERMINAL_GRACE_SECONDS:
                return True
    return False


async def _protected(session, document, versions, jobs) -> bool:
    if await session.scalar(select(_live_versions(document.id))):
        return True
    if document.object_key and document.object_key.startswith(REMOTE_COMPUTE_TMP_PREFIX):
        # The compute's execution, unacknowledged delivery, and grace period
        # protect its source even after its own resource has been tombstoned.
        if await _remote_compute_protected(session, document):
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
    # A live licensed-copy replica pins its fixed snapshot exactly like a live
    # version does: the GC must not collect `bundles/{version}/` bytes while a
    # live replica row references a still-live version's snapshot.
    # Revoked/expired rows protect nothing, and a deleted version/resource
    # leaves no readable licensed copy behind: deleted own replicas must not
    # become immortal GC roots. Scope is per logical resource/version, so a
    # live replica on one resource never pins another resource's same-hash
    # snapshot.
    live_resources = {
        r.id: r
        for r in (
            await session.execute(
                select(Resource).where(
                    Resource.id.in_([v.resource_id for v in versions])
                )
            )
        ).scalars()
    }
    live_version_ids = {
        v.id
        for v in versions
        if v.deleted_at is None
        and (live_resources.get(v.resource_id) is not None)
        and live_resources[v.resource_id].deleted_at is None
    }
    if live_version_ids:
        live_replica = select(BundleReplica.id).where(
            BundleReplica.source_version_id.in_(list(live_version_ids)),
            BundleReplica.resource_id.in_(
                [v.resource_id for v in versions if v.id in live_version_ids]
            ),
        )
        for row in (
            await session.execute(
                select(BundleReplica).where(BundleReplica.id.in_(live_replica))
            )
        ).scalars():
            if replica_is_live(row, utcnow()):
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


async def _drain_keys(session, storage, document, keys: list[str]) -> list[str]:
    """Delete keys, persisting partial progress. Never buffers object bytes."""
    remaining = list(keys)
    for key in tuple(remaining):
        try:
            await storage.delete(key)
        except Exception as exc:
            document.gc_error = f"delete_failed:{type(exc).__name__}"
            break
        remaining.remove(key)
    document.gc_pending_keys = remaining
    return remaining


async def _delete_chunks(session, document) -> None:
    # Cache text and vectors have no rebuildable source now. Audit
    # evidence and citations deliberately remain untouched.
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))


async def _claim_manifest(session, document, *, deleted_at, keys) -> bool:
    """Conditional-UPDATE claim (iron rule 6): the row must still be deleted.

    The UPDATE itself is the fence: it matches zero rows when a concurrent
    revival cleared deleted_at between lock and commit, and nothing is
    mutated in-session before it runs, so a racing revival is never
    overwritten by autoflush. Returns True when the claim won.
    Rows that were never stamped but are logically deleted through tombstoned
    versions/resources claim the live-to-deleted transition instead, with the
    same loser-rolls-back semantics.
    """
    parked = sorted(set(keys) | ({document.object_key} if document.object_key else set()))
    if document.deleted_at is None:
        claim = (
            update(Document)
            .where(Document.id == document.id, Document.deleted_at.is_(None))
            .values(deleted_at=deleted_at, gc_pending_keys=parked,
                    gc_error=None, object_key="")
        )
    else:
        claim = (
            update(Document)
            .where(Document.id == document.id, Document.deleted_at.is_not(None))
            .values(deleted_at=deleted_at, gc_pending_keys=parked,
                    gc_error=None, object_key="")
        )
    if (await session.execute(claim)).rowcount == 0:
        await session.rollback()
        return False
    document.deleted_at = deleted_at
    document.gc_pending_keys = parked
    document.gc_error = None
    document.object_key = ""
    return True


async def collect_deleted_objects(
    sessionmaker: async_sessionmaker, storage: Storage, limit: int = 20
) -> int:
    cleaned = 0
    cutoff = utcnow() - timedelta(seconds=settings.gc_grace_seconds)
    async with sessionmaker() as session:
        pending = func.json_array_length(Document.gc_pending_keys) > 0
        reclaimed_chunks = (
            Document.deleted_at.is_not(None)
            & (Document.object_key == "")
            & ~pending
            & exists(select(Chunk.id).where(Chunk.document_id == Document.id))
        )
        tombstoned = exists(
            select(ResourceVersion.id)
            .join(Resource)
            .where(
                ResourceVersion.document_id == Document.id,
                or_(ResourceVersion.deleted_at.is_not(None), Resource.deleted_at.is_not(None)),
            )
        )
        # Live rows can own a committed manifest (revive parks the superseded
        # key; a revival racing the manifest commit leaves it behind). They
        # are candidates for the live-row drain only — never for byte
        # collection while live.
        live_with_manifest = Document.deleted_at.is_(None) & pending
        candidate_ids = list(
            (
                await session.execute(
                    select(Document.id)
                    .where(
                        or_(
                            and_(
                                or_(Document.object_key != "", pending, reclaimed_chunks),
                                or_(Document.deleted_at.is_not(None), tombstoned, pending),
                                ~_live_versions(Document.id),
                            ),
                            live_with_manifest,
                        )
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
            if document is None:
                await session.rollback()
                continue
            # A live row can still own a committed manifest: revive parks the
            # superseded key (ingest._revive) and a revival racing the manifest
            # commit leaves it behind. Drain still-unreferenced manifest keys
            # against fresh references instead of leaking them forever.
            if document.deleted_at is None and (document.gc_pending_keys or []):
                live_keys = list(document.gc_pending_keys or [])
                try:
                    unreferenced = set(
                        await _collect_keys(session, storage, document, versions, jobs))
                except Exception as exc:
                    document.gc_error = f"list_failed:{type(exc).__name__}"
                    await session.commit()
                    continue
                # The live original is back in object_key, so _collect_keys may
                # re-add the live key itself: never drain the current original.
                unreferenced.discard(document.object_key)
                # _collect_keys re-adds pending keys only when still unreferenced;
                # keys another row now references are dropped from the manifest.
                # _drain_keys sets gc_pending_keys to the undrained remainder of
                # `doomed`; re-referenced keys (live_keys - unreferenced) must
                # leave the manifest without being deleted.
                doomed = sorted(set(live_keys) & unreferenced)
                await _drain_keys(session, storage, document, doomed)
                keep = set(document.gc_pending_keys) - (set(live_keys) - unreferenced)
                keep.discard(document.object_key)
                document.gc_pending_keys = sorted(keep)
                await session.commit()
                continue
            if await _protected(session, document, versions, jobs):
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
            # The exact old keys are now claimed before object_key is cleared. A
            # process crash after any delete can repeat that idempotent delete
            # on the next sweep. The UPDATE itself is the fence: nothing is
            # mutated in-session before it runs, so a racing revival is never
            # overwritten by autoflush.
            if not await _claim_manifest(session, document, deleted_at=deleted_at, keys=keys):
                # A revival raced the manifest commit and won the conditional
                # UPDATE: the claim loses, no byte is touched. The parked keys
                # stay owned by the live-row drain above.
                continue

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
            remaining = await _drain_keys(session, storage, document,
                                          list(document.gc_pending_keys))
            if not remaining:
                await _delete_chunks(session, document)
                document.gc_error = None
                cleaned += 1
            await session.commit()
    return cleaned
