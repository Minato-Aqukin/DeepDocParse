"""语料侧 outbox 投递器。

**这一整个文件是补的。** `usage.py` 一直有测试证明"用量与业务写入在同一个
事务里"，而全仓**没有任何东西读那张表** —— 事件写进去就再没人管。
表现是用量与账单永远是空的，而每一层都不报错：写入成功、事务原子、
`/api/usage` 如实返回"没有数据"。2026-09-02 第一次真起全栈才发现。

所以这里测的不是"能不能写"，是"**写完之后有没有人把它发出去**"。
"""
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
    """409 是幂等消费端对重投的**正确回应**，当成失败会让事件永远重投。"""
    respx.post(f"{settings.control_url}/internal/usage").mock(
        return_value=httpx.Response(409, json={"error": {"code": "duplicate"}}))
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
