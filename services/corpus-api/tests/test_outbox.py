"""语料侧 outbox 投递器。

**这一整个文件是补的。** `usage.py` 一直有测试证明"用量与业务写入在同一个
事务里"，而全仓**没有任何东西读那张表** —— 事件写进去就再没人管。
表现是用量与账单永远是空的，而每一层都不报错：写入成功、事务原子、
`/api/usage` 如实返回"没有数据"。2026-09-02 第一次真起全栈才发现。

所以这里测的不是"能不能写"，是"**写完之后有没有人把它发出去**"。
"""
from datetime import timedelta

import httpx
import pytest
import respx

from ddp_corpus import outbox
from ddp_corpus.config import settings
from ddp_corpus.models import CorpusOutbox
from ddp_corpus.usage import record_usage
from sqlalchemy import select


@pytest.fixture
def sessionmaker(engine):
    from ddp_corpus.db import get_sessionmaker
    return get_sessionmaker()


async def _seed(sessionmaker, kind: str = "parse", pages: int = 3) -> str:
    async with sessionmaker() as session:
        event_id = await record_usage(session, actor_id="user-1",
                                      organization_id="org-1", kind=kind, pages=pages)
        await session.commit()
    return event_id


async def _row(sessionmaker, event_id: str) -> CorpusOutbox:
    async with sessionmaker() as session:
        return (await session.execute(
            select(CorpusOutbox).where(CorpusOutbox.id == event_id))).scalar_one()


@respx.mock
async def test_delivered_events_are_marked_and_not_resent(sessionmaker):
    route = respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 1
        # 第二轮不该再发一次 —— 重复投递就是重复计费
        assert await outbox.deliver_once(sessionmaker, http) == 0

    assert route.call_count == 1
    body = route.calls[0].request.read().decode()
    assert event_id in body, "event_id 必须发出去 —— 它是消费端唯一的幂等键"
    assert (await _row(sessionmaker, event_id)).delivered_at is not None


@respx.mock
async def test_conflict_counts_as_delivered(sessionmaker):
    """只有 409 + `duplicate_event` 包络才算成功（照着 Go 侧 classifyDelivery 写）。

    以前所有 409 都算成功：别的原因的 409 会被记成"已投递"，那笔账就永远少了，
    还没人看得见。通用 409 按暂时故障退避重投，封顶后 parked 进积压。
    """
    route = respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(409, json={"error": {"code": "duplicate"}}))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 0

    row = await _row(sessionmaker, event_id)
    assert row.delivered_at is None, "没落账的不能记成已投递"
    assert row.attempts == 1
    assert "409" in (row.last_error or ""), row.last_error
    assert row.next_attempt_at > row.created_at
    assert route.call_count == 1


@respx.mock
async def test_duplicate_event_conflict_counts_as_delivered(sessionmaker):
    """409 + `duplicate_event` 是幂等消费端对重投的**正确回应**，当成功。

    当成失败会让这条事件永远重投 —— 而 control 按 event_id 幂等落账，
    重投本来就该这么回应。
    """
    respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(409, json={"error": {"code": "duplicate_event"}}))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 1
    assert (await _row(sessionmaker, event_id)).delivered_at is not None


@respx.mock
async def test_failures_back_off_and_record_the_reason(sessionmaker):
    """失败要留下原因并退避。**不能默默丢掉** —— 丢一条就是少收一笔钱。"""
    respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(502, json={"error": {"code": "upstream"}}))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 0

    row = await _row(sessionmaker, event_id)
    assert row.delivered_at is None
    assert row.attempts == 1
    assert "502" in (row.last_error or ""), row.last_error
    # 退避把它推到将来 —— 否则失败的那条会把整批位置一直占着
    assert row.next_attempt_at > row.created_at


@respx.mock
async def test_transport_errors_do_not_kill_the_batch(sessionmaker):
    """control-api 不可达时也要如实记下来，而不是抛到循环外面。"""
    respx.post(f"{settings.control_url}/internal/usage").mock(
        side_effect=httpx.ConnectError("connection refused"))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 0

    row = await _row(sessionmaker, event_id)
    assert row.attempts == 1
    assert "不可达" in (row.last_error or ""), row.last_error


def test_backoff_is_capped():
    """无上限的指数退避会让一次长故障之后的积压几个小时都不重试，
    而那时故障早就修好了。"""
    assert outbox._backoff_seconds(1) < outbox._backoff_seconds(5)
    assert outbox._backoff_seconds(100) == outbox.MAX_BACKOFF_SECONDS


@pytest.mark.parametrize("kind", ["parse", "embed"])
@respx.mock
async def test_every_billable_kind_reaches_control(sessionmaker, kind):
    """embed 最容易在"只统计解析"里被漏掉，而漏掉的表现是
    用量报表看着正常、成本对不上。"""
    route = respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    await _seed(sessionmaker, kind=kind)

    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 1
    import json
    sent = json.loads(route.calls[0].request.read())
    assert sent["payload"]["kind"] == kind


@respx.mock
async def test_gives_up_after_max_attempts_but_keeps_the_row(sessionmaker):
    """一条 control 永远不接受的事件必须**停下来**，但**不能消失**。

    停不下来：它占着投递位无限重投（`queue.py` 早就写过同一句话）。
    消失了：丢一条就是少收一笔钱，而且没有任何人会知道。
    正确做法是留在表里、标上原因、继续算在积压里 —— 有人会看见。
    """
    respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(400, json={"error": {"code": "nope"}}))
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        for _ in range(outbox.MAX_ATTEMPTS + 2):
            # 每一轮都把退避抹掉，好在一个测试里跑完全部重试
            async with sessionmaker() as session:
                row = await session.get(CorpusOutbox, event_id)
                row.next_attempt_at = row.created_at
                await session.commit()
            await outbox.deliver_once(sessionmaker, http)

    row = await _row(sessionmaker, event_id)
    assert row.delivered_at is None, "它从来没成功过，不该被标成已投递"
    assert row.attempts >= outbox.MAX_ATTEMPTS
    assert "放弃" in (row.last_error or ""), row.last_error
    # 推到很远的将来 = 不再重投；但行还在，仍然计入积压
    assert (row.next_attempt_at - row.created_at).days > 3000


@respx.mock
async def test_a_recovering_upstream_still_gets_delivered(sessionmaker):
    """封顶不能把"上游抖了几下然后好了"也一起放弃掉。"""
    route = respx.post(f"{settings.control_url}/internal/usage")
    route.side_effect = [httpx.Response(502), httpx.Response(502),
                         httpx.Response(200, json={"ok": True})]
    event_id = await _seed(sessionmaker)

    async with httpx.AsyncClient(trust_env=False) as http:
        for _ in range(3):
            async with sessionmaker() as session:
                row = await session.get(CorpusOutbox, event_id)
                row.next_attempt_at = row.created_at
                await session.commit()
            await outbox.deliver_once(sessionmaker, http)

    assert (await _row(sessionmaker, event_id)).delivered_at is not None


@respx.mock
async def test_crashed_claim_stays_invisible_until_timeout(sessionmaker):
    """认领完崩掉（领走但没写回结果）的事件，窗口内不该被第二轮领走重投。

    认领（attempts+1 落库）与投递结果写回是两批 session：不推 next_attempt_at
    的话，崩溃重启会立刻重投一批"可能已经发出去了"的事件。
    """
    route = respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    event_id = await _seed(sessionmaker)

    # 模拟"认领完当场崩掉"：直接调 _claim 领走，但不投递、不写回结果
    claimed = await outbox._claim(sessionmaker)
    assert [c[0] for c in claimed] == [event_id]

    row = await _row(sessionmaker, event_id)
    assert row.attempts == 1, "attempts+1 必须与可见性窗口在同一个事务里落库"
    window = row.next_attempt_at - row.created_at
    assert timedelta(seconds=59) <= window <= timedelta(seconds=120), window

    # 窗口内第二轮什么都不干 —— 重投就是重复计费
    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 0
    assert route.call_count == 0

    # 窗口过去后恢复可领（抹掉窗口模拟时间过去）：之前那次 attempts 照算
    async with sessionmaker() as session:
        row = await session.get(CorpusOutbox, event_id)
        row.next_attempt_at = row.created_at
        await session.commit()
    async with httpx.AsyncClient(trust_env=False) as http:
        assert await outbox.deliver_once(sessionmaker, http) == 1
    assert (await _row(sessionmaker, event_id)).delivered_at is not None


async def test_emit_race_rolls_back_only_to_savepoint(sessionmaker):
    """先查后插的竞态：败者 flush 撞主键时只回滚到 savepoint。

    调用方挂起的业务写入不受影响，照常随外层事务提交（以前整个 session 被
    污染，接下来的 commit 直接变 PendingRollbackError，业务写入跟着丢）。
    用 AsyncMock 把检查固定成"没看到"，模拟检查与插入之间的并发提交；
    **不 commit** 的约定不变：提交还是调用方的事。
    """
    from unittest.mock import AsyncMock, patch

    from ddp_corpus.usage import emit

    duplicate_id = "d" * 32
    async with sessionmaker() as winner:
        await emit(winner, "org-1", "UsageRecorded", {"n": 1}, event_id=duplicate_id)
        await winner.commit()

    async with sessionmaker() as session:
        # 挂起的"业务写入"：必须活下来
        session.add(CorpusOutbox(id="b" * 32, organization_id="org-1",
                                 type="UsageRecorded", payload={"n": 2}))
        # 类上 patch（实例上直接 setattr 可能撞上 __slots__）
        with patch("sqlalchemy.ext.asyncio.AsyncSession.get", new=AsyncMock(return_value=None)):
            assert await emit(session, "org-1", "UsageRecorded",
                              {"n": 1}, event_id=duplicate_id) == duplicate_id
        await session.commit()

    async with sessionmaker() as session:
        ids = (await session.execute(select(CorpusOutbox.id))).scalars().all()
    assert sorted(ids) == sorted([duplicate_id, "b" * 32]), ids


async def test_outbox_backlog_reports_pending_and_abandoned(sessionmaker):
    """outbox_backlog 给 /readyz 报水位：未投递数、最老年龄、已放弃数。

    键名与 /readyz 的响应体共用一个形状（undelivered / oldest_seconds /
    abandoned），改键名必须同步改 readyz 那边，断言钉死不许悄悄漂移。
    """
    event_id = await _seed(sessionmaker)

    async with sessionmaker() as session:
        stats = await outbox.outbox_backlog(session)
    assert set(stats) == {"undelivered", "oldest_seconds", "abandoned"}
    assert stats["undelivered"] == 1
    assert stats["abandoned"] == 0
    assert stats["oldest_seconds"] >= 0.0

    # 重投封顶后 parked 的行：仍算未投递，同时算 abandoned（需要人工处理）
    async with sessionmaker() as session:
        row = await session.get(CorpusOutbox, event_id)
        row.attempts = outbox.MAX_ATTEMPTS
        await session.commit()
    async with sessionmaker() as session:
        stats = await outbox.outbox_backlog(session)
    assert stats["undelivered"] == 1
    assert stats["abandoned"] == 1
