"""语料侧 outbox 的投递器。

`usage.py` 把用量写进 `corpus_outbox`，**与业务写入同一个事务** ——
那一半一直是对的。缺的是另一半：**把它们发出去**。

2026-09-02 第一次真起全栈时才发现：`corpus_outbox` 里躺着
`UsageRecorded`，`attempts = 0`，一辈子都是 0 —— 全仓没有任何代码读这张表。
表现是**用量与账单永远是空的**，而每一层都不报错：
写入成功、事务原子、`/api/usage` 如实返回"没有数据"。

Go 侧的 `deliverOutbox` 是同一件事的另一半，这里刻意照着它写：
认领时 `FOR UPDATE SKIP LOCKED`（多副本并行投递而不重复）、
指数退避、409 + `duplicate_event` 包络当成功（消费端幂等去重的正确回应，
别的 409 一律重投 —— 见 `_is_duplicate_ack`）。

**时间在 Python 侧算，不用 `make_interval`**：那是 PG 方言，
而这一层的单测跑在 SQLite 上 —— 用方言函数等于把这段逻辑变成"只能在
真库上验"，而它恰恰是最需要单测钉住的一段（每一条丢掉的事件都是一笔钱）。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ddp_corpus.config import settings
from ddp_corpus.models import CorpusOutbox, as_aware

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

#: 认领可见性窗口（秒）。见 `_claim` —— 认领与投递结果写回之间崩了的话，
#: 那几条要过这么久才会被别的投递轮次接走，而不是立刻重投。
VISIBILITY_TIMEOUT_SECONDS = 60


def _backoff_seconds(attempts: int) -> int:
    # 指数封在 12 而不是 8：封在 8 的话 2**8=256 < 上限 300，
    # 上限那一句永远不生效 —— 一个看起来在做事、实际是死代码的封顶
    return min(2 ** min(attempts, 12), MAX_BACKOFF_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _claim(sessionmaker: async_sessionmaker) -> list[tuple[str, str, str, dict, int]]:
    """认领一批待投递事件，并把 attempts 先加上去。

    **认领要先落库。** 即使本进程认领完当场崩掉，那几条也只是等可见性窗口
    过去才被别人接走，不会被无限重投 —— 而"崩了就永远不再投"才是真正的丢事件。

    可见性窗口与 attempts+1 在**同一个事务**里落库：认领与投递结果写回是两批
    session，中间崩了的话那几条在窗口内不会被别的 deliver_once 领走 —— 否则
    崩溃重启会立刻重投一批"可能已经发出去了"的事件（至少一次变成立刻两次）。
    投递结果写回时按实际结果重算 next_attempt_at，覆盖掉这个值 —— 它只在
    "认领完崩了"时生效。
    """
    async with sessionmaker() as session:
        now = _now()
        stmt = (select(CorpusOutbox)
                .where(CorpusOutbox.delivered_at.is_(None),
                       CorpusOutbox.next_attempt_at <= now)
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
            row.next_attempt_at = now + timedelta(seconds=VISIBILITY_TIMEOUT_SECONDS)
            claimed.append((row.id, row.organization_id, row.type,
                            dict(row.payload or {}), row.attempts))
        await session.commit()
        return claimed


def _error_code(resp: httpx.Response) -> str:
    """从 control 的错误包络里读 `error.code`。

    control 的契约形状是 `{"error": {"code": ...}}`（Go 侧 classifyDelivery
    也是这么解析的）；读不出（代理页、空 body、形状不对）就返回 "" —— 调用方
    按"暂时故障"重投，**绝不**按成功处理。
    """
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def _is_duplicate_ack(resp: httpx.Response) -> bool:
    """409 里只有 `duplicate_event` 算投递成功 —— 照着 Go 侧 classifyDelivery 写。

    control 按 event_id 幂等落账，重投命中已落账的行时回这个码（"这笔已经记
    过了"）。以前**所有 409 都算成功**：别的原因的 409 会被记成"已投递"，
    那笔账就永远少了，还没人看得见。
    反过来，别的 409（组织没了、让重试的状态冲突……）与读不出码的回应一律
    按暂时故障退避重投：宁可多投一次（消费端幂等），也不误判成已送达。
    """
    return resp.status_code == 409 and _error_code(resp) == "duplicate_event"


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
            # 成功 = 2xx，或 409 + `duplicate_event` 包络。control 按 event_id
            # 幂等落账，重投命中已落账的行时回这个码；别的 409（与读不出码的
            # 回应）一律按暂时故障重投 —— 见 `_is_duplicate_ack`。
            if 200 <= resp.status_code < 300 or _is_duplicate_ack(resp):
                ok, error = True, None
            else:
                code = _error_code(resp)
                ok = False
                error = (f"control-api 返回 {resp.status_code} {code}"
                         if code else f"control-api 返回 {resp.status_code}")
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


async def outbox_backlog(session: AsyncSession) -> dict[str, int | float]:
    """outbox 水位：未投递数、最老未投递年龄（秒）、已放弃（parked）数。

    给 /readyz 用的：`abandoned > 0` 说明有事件重投封顶后还留在表里，需要
    人工处理 —— 它们留着就是为了被看见（见 MAX_ATTEMPTS），而不是悄悄消失。
    **只读**：不认领、不改行，探针里调是安全的。
    """
    now = _now()
    undelivered = await session.scalar(
        select(func.count()).select_from(CorpusOutbox)
        .where(CorpusOutbox.delivered_at.is_(None))) or 0
    oldest = await session.scalar(
        select(func.min(CorpusOutbox.created_at))
        .where(CorpusOutbox.delivered_at.is_(None)))
    abandoned = await session.scalar(
        select(func.count()).select_from(CorpusOutbox)
        .where(CorpusOutbox.delivered_at.is_(None),
               CorpusOutbox.attempts >= MAX_ATTEMPTS)) or 0
    if oldest is None:
        oldest_seconds = 0.0
    else:
        oldest_seconds = (now - as_aware(oldest)).total_seconds()
    return {"undelivered": undelivered, "oldest_seconds": oldest_seconds,
            "abandoned": abandoned}
