"""Remote execution waiting preserves terminal outcomes and bounded root spending."""
from datetime import datetime, timezone

import pytest

from ddp_core.application import routing
from ddp_corpus import federation_tasks


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.sleeps = []

    def now(self):
        return datetime.fromtimestamp(1_800_000_000 + self.seconds, timezone.utc)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds


def clock_for(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(federation_tasks, "utcnow", clock.now)
    monkeypatch.setattr(federation_tasks.time, "monotonic", lambda: clock.seconds)
    monkeypatch.setattr(federation_tasks.asyncio, "sleep", clock.sleep)
    return clock


def budget_for(clock, requests):
    return routing.RootBudget(
        {"max_requests": requests, "max_bytes": 1_000_000, "max_hops": 4,
         "deadline": datetime.fromtimestamp(clock.now().timestamp() + 900, timezone.utc).isoformat(),
         "max_generation_tokens": 1024}, now=clock.now().timestamp())


async def test_cpu_executor_finishes_after_ten_minutes_within_root_request_budget(monkeypatch):
    clock = clock_for(monkeypatch)
    budget = budget_for(clock, 144)
    # Retrieval/admission/probe requests remain charged before generation waiting starts.
    budget.reserve("request", 12)
    calls = []

    class CPUExecutor:
        async def execution(self, executor_task_id):
            calls.append(clock.seconds)
            return {"state": "succeeded" if clock.seconds >= 600 else "running"}

    status = await federation_tasks._poll_execution(
        CPUExecutor(), "cpu-exec", deadline_ts=clock.now().timestamp() + 900, budget=budget)
    assert status["state"] == "succeeded", status
    assert 600 <= clock.seconds <= 615
    assert len(calls) <= 45
    assert budget.used()["requests"] == 12 + len(calls)


async def test_poll_budget_exhaustion_reports_actual_paid_polls(monkeypatch):
    clock = clock_for(monkeypatch)
    budget = budget_for(clock, 3)
    calls = []

    class Running:
        async def execution(self, executor_task_id):
            calls.append(executor_task_id)
            return {"state": "running"}

    status = await federation_tasks._poll_execution(
        Running(), "slow-exec", deadline_ts=clock.now().timestamp() + 900, budget=budget)
    assert status["state"] == "unreachable"
    assert status["error"] == "budget_exhausted:polls=3"
    assert calls == ["slow-exec"] * 3
    assert budget.used()["requests"] == 3


async def test_poll_deadline_does_not_send_a_late_request(monkeypatch):
    clock = clock_for(monkeypatch)
    calls = []

    class Running:
        async def execution(self, executor_task_id):
            calls.append(clock.seconds)
            return {"state": "running"}

    status = await federation_tasks._poll_execution(
        Running(), "slow-exec", deadline_ts=clock.now().timestamp() + 2.5)
    assert status == {"state": "unreachable", "error": "peer_execution_timeout"}
    assert calls == [0, 1]
    assert clock.seconds == 2.5


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
async def test_every_executor_terminal_state_returns_without_another_poll(monkeypatch, state):
    clock = clock_for(monkeypatch)
    calls = []
    terminal = {"state": state, "generation": 7, "result_ref": None,
                "wiki_draft": {"validation_state": "failed", "error": "wiki_budget_exceeded"}}

    class Terminal:
        async def execution(self, executor_task_id):
            calls.append(executor_task_id)
            return terminal

    status = await federation_tasks._poll_execution(
        Terminal(), "done-exec", deadline_ts=clock.now().timestamp() + 900)
    assert status == terminal
    assert calls == ["done-exec"]
    assert clock.sleeps == []


async def test_approved_deadline_also_bounds_a_stalled_status_read():
    import asyncio

    calls = []

    class Stalled:
        async def execution(self, executor_task_id):
            calls.append(executor_task_id)
            await asyncio.Event().wait()

    status = await asyncio.wait_for(federation_tasks._poll_execution(
        Stalled(), "stalled-exec",
        deadline_ts=federation_tasks.utcnow().timestamp() + 0.02), timeout=0.2)
    assert status == {"state": "unreachable", "error": "peer_execution_timeout"}
    assert calls == ["stalled-exec"]
