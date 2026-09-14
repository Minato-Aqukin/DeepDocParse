"""Opt-in real-PostgreSQL receipts for the P5 federation plane (migrated scratch DB).

SQLite gives this suite nothing on three axes the P5 brief calls out, and the
existing `test_federation_*` files cannot cover them:

1. **The migration chain itself.** SQLite tests build the schema from ORM
   metadata (`create_all`), so a migration that never creates a column and an
   ORM that quietly models it differently are both invisible. This file runs the
   real alembic CLI: head, one-revision downgrade and back, plus an
   ORM-vs-database column/nullability/type comparison for every federation,
   coverage, task and cache table.
2. **JSON round-trips through PostgreSQL.** The coverage ledger, delivery result
   and request/scope documents are serialized by the database, not by the ORM.
   The delivery digest is recomputed from the document PostgreSQL returned —
   a truncated or coerced value fails the digest.
3. **The coordinator E2E over the queue path.** `POST /tasks` must answer 202
   with a queued `federation_plan` row, the worker must advance it, local
   admissions must enqueue `federation_execute`, and the task/coverage/delivery
   read endpoints must serve what PostgreSQL actually stored.

Everything is gated on `FEDERATION_TEST_DATABASE_URL` (or the allowed alias
`CORPUS_TEST_DATABASE_URL`) and skips loudly when absent — the default suite
stays green without docker. `scripts/check_federation_pg.sh` creates and
migrates the scratch database and tears it down again.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import func, inspect, select, text

from conftest import actor_headers, drain_tasks
from ddp_corpus import db, directory, federation
from ddp_corpus.cache import FederationCacheEntry  # noqa: F401 — registers metadata
from ddp_corpus.config import settings
from ddp_corpus.federation_models import (  # noqa: F401 — registers metadata
    CoverageEntry,
    CoverageLedger,
    FederationAdmission,
    FederationDelivery,
    FederationExecution,
    FederationProbe,
    FederationRequest,
    FederationTaskEvent,
)
from ddp_corpus.main import app
from ddp_corpus.models import Base, Task, new_id
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import MemoryStorage
from ddp_core.application import plans
from ddp_core.search import PgVectorIndex
from pg_federation_server import (
    alembic_head,
    alembic_script,
    checked_alembic,
    federation_dsn,
    make_engine,
    make_factory,
)
from test_federation_probes import (
    NODE,
    configure_federation,
    indexed_source,
    publish_collection,
)
from test_federation_tasks import (
    approve_task,
    create_intent,
    exploration,
    member,
    plan_task,
    scope_manifest,
    task_spec,
)

#: Every table the P5/P6 federation plane owns, plus the task queue it rides on.
FEDERATION_TABLES = (
    "federation_probes", "federation_admissions", "federation_executions",
    "federation_requests", "federation_task_events", "coverage_ledgers",
    "coverage_entries", "federation_deliveries", "federation_cache_entries",
    "tasks",
)


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)
    monkeypatch.setattr(settings, "federation_execution_inline", False)


@pytest.fixture
async def pg_engine():
    dsn = federation_dsn()
    engine = make_engine(dsn)
    yield dsn, engine
    await engine.dispose()


@pytest.fixture
async def pg_stack(monkeypatch):
    """The real HTTP app wired to a real PostgreSQL sessionmaker.

    Same override pattern as `test_collection_catalog_pg.py`, extended with the
    app state the coordinator flow needs (HTTP client, memory storage and the
    production `PgVectorIndex`, so evidence really comes out of PostgreSQL).
    """
    dsn = federation_dsn()
    engine = make_engine(dsn)
    factory = make_factory(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_sessionmaker", factory)
    app.state.http = httpx.AsyncClient(timeout=5.0, trust_env=False)
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MemoryStorage()
    app.state.search_index = PgVectorIndex()
    app.state.redis = None
    directory.reset_cache()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://corpus", trust_env=False) as client:
        client.headers.update(actor_headers())
        yield client, factory, app.state
    await app.state.http.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 1: migration drill + ORM drift
# ---------------------------------------------------------------------------

def _type_signature(type_) -> tuple[str, object]:
    """Comparable (family, parameter) signature across ORM and reflection types.

    PostgreSQL reflection names are not ORM names (`TIMESTAMP` vs `DateTime`,
    `VARCHAR` vs `String`), so string equality is useless — compare the
    SQLAlchemy family and the attributes that carry meaning.
    """
    if isinstance(type_, sa.Text):                      # Text subclasses String
        return ("text", None)
    if isinstance(type_, sa.String):
        return ("string", type_.length)
    if isinstance(type_, sa.DateTime):
        return ("datetime", bool(type_.timezone))
    if isinstance(type_, sa.Integer):
        return ("integer", None)
    if isinstance(type_, sa.JSON):
        return ("json", None)
    if isinstance(type_, sa.Boolean):
        return ("boolean", None)
    if isinstance(type_, sa.Float):
        return ("float", None)
    return (type(type_).__name__.lower(), None)


async def _orm_drift(engine) -> list[str]:
    """Columns and nullability the migrations produce vs what the ORM believes."""
    problems: list[str] = []
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
        for table_name in FEDERATION_TABLES:
            if table_name not in names:
                problems.append(f"{table_name}: table missing from the database")
                continue
            orm_table = Base.metadata.tables[table_name]
            db_columns = {column["name"]: column for column in await conn.run_sync(
                lambda sync, table=table_name: inspect(sync).get_columns(table))}
            orm_names = {column.name for column in orm_table.columns}
            if orm_names != set(db_columns):
                problems.append(
                    f"{table_name}: column set differs "
                    f"orm-only={sorted(orm_names - set(db_columns))} "
                    f"db-only={sorted(set(db_columns) - orm_names)}")
                continue
            for column in orm_table.columns:
                db_column = db_columns[column.name]
                if bool(column.nullable) != bool(db_column["nullable"]):
                    problems.append(f"{table_name}.{column.name}: nullable "
                                    f"orm={column.nullable} db={db_column['nullable']}")
                if _type_signature(column.type) != _type_signature(db_column["type"]):
                    problems.append(f"{table_name}.{column.name}: type "
                                    f"orm={column.type} db={db_column['type']}")
    return problems


async def _constraint_snapshot(engine):
    async with engine.connect() as conn:
        def read(sync):
            inspector = inspect(sync)
            unique = {table: {constraint["name"] for constraint in
                              inspector.get_unique_constraints(table)}
                      for table in FEDERATION_TABLES}
            foreign = {table: [
                (tuple(fk["constrained_columns"]), fk["referred_table"],
                 tuple(fk["referred_columns"]),
                 (fk.get("options") or {}).get("ondelete"))
                for fk in inspector.get_foreign_keys(table)]
                for table in FEDERATION_TABLES}
            return unique, foreign
        return await conn.run_sync(read)


async def test_migration_drill_head_orm_drift_and_one_step_down(pg_engine):
    dsn, engine = pg_engine
    script = alembic_script()
    heads = script.get_heads()
    assert len(heads) == 1, f"migration chain has multiple heads: {heads}"
    head = script.get_current_head()
    down = script.get_revision(head).down_revision
    assert down, "head has no down_revision; cannot drill one revision down"

    async with engine.connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        tables = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
    assert version == head, \
        f"scratch DB is at {version!r}, migration scripts say head is {head!r}"
    missing = sorted(set(FEDERATION_TABLES) - tables)
    assert not missing, f"migrated scratch DB is missing federation tables: {missing}"

    drift = await _orm_drift(engine)
    assert drift == [], "ORM/migration drift:\n" + "\n".join(drift)

    unique, foreign = await _constraint_snapshot(engine)
    for table, name in (
            ("federation_admissions", "uq_federation_admissions_org_idempotency"),
            ("federation_requests", "uq_federation_requests_org_idempotency"),
            ("federation_requests", "uq_federation_requests_org_intent_idempotency"),
            ("federation_task_events", "uq_federation_task_events_seq"),
            ("federation_cache_entries", "uq_federation_cache_scope_key")):
        assert name in unique[table], f"{name} missing on {table}"
    assert (("admission_id",), "federation_admissions", ("admission_id",), None) \
        in foreign["federation_executions"]
    assert (("root_task_id",), "coverage_ledgers", ("root_task_id",), "CASCADE") \
        in foreign["coverage_entries"], "coverage entries must not outlive their ledger"

    try:
        await asyncio.to_thread(checked_alembic, "downgrade", down, dsn=dsn)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == down
            after = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
        if down == "0030":
            # One step back from the P6 head is exactly the cache table's migration.
            assert "federation_cache_entries" not in after, \
                "0032 downgrade must drop federation_cache_entries"
    finally:
        # Never leave the scratch DB mid-drill, even when an assertion above fired.
        await asyncio.to_thread(checked_alembic, "upgrade", "head", dsn=dsn)

    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        restored = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
    assert "federation_cache_entries" in restored
    assert await _orm_drift(engine) == [], "ORM drift after the down/up drill"


# ---------------------------------------------------------------------------
# Scenario 2: coordinator E2E over the queue path
# ---------------------------------------------------------------------------

async def test_concurrent_planning_of_one_root_probes_once_on_pg(pg_stack, monkeypatch):
    """同一 root 的两次并发规划：第二次必须等第一次提交，再返回同一份计划。

    旧行为：本地 `run_probe` 中途 `commit`，把 `create_plan` 的事务级咨询锁
    提前放掉；第二次规划在锁缝里看到 draft，把目录读、探测全做一遍并覆盖
    计划摘要，拿第一份摘要去审批的人随后 409 plan_changed。SQLite 没有
    咨询锁，这条只能在真 PostgreSQL 上验。
    """
    client, factory, _state = pg_stack
    run_key = new_id()
    async with factory() as session:
        _, version, _, _, _ = await indexed_source(session)
    collection = await publish_collection(client, version, key=f"pg-plan-race-{run_key}")
    manifest = scope_manifest([member(collection["collection_id"], node=NODE)])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        client, spec=task_spec(scope="federation_public", mode="fast"),
        consent=consent, manifest=manifest, key=f"pg-plan-race-intent-{run_key}")
    root = intent["root_task_id"]

    real_probe = federation.run_probe
    probe_calls = 0

    async def slow_probe(*args, **kwargs):
        nonlocal probe_calls
        probe_calls += 1
        result = await real_probe(*args, **kwargs)
        # 把"探测已落行、计划还没提交"的窗口撑大：锁若在这里已被放掉，
        # 第二个请求一定能挤进来。
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(federation, "run_probe", slow_probe)
    first, second = await asyncio.wait_for(asyncio.gather(
        client.post("/api/v1/task-plans", json={"root_task_id": root}),
        client.post("/api/v1/task-plans", json={"root_task_id": root})), timeout=60)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["plan_digest"] == second.json()["plan_digest"], \
        "并发重放必须返回同一份已落定的计划"
    assert probe_calls == 1, f"同一 root 只许探测一轮，实际 {probe_calls} 次"
    async with factory() as session:
        ready_events = await session.scalar(select(func.count()).select_from(
            FederationTaskEvent).where(FederationTaskEvent.root_task_id == root,
                                       FederationTaskEvent.type == "plan_ready"))
        stored = await session.get(FederationRequest, root)
    assert ready_events == 1
    assert stored.plan_digest == first.json()["plan_digest"]


async def test_coordinator_end_to_end_on_pg_queue_path(pg_stack):
    client, factory, state = pg_stack
    assert settings.federation_execution_inline is False, \
        "this test is only meaningful with the queue execution path"

    run_key = new_id()
    async with factory() as session:
        _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(client, version, key=f"pg-collection-{run_key}")
    # Pin the scope to this collection with an explicit manifest: `site_public`
    # would enumerate every published collection the scratch DB ever saw, which
    # makes the test order-dependent and not re-runnable.
    manifest = scope_manifest([member(collection["collection_id"], node=NODE)])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        client, spec=task_spec(scope="federation_public", mode="fast"),
        consent=consent, manifest=manifest, key=f"pg-intent-{run_key}")
    root = intent["root_task_id"]
    plan = await plan_task(client, root)
    await approve_task(client, root, plan)

    key = f"pg-queue-e2e-{run_key}"
    submitted = await client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": key},
        json={"root_task_id": root, "plan_digest": plan["plan_digest"]})
    assert submitted.status_code == 202, submitted.text
    accepted = submitted.json()
    assert accepted["status"] == "running" and accepted["result"] is None, \
        "new acceptance must be 202 + running with no result (execution is queued)"
    async with factory() as session:
        queued = await session.scalar(select(Task).where(
            Task.kind == "federation_plan",
            Task.dedupe_key == f"federation-request:{root}"))
        assert queued is not None and queued.status == "queued", \
            "the accepted status and its queue task must share one transaction"

    assert await drain_tasks(state) >= 1, "worker drained no federation tasks"
    status = (await client.get(f"/api/v1/tasks/{root}")).json()
    assert status["status"] == "succeeded", status
    assert status["result"]["counts"]["succeeded"] == 1
    assert status["result"]["evidence"][0]["evidence_id"] == evidence_rows[0].id
    assert status["result"]["result_manifest_digest"] == plans.digest({
        field: value for field, value in status["result"].items()
        if field != "result_manifest_digest"})

    coverage = (await client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["counts"]["total_targets"] == 1
    assert [entry["state"] for entry in coverage["entries"]] == ["succeeded"]

    async with factory() as session:
        # The stored JSON must round-trip *through PostgreSQL*, not just through
        # the API: compare the column value with the HTTP response.
        counts_type = await session.scalar(text(
            "SELECT pg_typeof(counts_json)::text FROM coverage_ledgers "
            "WHERE root_task_id = :root"), {"root": root})
        assert counts_type in ("json", "jsonb"), counts_type
        stored_counts = await session.scalar(select(CoverageLedger.counts_json).where(
            CoverageLedger.root_task_id == root))
        assert stored_counts == coverage["counts"]
        executions = list((await session.scalars(select(FederationExecution).where(
            FederationExecution.root_task_id == root))).all())
        admissions = list((await session.scalars(select(FederationAdmission).where(
            FederationAdmission.root_task_id == root))).all())
        assert len(executions) == 1 and executions[0].state == "succeeded"
        assert len(admissions) == 1, "the local target must have exactly one admission"
        # The success path clears dedupe_key, so match on the payload instead.
        execute_tasks = [task for task in (await session.scalars(select(Task).where(
            Task.kind == "federation_execute"))).all()
            if (task.payload or {}).get("executor_task_id")
            == executions[0].executor_task_id]
        assert len(execute_tasks) == 1, "local admission must enqueue federation_execute"
        assert execute_tasks[0].status == "succeeded", \
            "the queued executor task must have been drained, not left behind"
        plan_row = await session.get(Task, queued.id, populate_existing=True)
        assert plan_row.status == "succeeded"

    delivery_id = status["delivery_id"]
    assert delivery_id
    fetched = await client.get(f"/api/v1/deliveries/{delivery_id}")
    assert fetched.status_code == 200, fetched.text
    delivery = fetched.json()
    assert delivery["result"] is not None, "delivery bytes must be persisted"
    # Recompute the digest over what PostgreSQL returned: a coerced or truncated
    # document would fail here even though every status field looks right.
    assert plans.digest(delivery["result"]) == delivery["result_manifest_digest"]
    async with factory() as session:
        stored_document = await session.scalar(select(FederationDelivery.result_json).where(
            FederationDelivery.delivery_id == delivery_id))
        assert stored_document == delivery["result"]

    acked = await client.post(
        f"/api/v1/deliveries/{delivery_id}/ack",
        headers={"Idempotency-Key": key + "-ack"},
        json={"result_manifest_digest": delivery["result_manifest_digest"]})
    assert acked.status_code == 200, acked.text
    assert acked.json()["state"] == "confirmed"
    assert (await client.get(f"/api/v1/tasks/{root}")).json()["delivery_state"] \
        == "confirmed"
