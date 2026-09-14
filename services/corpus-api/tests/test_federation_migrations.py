"""N7: migration 0029's downgrade must truncate before narrowing the column.

The migration runs for real against a SQLite database here (batch mode recreates
the tables), so the lossy truncation is exercised rather than asserted from the
source. On PostgreSQL the old code would fail outright (`ALTER ... TYPE
varchar(160)` on rows longer than 160); SQLite ignores the width but would keep
the long value, which is exactly the difference the assertion catches.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration(name: str):
    path = Path(__file__).resolve().parents[3] / "database/corpus/alembic/versions" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0029_downgrade_truncates_overlong_exclusion_basis(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig29.sqlite3'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE coverage_ledgers (root_task_id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text(
            "CREATE TABLE coverage_entries ("
            "root_task_id VARCHAR(32) NOT NULL, "
            "target_digest VARCHAR(64) NOT NULL, "
            "exclusion_basis TEXT, "
            "PRIMARY KEY (root_task_id, target_digest))"))
        conn.execute(sa.text(
            "CREATE TABLE federation_requests ("
            "root_task_id VARCHAR(32) NOT NULL PRIMARY KEY, "
            "organization_id VARCHAR(32) NOT NULL, "
            "intent_idempotency_key VARCHAR(128), "
            "intent_request_digest VARCHAR(71), "
            "CONSTRAINT uq_federation_requests_org_intent_idempotency "
            "UNIQUE (organization_id, intent_idempotency_key))"))
        conn.execute(sa.text(
            "INSERT INTO coverage_entries (root_task_id, target_digest, exclusion_basis) "
            "VALUES ('t1', 'd1', :basis)"), {"basis": "x" * 4096})

        module = _migration("0029_federation_reconciliation.py")
        module.op = Operations(MigrationContext.configure(conn))
        module.downgrade()

        stored = conn.scalar(sa.text(
            "SELECT exclusion_basis FROM coverage_entries WHERE root_task_id = 't1'"))

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("coverage_entries")}
    assert stored == "x" * 160, "降级必须先把超长依据截到旧列宽（有损但明确），否则 ALTER 失败"
    assert columns["exclusion_basis"]["type"].length == 160
    request_columns = {column["name"]
                       for column in inspector.get_columns("federation_requests")}
    assert "intent_idempotency_key" not in request_columns
    assert "intent_request_digest" not in request_columns
    engine.dispose()
