"""`task_status=cancelled` 的队列语义：取消是终态，不能被复活或覆盖。

这份用例把四道闸分开钉死 —— 少任何一道，另一种"看起来像绿"的形态就会出现：

1. `queue.cancel` 幂等且只命中活跃状态（succeeded 再取消是 no-op）；
2. `claim` 永远不会领取 cancelled（否则取消只挡住了自己人，挡不住新 worker）；
3. `succeed`/`fail` 的状态守卫 —— **即使拿着取消后的新代次也写不进去**；
4. runner 遇到被取消的租约时不再写成功（旧代次的 `succeed` 抛 StaleGeneration）。

第 3 条是刻意的：取消会把 generation +1，所以迟到的成功通常先被代次围栏拦下。
只有把代次围栏拿掉之后仍然会被状态守卫拦住，那两道闸才算都真的存在。
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

import ddp_corpus.db as db
from ddp_corpus.config import settings  # noqa: F401 —— conftest 已设 service_token
from ddp_corpus.models import Task, utcnow
from ddp_corpus.queue import (
    StaleGeneration, cancel, cancel_by_dedupe, claim, enqueue, fail, heartbeat,
    is_terminal, succeed,
)
from ddp_worker.runner import Pool, WorkerState, run_one


async def _only_task_id(session) -> str:
    return (await session.execute(select(Task.id))).scalar_one()


async def test_cancel_queued_is_idempotent_and_terminal(session):
    await enqueue(session, kind="index", payload={}, dedupe_key="index:cancel")
    await session.commit()
    task_id = await _only_task_id(session)

    assert await cancel(session, task_id) is True
    row = await session.get(Task, task_id, populate_existing=True)
    assert row.status == "cancelled"
    assert row.error == "cancelled"
    assert row.finished_at is not None
    assert row.dedupe_key is None, "取消要腾出幂等键，用户重新发起时能再排一次"
    generation = row.generation

    assert await cancel(session, task_id) is False, "重复取消不得再改任何字段"
    row = await session.get(Task, task_id, populate_existing=True)
    assert (row.status, row.generation) == ("cancelled", generation)

    assert is_terminal("cancelled") is True


async def test_cancelled_task_is_never_claimed(session):
    await enqueue(session, kind="index", payload={})
    await session.commit()
    task_id = await _only_task_id(session)
    assert await cancel(session, task_id) is True

    # lease 过期也不该被领取：cancelled 不在 claim 的候选状态里。
    row = await session.get(Task, task_id)
    row.lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    assert await claim(session, ["index"]) == []


async def test_cancel_claimed_lease_fences_heartbeat_and_terminal_writes(session):
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    old_generation = task.generation

    assert await cancel(session, task.id) is True
    assert task.generation == old_generation + 1, "取消必须让代次前移（fencing token）"

    assert await heartbeat(session, task.id, old_generation) is False
    with pytest.raises(StaleGeneration):
        await succeed(session, task.id, old_generation)
    with pytest.raises(StaleGeneration):
        await fail(session, task.id, old_generation, "迟到的失败")

    row = await session.get(Task, task.id, populate_existing=True)
    assert row.status == "cancelled" and row.lease_until is None


async def test_terminal_writes_cannot_overwrite_cancelled_with_current_generation(session):
    """**状态守卫的变异确认点**：拿取消后的新代次来写也必须被拒。

    只依赖 generation 的实现在这里会红：旧代次被拒是代次围栏的功劳，
    拦不住"调用方重新读了行、拿最新代次来覆盖终态"这条路径。
    """
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    await cancel(session, task.id)
    current = (await session.get(Task, task.id, populate_existing=True)).generation

    with pytest.raises(StaleGeneration):
        await succeed(session, task.id, current)
    with pytest.raises(StaleGeneration):
        await fail(session, task.id, current, "终态覆盖")

    row = await session.get(Task, task.id, populate_existing=True)
    assert row.status == "cancelled"

async def test_fail_loses_to_cancel_between_read_and_write(session):
    """cancel 插在"fail 读行"与"fail 写失败"之间时 cancel 赢。

    旧实现先 `session.get` 读行、再在内存里改状态提交：cancel 若插在中间，
    迟到的失败会把 cancelled 复活成 queued/failed。新实现第二次 UPDATE 带
    generation + 状态守卫，cancel 赢 —— 统一 StaleGeneration，行保持 cancelled。
    """
    from ddp_corpus import queue as queue_module

    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    task_id, generation = task.id, task.generation

    real_execute = session.execute
    cancelled = False

    async def _execute_with_cancel_racing_in(stmt, *args, **kwargs):
        nonlocal cancelled
        # 在 fail 的第二次 UPDATE（写 queued/failed 的那条）**执行前**插 cancel。
        # 字面量编译后的 SQL 带目标状态值；cancel 自己的 UPDATE 也带 cancelled
        # 字面量 —— 但它发生在 cancelled 置位之后，旗标保证只拦第一次。
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        if (not cancelled and compiled.startswith("update")
                and "tasks" in compiled
                and ("queued" in compiled or "failed" in compiled)):
            async with db.get_sessionmaker()() as own:
                assert await cancel(own, task_id) is True
            cancelled = True
        return await real_execute(stmt, *args, **kwargs)

    session.execute = _execute_with_cancel_racing_in
    try:
        with pytest.raises(StaleGeneration):
            await fail(session, task_id, generation, "迟到的失败")
    finally:
        session.execute = real_execute
    assert cancelled, "cancel 没插进去 —— 这个测试什么都没测到"

    row = await session.get(Task, task_id, populate_existing=True)
    assert row.status == "cancelled", "cancel 赢：迟到的 fail 不得复活终态"
    assert row.error == "cancelled"
    assert await queue_module.claim(session, ["index"]) == []


async def test_cancel_of_succeeded_is_a_noop(session):
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    await succeed(session, task.id, task.generation)

    before = await session.get(Task, task.id, populate_existing=True)
    assert await cancel(session, task.id) is False
    after = await session.get(Task, task.id, populate_existing=True)
    assert after.status == "succeeded" and after.generation == before.generation


async def test_cancel_by_dedupe_only_touches_active_tasks(session):
    await enqueue(session, kind="federation_execute", payload={},
                  dedupe_key="federation-execution:abc")
    await session.commit()

    assert await cancel_by_dedupe(session, kind="federation_execute",
                                  dedupe_key="federation-execution:abc") is True
    assert await cancel_by_dedupe(session, kind="federation_execute",
                                  dedupe_key="federation-execution:abc") is False
    assert await cancel_by_dedupe(session, kind="federation_execute",
                                  dedupe_key="federation-execution:never") is False


async def test_runner_tolerates_a_cancelled_lease(session):
    """worker 跑着跑着任务被取消：心跳失败之外，终态写入也必须被拒绝。"""
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])

    async def handler(claimed, state):
        async with db.get_sessionmaker()() as own:
            assert await cancel(own, claimed.id) is True
        return None                         # 跑完了，但已经没资格落成功

    state = WorkerState(http=None, storage=None, search_index=None)
    await run_one(task, Pool("index", handler, 1), state)

    row = await session.get(Task, task.id, populate_existing=True)
    assert row.status == "cancelled", "runner 绝不能把被取消的任务写回 succeeded"
    assert row.error == "cancelled"


async def test_runner_stops_handler_when_heartbeat_fails(session, monkeypatch):
    from ddp_worker import runner

    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handler_task = None

    async def handler(claimed, state):
        nonlocal handler_task
        handler_task = asyncio.current_task()
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    async def broken_heartbeat(*args):
        await started.wait()
        raise RuntimeError("heartbeat database unavailable")

    monkeypatch.setattr(settings, "task_heartbeat_seconds", 0.001)
    monkeypatch.setattr(runner, "heartbeat", broken_heartbeat)
    state = WorkerState(http=None, storage=None, search_index=None)
    try:
        await asyncio.wait_for(run_one(task, Pool("index", handler, 1), state), timeout=2)
        assert stopped.is_set(), "a handler without a renewable lease must stop before requeue"
        row = await session.get(Task, task.id, populate_existing=True)
        assert row.status == "queued"
        assert "heartbeat database unavailable" in row.error
        assert row.lease_until is None
    finally:
        release.set()
        if handler_task is not None:
            await asyncio.gather(handler_task, return_exceptions=True)

async def test_runner_times_out_a_hung_handler(session, monkeypatch):
    """挂起的 handler 超过 deadline 后被取消，任务以 task_timeout 落失败/重试。

    没有 deadline 时，这个任务会永远占着租约（心跳一直续）—— 活锁。
    短 deadline 下它必须停手，且行上留的是 task_timeout 而不是"一直在处理中"。
    """
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    started, stopped = asyncio.Event(), asyncio.Event()

    async def hung(claimed, state):
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()

    monkeypatch.setattr(settings, "task_heartbeat_seconds", 0.001)
    monkeypatch.setattr(settings, "task_max_runtime_seconds", 0.02)
    monkeypatch.setattr(settings, "task_max_runtime_seconds_by_kind", {})
    state = WorkerState(http=None, storage=None, search_index=None)
    await asyncio.wait_for(run_one(task, Pool("index", hung, 1), state), timeout=5)
    assert stopped.is_set(), "超时的 handler 必须被取消，不能留孤儿协程"
    row = await session.get(Task, task.id, populate_existing=True)
    assert row.status == "queued", "还有重试机会就该排回去等退避"
    assert row.error is not None and "task_timeout" in row.error
    assert row.lease_until is None



async def test_cancelling_runner_joins_handler_before_returning(session):
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handler_task = None

    async def handler(claimed, state):
        nonlocal handler_task
        handler_task = asyncio.current_task()
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    state = WorkerState(http=None, storage=None, search_index=None)
    operation = asyncio.create_task(run_one(task, Pool("index", handler, 1), state))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert stopped.is_set(), "run_one returned while its handler could still write"
        row = await session.get(Task, task.id, populate_existing=True)
        assert row.status == "claimed", "interrupted work must remain reclaimable after expiry"
    finally:
        operation.cancel()
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        if handler_task is not None:
            await asyncio.gather(handler_task, return_exceptions=True)
