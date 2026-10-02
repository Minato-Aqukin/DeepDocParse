"""Root reservations survive resume but failed increments never reserve a step."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from ddp_core.application.ports import ApplicationError
from ddp_corpus import federation_budget
from ddp_corpus.federation_models import FederationRootLedger, FederationRootReservation
from ddp_corpus.models import new_id, utcnow


@pytest.fixture
async def reservation_root(session):
    root, org, stamp = new_id(), new_id(), utcnow()
    caps = {"max_requests": 10, "max_bytes": 1024, "max_hops": 2,
            "max_generation_tokens": 100, "max_probe_requests": 0,
            "max_discovery_requests": 0, "max_egress_bytes": 1024,
            "deadline": (stamp + timedelta(minutes=5)).isoformat()}
    await federation_budget.ensure_ledger(
        session, root_task_id=root, organization_id=org,
        caller_budget=None, server_caps=caps, now=stamp)
    await session.commit()
    return root, org


@pytest.mark.parametrize("kind,amount", [("hops", 2), ("generation_tokens", 100)])
async def test_reloaded_step_reservation_does_not_exhaust_or_increment_memory(
        session, reservation_root, kind, amount):
    root, org = reservation_root
    await federation_budget.spend(root_task_id=root, organization_id=org,
                                  kind=kind, amount=amount, step_id="step-1")
    row = await session.get(FederationRootLedger, root, populate_existing=True)
    restored = federation_budget.rebuild_from_ledger(row, None, now=utcnow().timestamp())
    await federation_budget.spend(root_task_id=root, organization_id=org,
                                  kind=kind, amount=amount, step_id="step-1", budget=restored)
    assert restored.used()[kind] == amount
    assert (await federation_budget.ledger_used(
        session, root_task_id=root, organization_id=org))[kind] == amount
    with pytest.raises(ApplicationError, match="budget"):
        await federation_budget.spend(root_task_id=root, organization_id=org,
                                      kind=kind, amount=amount, step_id="step-2", budget=restored)
    assert await session.scalar(select(func.count()).select_from(
        FederationRootReservation).where(FederationRootReservation.root_task_id == root)) == 1


async def test_failed_ledger_increment_rolls_back_reservation(session, reservation_root):
    root, org = reservation_root
    with pytest.raises(ApplicationError, match="budget"):
        await federation_budget.spend(root_task_id=root, organization_id=org,
                                      kind="hops", amount=3, step_id="step-1")
    assert await session.scalar(select(func.count()).select_from(
        FederationRootReservation).where(FederationRootReservation.root_task_id == root)) == 0
    await federation_budget.spend(root_task_id=root, organization_id=org,
                                  kind="hops", amount=2, step_id="step-1")
    assert (await federation_budget.ledger_used(
        session, root_task_id=root, organization_id=org))["hops"] == 2
