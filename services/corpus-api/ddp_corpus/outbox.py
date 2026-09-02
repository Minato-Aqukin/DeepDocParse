"""语料侧 outbox 的投递器。

`usage.py` 把用量写进 `corpus_outbox`，**与业务写入同一个事务** ——
那一半一直是对的。缺的是另一半：**把它们发出去**。

2026-09-02 第一次真起全栈时才发现：`corpus_outbox` 里躺着
`UsageRecorded`，`attempts = 0`，一辈子都是 0 —— 全仓没有任何代码读这张表。
表现是**用量与账单永远是空的**，而每一层都不报错：
写入成功、事务原子、`/api/usage` 如实返回"没有数据"。

Go 侧的 `deliverOutbox` 是同一件事的另一半，这里刻意照着它写：
认领时 `FOR UPDATE SKIP LOCKED`（多副本并行投递而不重复）、
指数退避、409 当成功（消费端幂等去重的正确回应）。

**时间在 Python 侧算，不用 `make_interval`**：那是 PG 方言，
而这一层的单测跑在 SQLite 上 —— 用方言函数等于把这段逻辑变成"只能在
真库上验"，而它恰恰是最需要单测钉住的一段（每一条丢掉的事件都是一笔钱）。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ddp_corpus.config import settings
from ddp_corpus.models import CorpusOutbox

log = logging.getLogger("ddp.outbox")

#: 一批认领多少条。小一点让失败的影响面小，也让多副本分得开
BATCH = 20

#: 退避上限。**必须有上限** —— 无上限的指数退避会让一次长故障之后的
#: 积压事件几个小时都不重试，而那时故障早就修好了
MAX_BACKOFF_SECONDS = 300

#: 重投多少次之后放弃。**必须有封顶**：一条 control 永远不接受的事件
#: （比如它还没升级到认识这个计量种类）会占着投递位无限重投，
#: 而 `queue.py` 早就写过同一句话——"无限重试会让一个必然失败的任务
#: 永远占着 worker"。放弃时**不是删掉**：留在表里、标上原因，
#: 让它进 /readyz 的积压计数，而不是悄悄消失（丢一条就是少收一笔钱）。
MAX_ATTEMPTS = 12


def _backoff_seconds(attempts: int) -> int:
    # 指数封在 12 而不是 8：封在 8 的话 2**8=256 < 上限 300，
    # 上限那一句永远不生效 —— 一个看起来在做事、实际是死代码的封顶
    return min(2 ** min(attempts, 12), MAX_BACKOFF_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _claim(sessionmaker: async_sessionmaker) -> list[tuple[str, str, str, dict, int]]:
    """认领一批待投递事件，并把 attempts 先加上去。

    **认领要先落库。** 即使本进程认领完当场崩掉，那几条也只是等下一次
    退避窗口，不会被无限重投 —— 而"崩了就永远不再投"才是真正的丢事件。
    """
    async with sessionmaker() as session:
        stmt = (select(CorpusOutbox)
                .where(CorpusOutbox.delivered_at.is_(None),
                       CorpusOutbox.next_attempt_at <= _now())
                .order_by(CorpusOutbox.created_at)
                .limit(BATCH))
        if session.bind.dialect.name == "postgresql":
            # 多副本并行投递而不互相阻塞、也不会把同一条投两次。
            # SQLite 没有行锁（单测就跑在它上面），加了会直接报错
            stmt = stmt.with_for_update(skip_locked=True)
        rows = (await session.execute(stmt)).scalars().all()
        claimed = []
        for row in rows:
            row.attempts += 1
            claimed.append((row.id, row.organization_id, row.type,
                            dict(row.payload or {}), row.attempts))
        await session.commit()
        return claimed


async def deliver_once(sessionmaker: async_sessionmaker, http: httpx.AsyncClient) -> int:
    """认领并投递一批，返回成功投出去的条数。"""
    delivered = 0
    for event_id, organization_id, event_type, payload, attempts in await _claim(sessionmaker):
        try:
            resp = await http.post(
                f"{settings.control_url}/internal/usage",
                json={"event_id": event_id, "type": event_type,
                      "organization_id": organization_id, "payload": payload},
                headers={"Authorization": f"Bearer {settings.service_token}",
                         "X-DDP-Service": "corpus-api"},
                timeout=10.0,
            )
            # 409 = 消费端已经处理过。**当成功** —— 把它当失败会让这条
            # 事件永远重投，而幂等消费端本来就该这么回应重投
            ok = resp.status_code < 300 or resp.status_code == 409
            error = None if ok else f"control-api 返回 {resp.status_code}"
        except httpx.HTTPError as exc:
            ok, error = False, f"control-api 不可达：{exc}"

        async with sessionmaker() as session:
            row = await session.get(CorpusOutbox, event_id)
            if row is None:                       # 被 GC 清掉了，不是错误
                continue
            if ok:
                row.delivered_at = _now()
                delivered += 1
            elif attempts >= MAX_ATTEMPTS:
                # 放弃重投，但**留着**。next_attempt_at 推到很远的将来，
                # delivered_at 仍是空 —— 于是它继续算在积压里，有人会看见
                row.last_error = f"重投 {attempts} 次后放弃：{error}"
                row.next_attempt_at = _now() + timedelta(days=3650)
                log.error("outbox 事件放弃投递 id=%s type=%s attempts=%s：%s"
                          "（它仍留在表里并计入积压，需要人工处理）",
                          event_id, event_type, attempts, error)
            else:
                row.last_error = error
                row.next_attempt_at = _now() + timedelta(seconds=_backoff_seconds(attempts))
                # 不要把 payload 打进日志：里面有 actor_id 与用量明细
                log.warning("outbox 投递失败 id=%s type=%s attempts=%s：%s",
                            event_id, event_type, attempts, error)
            await session.commit()
    return delivered


async def deliver_loop(sessionmaker: async_sessionmaker, http: httpx.AsyncClient,
                       interval: float = 5.0) -> None:
    """长跑循环。**任何异常都不许让它退出** —— 循环一停，用量就再也不上报了，
    而那件事没有任何外部症状（它不影响任何用户可见的功能）。"""
    while True:
        try:
            await deliver_once(sessionmaker, http)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 见上面那句
            log.exception("outbox 投递循环出错，继续：%s", exc)
        await asyncio.sleep(interval)
