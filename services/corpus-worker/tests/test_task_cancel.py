"""`task_status=cancelled` 的队列语义：取消是终态，不能被复活或覆盖。

这份用例把四道闸分开钉死 —— 少任何一道，另一种"看起来像绿"的形态就会出现：

1. `queue.cancel` 幂等且只命中活跃状态（succeeded 再取消是 no-op）；
2. `claim` 永远不会领取 cancelled（否则取消只挡住了自己人，挡不住新 worker）；
3. `succeed`/`fail` 的状态守卫 —— **即使拿着取消后的新代次也写不进去**；
4. runner 遇到被取消的租约时不再写成功（旧代次的 `succeed` 抛 StaleGeneration）。

第 3 条是刻意的：取消会把 generation +1，所以迟到的成功通常先被代次围栏拦下。
只有把代次围栏拿掉之后仍然会被状态守卫拦住，那两道闸才算都真的存在。
"""
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
