"""One immutable allowance and independently committed cost ledger per root.

The ledger deliberately has no parent FK: spending must not wait for the
coordinator's request-row lock, and a parent rollback must never refund egress.
Only creation imports legacy result counters; plans, retries and resumptions
read the same frozen limits. Business transactions never update a live ledger.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import plans, routing
from ddp_core.application.ports import ApplicationError
from ddp_corpus.errors import APIError
from ddp_corpus.models import as_aware, utcnow

BUDGET_FIELD = "_budget_used"
FAST_STOP_FIELD = "_fast_stop"
CANDIDATE_GRAPH_FIELD = "_candidate_graph"
_COUNTERS = ("requests", "bytes", "generation_tokens", "hops", "discovery", "probes", "egress_bytes")
_CALLER_FIELDS = {"max_requests", "max_bytes", "max_hops", "deadline", "max_generation_tokens"}


def load_used(result: dict | None) -> dict:
    stored = (result or {}).get(BUDGET_FIELD) or {}
    return {key: stored[key] if type(stored.get(key)) is int and stored[key] >= 0 else 0
            for key in _COUNTERS}


def validate_caller_budget(budget: dict | None) -> dict | None:
    if budget is None:
        return None
    try:
        if not isinstance(budget, dict) or set(budget) - _CALLER_FIELDS:
            raise ValueError()
        for key, minimum in (("max_requests", 1), ("max_bytes", 4096), ("max_hops", 1)):
            plans.integer(budget[key], minimum=minimum)
        plans.integer(budget.get("max_generation_tokens", 0))
        plans.instant(budget["deadline"])
    except (KeyError, ValueError, ApplicationError):
        raise APIError(400, "caller budget is invalid", "invalid_request_error", "budget_invalid") from None
    return {**budget, "max_generation_tokens": budget.get("max_generation_tokens", 0)}


def effective_caps(server: dict, caller: dict | None) -> dict:
    caps = dict(server)
    if caller is not None:
        for key in ("max_requests", "max_bytes", "max_hops", "max_generation_tokens"):
            caps[key] = min(caps.get(key, 0), caller.get(key, 0))
        if plans.instant(caller["deadline"]) < plans.instant(caps["deadline"]):
            caps["deadline"] = caller["deadline"]
    for key in ("max_probe_requests", "max_discovery_requests"):
        caps[key] = min(caps.get(key, caps["max_requests"]), caps["max_requests"])
    caps["max_egress_bytes"] = min(caps.get("max_egress_bytes", caps["max_bytes"]), caps["max_bytes"])
    return caps


async def ensure_ledger(session: AsyncSession, *, root_task_id: str,
                        organization_id: str, caller_budget: dict | None,
                        server_caps: dict, legacy_result: dict | None = None,
                        now: datetime):
    """Create alongside an intent, or in a separate transaction for an old root.

    Callers serialize the logical root. Existing rows are read-only: changing
    caps here would hold a row lock until the parent transaction completes and
    deadlock independently committed spending from that same request.
    """
    from ddp_corpus.federation_models import FederationRootLedger

    row = await session.get(FederationRootLedger, root_task_id, populate_existing=True)
    if row is not None:
        if row.organization_id != organization_id:
            raise APIError(404, "task intent not found", "invalid_request_error", "task_not_found")
        return row
    caps = effective_caps(server_caps, caller_budget)
    used = load_used(legacy_result)
    row = FederationRootLedger(
        root_task_id=root_task_id, organization_id=organization_id,
        caller_budget_json=caller_budget,
        **{key: value for key, value in caps.items() if key != "deadline"},
        deadline=datetime.fromisoformat(caps["deadline"].upper().replace("Z", "+00:00")),
        **{"used_" + key: value for key, value in used.items()},
        created_at=now, updated_at=now)
    session.add(row)
    await session.flush()
    return row


def limits(row) -> dict:
    return {**{key: int(getattr(row, key)) for key in (
        "max_requests", "max_bytes", "max_hops", "max_generation_tokens",
        "max_probe_requests", "max_egress_bytes", "max_discovery_requests")},
        "deadline": as_aware(row.deadline).isoformat().replace("+00:00", "Z")}


def _used(row) -> dict:
    return {key: int(getattr(row, "used_" + key)) for key in _COUNTERS}


async def ledger_used(session: AsyncSession, *, root_task_id: str, organization_id: str) -> dict:
    from ddp_corpus.federation_models import FederationRootLedger

    row = await session.get(FederationRootLedger, root_task_id, populate_existing=True)
    if row is None or row.organization_id != organization_id:
        return dict.fromkeys(_COUNTERS, 0)
    return _used(row)


def rebuild_from_ledger(row, plan_budget: dict | None, *, now: float) -> routing.RootBudget:
    budget = routing.RootBudget(effective_caps(limits(row), plan_budget), now=now)
    used = _used(row)
    # Probe/discovery share total requests; outgoing bytes share total bytes.
    # Replay each increment once, including bytes received in addition to egress.
    generic = used["requests"] - used["discovery"] - used["probes"]
    incoming = used["bytes"] - used["egress_bytes"]
    if generic < 0 or incoming < 0:
        raise ApplicationError("budget_exhausted", "invalid persisted budget counters")
    for kind, amount in (("discovery", used["discovery"]), ("probe", used["probes"]),
                         ("request", generic), ("egress_bytes", used["egress_bytes"]),
                         ("bytes", incoming), ("generation_tokens", used["generation_tokens"]),
                         ("hops", used["hops"])):
        if amount:
            budget.reserve(kind, amount)
    return budget


def _columns(kind):
    if kind == "probe":
        return "requests", "probes", "max_probe_requests"
    if kind == "discovery":
        return "requests", "discovery", "max_discovery_requests"
    if kind == "egress_bytes":
        return "bytes", "egress_bytes", "max_egress_bytes"
    counter = {"request": "requests", "requests": "requests", "retrieve": "requests",
               "bytes": "bytes", "hop": "hops", "hops": "hops",
               "generation": "generation_tokens", "generation_tokens": "generation_tokens",
               "tokens": "generation_tokens"}.get(kind)
    if counter is None:
        raise ApplicationError("protocol_incompatible", "unknown budget kind")
    return counter, None, None


async def spend(*, root_task_id: str, organization_id: str, kind: str,
                amount: int = 1, budget=None, now: datetime | None = None,
                step_id: str | None = None) -> None:
    """Prepay physical attempts, or reserve a plan step's allowance exactly once."""
    from ddp_corpus.db import get_sessionmaker
    from ddp_corpus.federation_models import FederationRootLedger, FederationRootReservation

    if type(amount) is not int or not 0 <= amount <= 2**63 - 1:
        raise ApplicationError("budget_exhausted", "invalid budget amount")
    counter, sub, sub_cap = _columns(kind)
    reserved = counter in {"hops", "generation_tokens"}
    if reserved and (not isinstance(step_id, str) or not step_id):
        raise ApplicationError("protocol_incompatible", "reservation requires a plan step")
    if not reserved and budget is not None:
        budget.check(kind, amount)
    if not amount:
        return
    stamp = now or utcnow()
    field = getattr(FederationRootLedger, "used_" + counter)
    cap = getattr(FederationRootLedger, "max_" + counter)
    conditions = [FederationRootLedger.root_task_id == root_task_id,
                  FederationRootLedger.organization_id == organization_id,
                  field <= cap - amount, FederationRootLedger.deadline > stamp]
    values = {"used_" + counter: field + amount, "updated_at": stamp}
    if sub is not None:
        sub_field = getattr(FederationRootLedger, "used_" + sub)
        conditions.append(sub_field <= getattr(FederationRootLedger, sub_cap) - amount)
        values["used_" + sub] = sub_field + amount
    async with get_sessionmaker()() as independent:
        if reserved:
            # No FK/parent lock: this independent transaction must not wait on the
            # business request row. The PK arbitrates concurrent coordinators.
            ledger = await independent.scalar(select(FederationRootLedger).where(
                FederationRootLedger.root_task_id == root_task_id,
                FederationRootLedger.organization_id == organization_id))
            if ledger is None:
                raise ApplicationError("budget_exhausted", "root budget not found")
            if independent.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            inserted = await independent.scalar(
                insert(FederationRootReservation).values(
                    root_task_id=root_task_id, reservation_key=f"{counter}:{step_id}",
                    kind=counter, amount=amount, created_at=stamp)
                .on_conflict_do_nothing(index_elements=["root_task_id", "reservation_key"])
                .returning(FederationRootReservation.reservation_key))
            if inserted is None:
                # rebuild_from_ledger has already replayed this persisted usage.
                await independent.commit()
                return
            if budget is not None:
                budget.check(kind, amount)
        changed = await independent.execute(
            update(FederationRootLedger).where(*conditions).values(**values)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            await independent.rollback()
            raise ApplicationError("budget_exhausted", f"{kind} budget exhausted")
        await independent.commit()
    if budget is not None:
        budget.reserve(kind, amount)
