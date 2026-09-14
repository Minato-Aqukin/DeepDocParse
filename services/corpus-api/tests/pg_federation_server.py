"""Shared mechanics for the opt-in real-PostgreSQL federation suites.

Three things must be *the same three things* across `test_federation_pg.py`,
`test_federation_concurrency_pg.py` and the pre-existing `test_cache_pg.py`:

1. a DSN read from an explicit environment variable, with a loud skip naming the
   variable when absent (the default suite must stay green without docker);
2. an engine/sessionmaker with a real pool — the concurrency suites need
   independent sessions/transactions, not one shared connection;
3. the alembic head **computed from the migration scripts**, never hardcoded:
   a hardcoded head turns every later migration into a false red.

The migration drill invokes alembic as a subprocess through the real CLI:
`database/corpus/alembic/env.py` calls `asyncio.run()`, which cannot run inside
the pytest event loop — and using the same CLI the deploy pipeline uses is the
whole point of a drill.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[3]

#: Primary variable, plus the alias the P5 brief allows. Both are named in the
#: skip message so the operator does not have to guess which one to export.
DSN_ENVS = ("FEDERATION_TEST_DATABASE_URL", "CORPUS_TEST_DATABASE_URL")


def federation_dsn() -> str:
    """The scratch-DB DSN or a loud opt-in skip (naming the env var)."""
    for name in DSN_ENVS:
        dsn = os.getenv(name, "")
        if dsn:
            return dsn
    pytest.skip(
        "real-PostgreSQL federation suites are opt-in: set "
        "FEDERATION_TEST_DATABASE_URL (or CORPUS_TEST_DATABASE_URL) to a "
        "migrated scratch database, e.g. via scripts/check_federation_pg.sh")


def make_engine(dsn: str, *, pool_size: int = 8, max_overflow: int = 32):
    return create_async_engine(dsn, pool_size=pool_size, max_overflow=max_overflow)


def make_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_script():
    """ScriptDirectory of the corpus migration chain (source of truth for head)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "database/corpus/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "database/corpus/alembic"))
    return ScriptDirectory.from_config(config)


def alembic_head() -> str:
    return alembic_script().get_current_head()


def _alembic_bin() -> str:
    candidate = Path(sys.executable).with_name("alembic")
    if candidate.exists():
        return str(candidate)
    return shutil.which("alembic") or "alembic"


def run_alembic(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against `dsn` (blocking; call via to_thread)."""
    env = {**os.environ, "DATABASE_URL": dsn}
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT / "services/corpus-api") + (
        os.pathsep + existing if existing else "")
    return subprocess.run(
        [_alembic_bin(), "-c", "alembic.ini", *args],
        cwd=ROOT / "database/corpus", env=env,
        capture_output=True, text=True, timeout=300, check=False)


def checked_alembic(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    """`run_alembic` + a readable failure that includes the CLI's own output."""
    result = run_alembic(*args, dsn=dsn)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result
