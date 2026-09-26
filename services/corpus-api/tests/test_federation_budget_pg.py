"""Opt-in root-budget transaction/locking regressions on migrated PostgreSQL."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ddp_core.application.ports import ApplicationError
from ddp_corpus import db, federation_budget
from ddp_corpus.federation_models import FederationRequest, FederationRootLedger
from ddp_corpus.models import new_id, utcnow


@pytest.fixture
async def budget_pg(monkeypatch):
    dsn = os.getenv("BUDGET_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("BUDGET_TEST_DATABASE_URL must name a migrated scratch PostgreSQL database")
    engine = create_async_engine(dsn, pool_size=8, max_overflow=24)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "get_sessionmaker", lambda: maker)
    root, org = new_id(), new_id()
    now = utcnow()
    caps = {"max_requests": 7, "max_bytes": 5 * 1024**3, "max_hops": 8,
            "max_generation_tokens": 100, "max_probe_requests": 2,
            "max_discovery_requests": 2, "max_egress_bytes": 1024**3,
            "deadline": (now + timedelta(minutes=5)).isoformat()}
    async with maker() as session:
        session.add(FederationRequest(root_task_id=root, organization_id=org,
                                      actor_id="budget-owner", task_spec_digest="sha256:" + "a" * 64))
        await federation_budget.ensure_ledger(
            session, root_task_id=root, organization_id=org,
            caller_budget=None, server_caps=caps, now=now)
        await session.commit()
    try:
        yield maker, root, org, dsn
    finally:
        async with maker() as session:
            await session.execute(delete(FederationRequest).where(FederationRequest.root_task_id == root))
            await session.execute(delete(FederationRootLedger).where(FederationRootLedger.root_task_id == root))
            await session.commit()
        await engine.dispose()


async def test_charge_commits_while_parent_locked_and_survives_rollback(budget_pg):
    maker, root, org, _ = budget_pg
    async with maker() as business:
        row = await business.get(FederationRequest, root, with_for_update=True)
        row.status = "running"
        await business.flush()
        await asyncio.wait_for(federation_budget.spend(
            root_task_id=root, organization_id=org, kind="probe"), timeout=5)
        await business.rollback()
    async with maker() as reader:
        assert (await reader.get(FederationRequest, root)).status == "queued"
        used = await federation_budget.ledger_used(reader, root_task_id=root, organization_id=org)
        assert used["requests"] == used["probes"] == 1


async def test_concurrent_charges_cannot_exceed_frozen_allowance(budget_pg):
    maker, root, org, _ = budget_pg

    async def send():
        try:
            await federation_budget.spend(root_task_id=root, organization_id=org, kind="request")
            return True
        except ApplicationError as exc:
            assert exc.code == "budget_exhausted"
            return False

    outcomes = await asyncio.gather(*(send() for _ in range(32)))
    assert sum(outcomes) == 7
    async with maker() as reader:
        row = await reader.get(FederationRootLedger, root)
        restored = federation_budget.rebuild_from_ledger(row, None, now=utcnow().timestamp())
        assert restored.used()["requests"] == 7
        with pytest.raises(ApplicationError, match="budget"):
            restored.reserve("request")


async def test_large_incoming_bytes_survive_reload_without_becoming_egress(budget_pg):
    maker, root, org, _ = budget_pg
    await federation_budget.spend(root_task_id=root, organization_id=org,
                                  kind="bytes", amount=3 * 1024**3)
    await federation_budget.spend(root_task_id=root, organization_id=org,
                                  kind="egress_bytes", amount=1024)
    async with maker() as reader:
        row = await reader.get(FederationRootLedger, root)
        restored = federation_budget.rebuild_from_ledger(row, None, now=utcnow().timestamp())
        assert restored.used()["bytes"] == 3 * 1024**3 + 1024
        restored.reserve("egress_bytes", 1024**3 - 1024)
        with pytest.raises(ApplicationError, match="budget"):
            restored.reserve("egress_bytes", 1)
        restored.reserve("bytes", 1024**3)
        with pytest.raises(ApplicationError, match="budget"):
            restored.reserve("bytes", 1)


async def test_process_death_cannot_refund_a_committed_charge(budget_pg):
    maker, root, org, dsn = budget_pg
    program = '''
import asyncio, os
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.federation_budget import spend
from ddp_corpus.federation_models import FederationRequest
async def crash():
    async with get_sessionmaker()() as business:
        row = await business.get(FederationRequest, os.environ["BUDGET_ROOT"], with_for_update=True)
        row.status = "running"
        await business.flush()
        await spend(root_task_id=row.root_task_id, organization_id=row.organization_id, kind="request")
        os._exit(0)
asyncio.run(crash())
'''
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", program,
        env={**os.environ, "DATABASE_URL": dsn, "BUDGET_ROOT": root},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=15)
    except TimeoutError:
        child.kill()
        await child.communicate()
        raise
    assert child.returncode == 0, (stdout, stderr)
    async with maker() as reader:
        assert (await reader.get(FederationRequest, root)).status == "queued"
        assert (await federation_budget.ledger_used(
            reader, root_task_id=root, organization_id=org))["requests"] == 1
