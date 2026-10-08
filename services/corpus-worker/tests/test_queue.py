"""持久任务队列的三条保证：claim / lease / generation fencing。

**第三条最容易漏，也最难查。** 只有 lease 没有 fencing 的队列是不安全的：
被判死的旧 worker 醒过来之后照样会写结果，把新 worker 算出来的东西覆盖掉。
表现是"偶尔出现一份旧结果"，日志上什么都看不出来。
"""
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from ddp_corpus.models import Task, as_aware, new_id, utcnow
from ddp_corpus.queue import (
    StaleGeneration, backlog, claim, enqueue, fail, heartbeat, succeed,
    sweep_exhausted,
)


async def test_enqueue_is_idempotent_by_dedupe_key(session):
    """用户连点三次"重建索引"不该跑三遍。"""
    first = await enqueue(session, kind="index", payload={"document_id": "d1"},
                          dedupe_key="index:d1")
    await session.commit()
    second = await enqueue(session, kind="index", payload={"document_id": "d1"},
                           dedupe_key="index:d1")
    await session.commit()

    assert first is not None
    assert second is None, "同 dedupe_key 的任务不该排第二次"


async def test_enqueue_never_mistakes_a_callers_conflict_for_its_own_dedupe(session):
    """调用方自己待写的行撞了约束，必须以 IntegrityError 回到调用方。

    enqueue 若把它当成"同 dedupe_key 已在队列里"返回 None，调用方会以为
    任务排上了；会话其实已被毒化，commit 时才炸成 PendingRollbackError
    （HTTP 500），调用方自己的冲突仲裁分支永远到不了。
    """
    await enqueue(session, kind="index", payload={"document_id": "d1"}, dedupe_key="index:d1")
    await session.commit()
    # 调用方自己的待写行：与已排队任务同 dedupe_key，flush 时撞唯一约束。
    session.add(Task(id=new_id(), kind="index", payload={"document_id": "d1"},
                     organization_id="", dedupe_key="index:d1", max_attempts=3,
                     run_after=utcnow()))
    try:
        with pytest.raises(IntegrityError):
            await enqueue(session, kind="index", payload={"document_id": "d2"},
                          dedupe_key="index:d2")
    finally:
        await session.rollback()
    # 真正的同键去重照旧返回 None。
    assert await enqueue(session, kind="index", payload={"document_id": "d1"},
                         dedupe_key="index:d1") is None


async def test_unknown_kind_is_rejected_at_enqueue(session):
    """契约外的任务种类当场拒绝 —— 排进去的话没有 handler 认得它，
    它会永远躺在队列里，而队列水位要过一阵子才有人看。"""
    with pytest.raises(ValueError, match="未知任务种类"):
        await enqueue(session, kind="not-a-real-kind", payload={})


async def test_claim_bumps_generation_and_sets_lease(session):
    await enqueue(session, kind="index", payload={"document_id": "d1"})
    await session.commit()

    [task] = await claim(session, ["index"])
    assert task.status == "claimed"
    assert task.generation == 1, "每次领取都要 +1 —— 那是 fencing token"
    assert task.lease_until is not None and task.claimed_by
    assert task.attempts == 1


async def test_claim_skips_tasks_of_other_kinds(session):
    await enqueue(session, kind="index", payload={})
    await enqueue(session, kind="extract", payload={})
    await session.commit()

    claimed = await claim(session, ["extract"], limit=10)
    assert [t.kind for t in claimed] == ["extract"], \
        "按种类分池的前提就是领取要能按种类过滤"


async def test_expired_lease_can_be_taken_over(session):
    """worker 崩了之后任务必须能被接管（企业边界 7）。"""
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [first] = await claim(session, ["index"])

    # 还没到期时不该被别人抢走
    assert await claim(session, ["index"]) == []

    # 模拟 worker 崩溃：租约过期
    first.lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()

    [second] = await claim(session, ["index"])
    assert second.id == first.id
    assert second.generation == 2, "接管必须让 generation 前进"


async def test_stale_worker_cannot_overwrite_a_newer_result(session):
    """**这条是 fencing 的全部意义。**

    旧 worker 被判死之后醒过来，拿着自己那一代的 generation 去写结果 ——
    必须被拒。只有 lease 没有 fencing 的话，它会把新 worker 的结果覆盖掉，
    而那不会报任何错。
    """
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [old] = await claim(session, ["index"])
    old_generation = old.generation

    old.lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    [new] = await claim(session, ["index"])
    assert new.generation > old_generation

    # 旧 worker 醒了
    with pytest.raises(StaleGeneration):
        await succeed(session, new.id, old_generation)
    with pytest.raises(StaleGeneration):
        await fail(session, new.id, old_generation, "旧 worker 的错误")

    # 新 worker 照常写得进去
    await succeed(session, new.id, new.generation)
    row = await session.get(Task, new.id)
    await session.refresh(row)
    assert row.status == "succeeded"


async def test_heartbeat_reports_takeover(session):
    """心跳返回 False = 已被接管，worker 应当立刻停手。

    继续跑不会出错，但算出来的结果写不进去（generation 对不上），
    纯粹白烧 GPU。
    """
    await enqueue(session, kind="index", payload={})
    await session.commit()
    [task] = await claim(session, ["index"])
    generation = task.generation

    assert await heartbeat(session, task.id, generation) is True

    task.lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    await claim(session, ["index"])          # 被别人接管

    assert await heartbeat(session, task.id, generation) is False


async def test_failure_retries_with_backoff_then_gives_up(session):
    """失败原因必须持久化，且不能无限重试。

    无限重试会让一个必然失败的任务永远占着 worker；
    只写日志则会让用户看到"一直在处理中"。
    """
    await enqueue(session, kind="index", payload={}, max_attempts=2)
    await session.commit()

    [task] = await claim(session, ["index"])
    await fail(session, task.id, task.generation, "第一次炸了")
    row = await session.get(Task, task.id)
    await session.refresh(row)
    assert row.status == "queued", "还有重试机会就该排回去"
    assert row.error == "第一次炸了", "失败原因要留着 —— 排查时要看得到每次为什么失败"
    # SQLite 存 naive、PG 存 aware —— 库里读出来的时间一律过 as_aware 再比
    # （全项目统一口径，见 ddp_core.models.as_aware）
    assert as_aware(row.run_after) > utcnow(), "重试要退避，不能立刻再来一次"

    row.run_after = utcnow()
    await session.commit()
    [again] = await claim(session, ["index"])
    await fail(session, again.id, again.generation, "第二次也炸了")
    row = await session.get(Task, again.id)
    await session.refresh(row)
    assert row.status == "failed", "超过 max_attempts 必须落终态"
    assert row.error == "第二次也炸了"
    assert row.finished_at is not None

async def test_exhausted_task_is_never_claimed_and_swept_to_failed(session):
    """毒丸不占 worker：超限行 claim 领不到，死信清扫把它标成可见 failed。

    只剩 claim 谓词时，超限行会永远停在 queued —— 对用户是"一直在处理中"；
    只剩清扫时，超限行仍会被领走反复跑。两道必须同时存在。
    """
    await enqueue(session, kind="index", payload={}, dedupe_key="index:poison",
                  max_attempts=1)
    await session.commit()

    [task] = await claim(session, ["index"])
    task_id = task.id
    await fail(session, task_id, task.generation, "第一次就炸了", retry=True)
    row = await session.get(Task, task_id, populate_existing=True)
    # max_attempts=1 且 retry=True：第一次失败即落终态（fail 的超限分支）。
    assert row.status == "failed"

    # 残留形态：耗尽的行卡在 queued（例如旧版本 fail 没落终态、或外部写入），
    # 租约已过期 —— claim 不得再领，清扫必须把它标成可见 failed。
    row.status = "queued"
    row.attempts = row.max_attempts
    row.lease_until = utcnow() - timedelta(seconds=1)
    row.dedupe_key = "index:poison"
    row.finished_at = None
    row.error = "第一次就炸了"
    await session.commit()

    assert await claim(session, ["index"]) == [], "耗尽的行不得再被领取"

    swept = await sweep_exhausted(session)
    assert swept == 1
    row = await session.get(Task, task_id, populate_existing=True)
    assert row.status == "failed", "死信必须可见，不能永远处理中"
    assert row.error is not None and "poison" in row.error
    assert row.dedupe_key is None, "死信要腾出幂等键，允许人工重排"
    assert row.finished_at is not None
    assert await claim(session, ["index"]) == []

    # 租约内的 claimed 行正在被 worker 处理，清扫不得抢。
    await enqueue(session, kind="index", payload={}, max_attempts=1)
    await session.commit()
    [live] = await claim(session, ["index"])
    live.attempts = live.max_attempts
    await session.commit()
    assert await sweep_exhausted(session) == 0
    row = await session.get(Task, live.id, populate_existing=True)
    assert row.status == "claimed"



async def test_succeed_frees_the_dedupe_key(session):
    """完成之后同样的任务要能再排 —— 否则"重建索引"一辈子只能点一次。"""
    await enqueue(session, kind="index", payload={}, dedupe_key="index:d1")
    await session.commit()
    [task] = await claim(session, ["index"])
    await succeed(session, task.id, task.generation)

    again = await enqueue(session, kind="index", payload={}, dedupe_key="index:d1")
    await session.commit()
    assert again is not None


async def test_backlog_reports_age_not_just_count(session):
    """**最老任务年龄比积压数更能说明问题**：积压 100 可能只是刚来一批，
    最老一条 20 分钟没被领走才是故障。"""
    await enqueue(session, kind="index", payload={})
    await session.commit()
    old = (await session.execute(
        __import__("sqlalchemy").select(Task))).scalars().one()
    old.created_at = utcnow() - timedelta(minutes=20)
    await session.commit()

    stats = await backlog(session)
    assert stats["index"]["pending"] == 1
    assert stats["index"]["oldest_seconds"] > 600
    assert stats["extract"]["pending"] == 0
