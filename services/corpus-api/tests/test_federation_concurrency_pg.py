"""Real-PostgreSQL concurrency receipts for the P5 queue and P6 cache planes.

Every scenario here is one SQLite cannot express:

1. `FOR UPDATE SKIP LOCKED` claim races — two workers, one task, one winner;
2. generation fencing across *processes* — a task claimed in one session,
   cancelled in another, then written by the stale holder;
3. expired-lease reclaim — terminal rows are never re-claimed; the stale
   writer's generation is rejected even when its lease was valid when taken;
4. admission idempotency — a genuine pre-check race (both transactions miss the
   existing row, the advisory lock is bypassed) that only the unique constraint
   can arbitrate, plus the normal advisory-lock serialized race;
5. cache caps under parallel writers from independent sessions (the transaction
   advisory lock, not "our transaction is the only writer");
6. the sweeper on real PostgreSQL — expired leases and stalled coordinators get
   visible failure reasons while terminal rows and coverage denominators stay.

Gated on `FEDERATION_TEST_DATABASE_URL` (or `CORPUS_TEST_DATABASE_URL`) with a
loud skip; `scripts/check_federation_pg.sh` provides the scratch database.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from ddp_corpus import cache, federation, queue, reconcile
from ddp_corpus.cache import CacheLimits, FederationCacheEntry
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationAdmission,
    FederationExecution,
    FederationRequest,
)
from ddp_corpus.models import Task, new_id, utcnow
from ddp_core.search import PgVectorIndex
from pg_federation_server import federation_dsn, make_engine, make_factory
from test_federation_admissions import admission_body
from test_federation_probes import NODE, configure_federation

NODE_ORG = "org-race"


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_execution_inline", False)


@pytest.fixture
async def pg_db():
    dsn = federation_dsn()
    engine = make_engine(dsn)
    yield make_factory(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_queue(pg_db):
    """Keep the shared `tasks` table clean: `claim` has no organization filter.

    Without this, a queued `federation_execute` left behind by an earlier
    scenario can be claimed by the next one and make "exactly one winner"
    assertions pass for the wrong reason.
    """
    async def clean():
        async with pg_db() as session:
            await session.execute(delete(Task).where(
                Task.kind.in_(("federation_execute", "federation_plan"))))
            await session.commit()
    await clean()
    yield
    await clean()


def _task(**overrides) -> Task:
    values = {"id": new_id(), "kind": "federation_execute", "status": "queued",
              "organization_id": NODE_ORG, "payload": {}, "dedupe_key": None}
    values.update(overrides)
    return Task(**values)


def _admission(admission_id: str, *, executor_task_id: str | None, org: str = NODE_ORG,
               root_task_id: str = "root-1", key: str | None = None) -> FederationAdmission:
    return FederationAdmission(
        admission_id=admission_id, organization_id=org, actor_id="actor-alice",
        idempotency_key=key or f"key-{admission_id}",
        request_digest="sha256:" + "1" * 64, plan_digest="sha256:" + "2" * 64,
        root_task_id=root_task_id, step_id="retrieve-1", delegation_generation=0,
        issuer_node_id=NODE, executor_node_id=NODE, state="accepted",
        input_validation="content_verified", executor_task_id=executor_task_id,
        verified_input_manifest_digest="sha256:" + "3" * 64,
        effective_policy_ref="policy-1", receipt_json={}, receipt_revision=1,
        created_at=utcnow(), updated_at=utcnow())


# ---------------------------------------------------------------------------
# Queue claim / fencing
# ---------------------------------------------------------------------------

async def test_concurrent_claims_of_one_task_have_exactly_one_winner(pg_db):
    factory = pg_db
    task = _task()
    async with factory() as session:
        session.add(task)
        await session.commit()

    async def claim_once():
        async with factory() as session:
            return await queue.claim(session, ["federation_execute"], limit=1)

    first, second = await asyncio.gather(claim_once(), claim_once())
    winners = [claimed for batch in (first, second) for claimed in batch
               if claimed.id == task.id]
    assert len(winners) == 1, \
        f"SKIP LOCKED must give exactly one claimant: {first!r} / {second!r}"
    claimed = winners[0]
    assert claimed.status == "claimed" and claimed.generation == 1
    assert claimed.attempts == 1 and claimed.lease_until is not None


async def test_claim_skips_a_row_locked_by_another_transaction(pg_db):
    """SKIP LOCKED is load-bearing: a locked row is skipped, never waited on.

    Without it the candidate query waits for the other transaction's row lock
    (a stalled pool in production); with it the claimant moves on empty-handed.
    This is the deterministic counterpart of the two-claimant race above.
    """
    factory = pg_db
    task = _task()
    async with factory() as session:
        session.add(task)
        await session.commit()
    async with factory() as holder:
        await holder.execute(select(Task).where(Task.id == task.id).with_for_update())
        async with factory() as claimant:
            batch = await asyncio.wait_for(
                queue.claim(claimant, ["federation_execute"], limit=1), timeout=5)
            assert batch == [], "a row locked by another transaction must be skipped"
        await holder.rollback()
    async with factory() as session:
        row = await session.get(Task, task.id, populate_existing=True)
        assert row.status == "queued", "the holder never wrote; the row stays queued"


async def test_cancel_between_claim_and_succeed_fences_the_stale_writer(pg_db):
    factory = pg_db
    task_id = new_id()
    async with factory() as session:
        session.add(_task(id=task_id))
        await session.commit()

    async with factory() as session:
        claimed = await queue.claim(session, ["federation_execute"], limit=1)
    assert [task.id for task in claimed] == [task_id]
    stale_generation = claimed[0].generation

    # Session B cancels while the (conceptual) session A still holds the lease.
    async with factory() as session:
        assert await queue.cancel(session, task_id) is True
        assert await queue.cancel(session, task_id) is False, "cancel is idempotent"

    async with factory() as session:
        with pytest.raises(queue.StaleGeneration):
            await queue.succeed(session, task_id, stale_generation)

    async with factory() as session:
        row = await session.get(Task, task_id, populate_existing=True)
        assert row.status == "cancelled" and row.error == "cancelled"
        assert row.generation == stale_generation + 1
        assert row.dedupe_key is None and row.finished_at is not None
        current_generation = row.generation
    # The state guard is the second, independent gate: even the cancelled row's
    # *current* generation cannot be written by a late worker.
    async with factory() as session:
        with pytest.raises(queue.StaleGeneration):
            await queue.succeed(session, task_id, current_generation)


async def test_expired_lease_reclaim_reruns_and_terminal_rows_are_never_touched(pg_db):
    factory = pg_db
    now = utcnow()
    expired = now - timedelta(seconds=120)
    stale_id, queued_id, terminal_id = new_id(), new_id(), new_id()
    async with factory() as session:
        session.add_all([
            # A crashed worker's claim: lease long expired.
            _task(id=stale_id, status="claimed", generation=1, attempts=1,
                  claimed_by="dead-worker", lease_until=expired, run_after=expired),
            _task(id=queued_id),
            # A terminal row with an expired lease must stay invisible to claim.
            _task(id=terminal_id, status="succeeded", generation=4, attempts=2,
                  lease_until=expired, run_after=expired, degraded="partial"),
        ])
        await session.commit()

    async with factory() as session:
        claimed = await queue.claim(session, ["federation_execute"], limit=10)
    by_id = {task.id: task for task in claimed}
    assert set(by_id) == {stale_id, queued_id}, \
        f"terminal rows must never be reclaimed: {sorted(by_id)}"
    reclaimed = by_id[stale_id]
    assert reclaimed.generation == 2 and reclaimed.attempts == 2
    new_generation = reclaimed.generation

    # The old holder (generation 1) wakes up: its result is fenced.
    async with factory() as session:
        with pytest.raises(queue.StaleGeneration):
            await queue.succeed(session, stale_id, 1)
    async with factory() as session:
        await queue.succeed(session, stale_id, new_generation)

    async with factory() as session:
        with pytest.raises(queue.StaleGeneration):
            await queue.succeed(session, terminal_id, 4)
        row = await session.get(Task, terminal_id, populate_existing=True)
        assert row.status == "succeeded" and row.generation == 4
        assert row.degraded == "partial" and row.attempts == 2
    async with factory() as session:
        assert await queue.claim(session, ["federation_execute"], limit=10) == [], \
            "no active rows left after the reclaim; terminal rows stay unclaimed"


# ---------------------------------------------------------------------------
# Admission idempotency
# ---------------------------------------------------------------------------

async def test_admission_same_key_race_one_row_replay_and_conflict(pg_db, monkeypatch):
    factory = pg_db
    # Unique org/key per run: the scratch DB survives between pytest invocations,
    # and a leftover admission would turn this race into two replays. The org id
    # is bounded by the String(32) column, so keep the suffix short.
    org = "org-" + new_id()[:20]
    actor = Actor(id="actor-alice", kind="user", organization_id=org, role="contributor")
    body = admission_body(key="race-key")

    # Phase 1: a genuine pre-check race. Both transactions pass the SELECT
    # before either INSERTs; only the unique constraint can arbitrate. The
    # advisory lock is replaced by a barrier that releases both at once, so
    # this is deterministic rather than "usually overlapping".
    barrier = asyncio.Barrier(2)

    async def gated_lock(_session, _key):
        await barrier.wait()

    async def submit(payload):
        async with factory() as session:
            return await federation.admit(session, actor, payload, now=utcnow(),
                                          http=None, index=None)

    with monkeypatch.context() as scoped:
        scoped.setattr(federation.catalog, "lock_key", gated_lock)
        first, second = await asyncio.wait_for(
            asyncio.gather(submit(body), submit(body)), timeout=60)

    created = [flag for _, flag in (first, second)]
    assert sorted(created) == [False, True], \
        f"race must produce one creator and one replay, got {created}"
    winner = first[0] if first[1] else second[0]
    loser = second[0] if first[1] else first[0]
    assert loser == winner, "the loser must replay the winner's receipt, not re-run"

    async with factory() as session:
        admissions = list((await session.scalars(select(FederationAdmission).where(
            FederationAdmission.organization_id == org))).all())
        assert len(admissions) == 1
        assert admissions[0].receipt_json == winner
        executions = list((await session.scalars(select(FederationExecution).where(
            FederationExecution.admission_id == admissions[0].admission_id))).all())
        assert len(executions) == 1
        executor_task_id = executions[0].executor_task_id
        tasks = list((await session.scalars(select(Task).where(
            Task.kind == "federation_execute",
            Task.organization_id == org))).all())
        assert len(tasks) == 1, "exactly one queue task may survive the race"
        assert tasks[0].dedupe_key == f"federation-execution:{executor_task_id}"
        assert tasks[0].payload["executor_task_id"] == executor_task_id

    # Same key, different request digest -> explicit 409, never a second receipt.
    changed = admission_body(key="race-key", query="a different question")
    async with factory() as session:
        with pytest.raises(APIError) as excinfo:
            await federation.admit(session, actor, changed, now=utcnow(),
                                   http=None, index=None)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "idempotency_conflict"

    # Phase 2: the *real* advisory lock path (no barrier). Both calls race, the
    # lock serializes them, and the loser sees the committed winner.
    body2 = admission_body(key="race-key-advised")
    first2, second2 = await asyncio.wait_for(
        asyncio.gather(submit(body2), submit(body2)), timeout=60)
    assert sorted(flag for _, flag in (first2, second2)) == [False, True]
    assert first2[0] == second2[0]
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(FederationAdmission).where(
            FederationAdmission.organization_id == org))
        assert count == 2, "one admission per distinct idempotency key"


# ---------------------------------------------------------------------------
# Cache caps / purge / upsert under concurrency
# ---------------------------------------------------------------------------

async def test_cache_caps_hold_under_parallel_writer_sessions(pg_db):
    factory = pg_db
    limits = CacheLimits(entries=5, bytes=1200, ttl_seconds=60, per_scope_entries=2)
    now = utcnow()
    scopes = [f"pg-cap-{new_id()}" for _ in range(3)]

    async def writer(scope, index):
        async with factory() as session:
            await cache.put(session, scope_key=scope, cache_key=f"k{index}", kind="cap",
                            value={"payload": "x" * 500, "i": index}, ttl_seconds=60,
                            limits=limits, now=now + timedelta(milliseconds=index))
            await session.commit()

    await asyncio.gather(*(writer(scope, index)
                           for scope in scopes for index in range(4)))

    async with factory() as session:
        total, size = (await session.execute(select(
            func.count(FederationCacheEntry.id),
            func.coalesce(func.sum(FederationCacheEntry.bytes), 0)).where(
            FederationCacheEntry.scope_key.in_(scopes)))).one()
        per_scope = dict((await session.execute(select(
            FederationCacheEntry.scope_key,
            func.count(FederationCacheEntry.id)).where(
            FederationCacheEntry.scope_key.in_(scopes)).group_by(
            FederationCacheEntry.scope_key))).all())
        assert total <= limits.entries, f"{total} entries exceed the global cap"
        assert size <= limits.bytes, f"{size} bytes exceed the global cap"
        assert all(count <= limits.per_scope_entries for count in per_scope.values()), per_scope
        await cache.invalidate(session, kind="cap")
        await session.commit()


async def test_cache_expired_purge_and_concurrent_same_key_upsert(pg_db):
    factory = pg_db
    limits = CacheLimits(entries=100, bytes=1 << 20, ttl_seconds=60, per_scope_entries=100)
    scope = f"pg-purge-{new_id()}"
    base = utcnow()

    # Five entries that are valid at write time but expire a second later.
    async with factory() as session:
        for index in range(5):
            await cache.put(session, scope_key=scope, cache_key=f"expired-{index}",
                            kind="purge", value={"i": index}, ttl_seconds=1,
                            limits=limits, now=base)
        await session.commit()

    async with factory() as session:
        assert await cache.purge_expired(session, now=base + timedelta(seconds=5), limit=2) == 2
        await session.commit()
    async with factory() as session:
        assert await cache.purge_expired(session, now=base + timedelta(seconds=5), limit=10) == 3
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(
            FederationCacheEntry).where(FederationCacheEntry.scope_key == scope)) == 0

    # One row for one (scope, cache_key) no matter how many writers race.
    upsert_scope = f"pg-upsert-{new_id()}"

    async def upsert(value):
        async with factory() as session:
            await cache.put(session, scope_key=upsert_scope, cache_key="hot", kind="upsert",
                            value={"v": value}, ttl_seconds=60, limits=limits, now=utcnow())
            await session.commit()

    await asyncio.gather(upsert(1), upsert(2))
    async with factory() as session:
        rows = list((await session.scalars(select(FederationCacheEntry).where(
            FederationCacheEntry.scope_key == upsert_scope))).all())
        assert len(rows) == 1, "the unique (scope_key, cache_key) must not duplicate"
        assert rows[0].value_json in ({"v": 1}, {"v": 2})
        read = await cache.get(session, scope_key=upsert_scope, cache_key="hot", now=utcnow())
        assert read == rows[0].value_json and rows[0].hits == 1
        await cache.invalidate(session, scope_key=upsert_scope)
        await session.commit()


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------

async def test_sweeper_on_pg_fails_expired_lease_and_stalled_request(pg_db):
    factory = pg_db
    now = utcnow()
    expired = now - timedelta(seconds=120)
    stuck_old = now - timedelta(seconds=settings.federation_request_stuck_seconds + 60)
    stale_admission, stale_execution = new_id(), new_id()
    done_admission, done_execution = new_id(), new_id()
    stuck_root, done_root = f"stuck-{new_id()}", f"done-{new_id()}"
    plan_task_id, untouched_task_id = new_id(), new_id()

    async with factory() as session:
        session.add_all([
            _admission(stale_admission, executor_task_id=stale_execution,
                       root_task_id=stuck_root),
            FederationExecution(
                executor_task_id=stale_execution, admission_id=stale_admission,
                root_task_id=stuck_root, step_id="retrieve-1", operation="retrieve",
                state="running", generation=3, lease_until=expired,
                result_json={"spec": {}, "result": None},
                created_at=now, updated_at=now),
            _admission(done_admission, executor_task_id=done_execution,
                       root_task_id=done_root),
            FederationExecution(
                executor_task_id=done_execution, admission_id=done_admission,
                root_task_id=done_root, step_id="retrieve-1", operation="retrieve",
                state="succeeded", generation=5, lease_until=expired,
                result_json={"spec": {}, "result": {"sentinel": True}},
                created_at=now, updated_at=now),
            FederationRequest(
                root_task_id=stuck_root, organization_id=NODE_ORG, actor_id="actor-alice",
                task_spec_digest="sha256:" + "4" * 64, scope_id="scope-sweep",
                scope_digest="sha256:" + "5" * 64, search_mode="fast",
                planning_state="approved", status="running",
                created_at=stuck_old, updated_at=stuck_old),
            FederationRequest(
                root_task_id=done_root, organization_id=NODE_ORG, actor_id="actor-alice",
                task_spec_digest="sha256:" + "6" * 64, scope_id="scope-sweep",
                scope_digest="sha256:" + "7" * 64, search_mode="fast",
                planning_state="approved", status="succeeded",
                created_at=expired, updated_at=expired),
            CoverageLedger(
                root_task_id=stuck_root, scope_ref="scope-sweep", search_mode="fast",
                enumeration_state="sealed", retrieval_completeness="partial",
                evidence_sufficiency="insufficient",
                counts_json={"total_targets": 1, "succeeded": 0}, manifest_digest="",
                created_at=expired, updated_at=expired),
        ])
        await session.flush()
        session.add(CoverageEntry(
            root_task_id=stuck_root, target_digest="d" * 64,
            target_key_json={"origin_node_id": NODE, "collection_id": "col-1",
                             "operation": "corpus.retrieve"},
            query_digest="sha256:" + "8" * 64, state="in_flight",
            probe_refs_json=[], evidence_refs_json=[], used_budget_json={}, attempts=1))
        session.add_all([
            _task(id=plan_task_id, kind="federation_plan", payload={"root_task_id": stuck_root},
                  dedupe_key=f"federation-request:{stuck_root}", run_after=expired),
            _task(id=untouched_task_id, kind="federation_plan",
                  payload={"root_task_id": done_root},
                  dedupe_key=f"federation-request:{done_root}", run_after=expired),
        ])
        await session.commit()

    stats = await reconcile.sweep_federation_once(factory, now=now)
    # The sweep is a global scan over a scratch DB that survives between pytest
    # invocations, so a failed earlier run can leave other expired rows behind;
    # the per-row assertions below are the precise contract.
    assert stats["executions"] >= 1 and stats["requests"] >= 1 \
        and stats["cancelled_tasks"] >= 1, stats

    async with factory() as session:
        stale = await session.get(FederationExecution, stale_execution,
                                  populate_existing=True)
        assert stale.state == "failed" and stale.error == "lease_expired"
        assert stale.generation == 4 and stale.lease_until is None
        terminal = await session.get(FederationExecution, done_execution,
                                     populate_existing=True)
        assert terminal.state == "succeeded"
        assert terminal.result_json["result"] == {"sentinel": True}
        stuck = await session.get(FederationRequest, stuck_root, populate_existing=True)
        assert stuck.status == "failed" and stuck.error == "coordinator_stalled"
        done = await session.get(FederationRequest, done_root, populate_existing=True)
        assert done.status == "succeeded"
        # The coverage denominator survives the sweep (P5 §7.4).
        ledger = await session.get(CoverageLedger, stuck_root)
        entries = list((await session.scalars(select(CoverageEntry).where(
            CoverageEntry.root_task_id == stuck_root))).all())
        assert ledger is not None and ledger.counts_json["total_targets"] == 1
        assert [entry.state for entry in entries] == ["in_flight"]
        plan_task = await session.get(Task, plan_task_id, populate_existing=True)
        untouched = await session.get(Task, untouched_task_id, populate_existing=True)
        assert plan_task.status == "cancelled" and plan_task.dedupe_key is None
        assert untouched.status == "queued", "other roots' queue tasks stay untouched"

    assert await reconcile.sweep_federation_once(factory, now=now) == \
        {"executions": 0, "requests": 0, "cancelled_tasks": 0}


async def test_sweeper_on_pg_marks_queued_execution_whose_queue_task_died(pg_db):
    """A queued execution with a terminal queue task ends failed, not queued.

    The execution never had a lease, so the lease-expiry branch cannot see it;
    the payload JSON lookup (`->>` on PG) is what links it to the dead task.
    """
    factory = pg_db
    now = utcnow()
    admission_id, execution_id, task_id = new_id(), new_id(), new_id()
    async with factory() as session:
        session.add_all([
            _admission(admission_id, executor_task_id=execution_id,
                       root_task_id="dead-queue-root"),
            FederationExecution(
                executor_task_id=execution_id, admission_id=admission_id,
                root_task_id="dead-queue-root", step_id="retrieve-1", operation="retrieve",
                state="queued", generation=2, lease_until=None,
                result_json={"spec": {}, "result": None},
                created_at=now, updated_at=now),
            _task(id=task_id, status="failed", error="boom", attempts=1, max_attempts=1,
                  dedupe_key=None, payload={"executor_task_id": execution_id}),
        ])
        await session.commit()

    stats = await reconcile.sweep_federation_once(factory, now=now)
    assert stats["executions"] >= 1, stats
    async with factory() as session:
        row = await session.get(FederationExecution, execution_id, populate_existing=True)
        assert row.state == "failed" and row.error == "queue_task_failed"
        assert row.generation == 3, "the sweep advances the fence"
        assert row.lease_until is None


# ---------------------------------------------------------------------------
# Federation execution reclaim (node side)
# ---------------------------------------------------------------------------

async def test_federation_execute_takes_over_expired_lease_and_keeps_terminal_results(pg_db):
    factory = pg_db
    now = utcnow()
    actor = Actor(id="actor-alice", kind="user", organization_id=NODE_ORG,
                  role="contributor")
    admission_id, execution_id = new_id(), new_id()
    async with factory() as session:
        session.add(_admission(admission_id, executor_task_id=execution_id,
                               root_task_id="takeover-root"))
        session.add(FederationExecution(
            executor_task_id=execution_id, admission_id=admission_id,
            root_task_id="takeover-root", step_id="retrieve-1", operation="retrieve",
            state="running", generation=2,
            lease_until=now - timedelta(seconds=60),
            result_json={"spec": {"query": "takeover query", "collection_id": "",
                                  "candidate_limit": 8, "probe_refs": [],
                                  "evidence": [], "max_generation_tokens": 0},
                         "result": None},
            created_at=now, updated_at=now))
        await session.commit()

    async with factory() as session:
        execution = await federation.require_execution(session, actor, execution_id)
        status = await federation.execute(session, actor, execution, now=now,
                                          http=None, index=PgVectorIndex())
    assert status["state"] == "succeeded", f"{status['state']}: {status['error']}"
    assert status["generation"] == 3, "takeover must advance the fencing token"
    assert status["lease_until"] is None

    terminal_admission, terminal_execution = new_id(), new_id()
    async with factory() as session:
        session.add(_admission(terminal_admission, executor_task_id=terminal_execution,
                               root_task_id="terminal-root"))
        session.add(FederationExecution(
            executor_task_id=terminal_execution, admission_id=terminal_admission,
            root_task_id="terminal-root", step_id="retrieve-1", operation="retrieve",
            state="succeeded", generation=7, lease_until=None,
            result_json={"spec": {"query": "x"}, "result": {"sentinel": "keep-me"}},
            created_at=now, updated_at=now))
        await session.commit()

    async with factory() as session:
        execution = await federation.require_execution(session, actor, terminal_execution)
        status = await federation.execute(session, actor, execution, now=now,
                                          http=None, index=PgVectorIndex())
    assert status["state"] == "succeeded"
    async with factory() as session:
        row = await session.get(FederationExecution, terminal_execution,
                                populate_existing=True)
        assert row.generation == 7
        assert row.result_json["result"] == {"sentinel": "keep-me"}, \
            "a terminal execution must never be re-run or overwritten"
