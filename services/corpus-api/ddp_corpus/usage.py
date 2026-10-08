"""用量上报 —— **写的是 outbox，不是账本。**

## 为什么不直接写 usage_ledger

计量的**真相**在 control schema（Go 扣配额、出账单），而"这次解析用了几页"
只有语料侧知道。两边都写同一张表就是两个写入所有者，违反企业边界 5。

所以这里把用量作为事件发出去，由 control-api 的消费者按 `event_id` 幂等落账：

    corpus: BEGIN
              ...业务写入...
              INSERT corpus.corpus_outbox (UsageRecorded)
            COMMIT
                └─ 投递器 -> control-api /internal/events -> control.usage_ledger

**与业务写入同一个事务**是关键：分两次写的话，进程在中间崩溃会让
"解析成功了但没记账"或者反过来 —— 前者是漏收钱，后者是收了不该收的钱。
"""
import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_contracts import USAGE_KIND_VALUES
from ddp_corpus.models import CorpusOutbox, new_id


def business_event_id(business_key: str) -> str:
    """业务键 -> 确定的 32 位事件 ID（与 `new_id()` 同宽）。

    同一业务事实无论被写几次都得到同一个 ID：outbox 主键挡住同库重复，
    control 侧 `usage_ledger.event_id` 的唯一约束挡住投递后的重复。
    """
    return hashlib.sha256(business_key.encode()).hexdigest()[:32]


async def record_usage(session: AsyncSession, *, actor_id: str, organization_id: str,
                       kind: str, api_key_id: str | None = None,
                       parse_job_id: str | None = None,
                       pages: int = 0, requests: int = 1,
                       business_key: str | None = None) -> str:
    """把一笔用量写进 outbox，返回事件 ID。

    **不 commit** —— 由调用方连同业务写入一起提交。这是"同一个事务"的落点，
    也是最容易被改坏的地方：谁在这里加一句 `await session.commit()`，
    就把原子性拆掉了（有守卫钉着）。

    `business_key` 给出时事件 ID 由它确定，同一事实恰好记一次（联邦计量用：
    受理重放、补做、代次推进都会再次走到成功分支）。
    """
    if kind not in USAGE_KIND_VALUES:
        # 契约外的计量种类会让 control 侧的账目出现一个没人认识的分类，
        # 而那在对账时表现为"总数对不上"。当场拒绝，别让它进 outbox
        raise ValueError(f"未知的计量种类 {kind!r}（契约允许：{USAGE_KIND_VALUES}）")

    return await emit(session, organization_id, "UsageRecorded", {
        "actor_id": actor_id,
        "api_key_id": api_key_id,
        "parse_job_id": parse_job_id,
        "kind": kind,
        "pages": pages,
        "requests": requests,
    }, event_id=business_event_id(business_key) if business_key else None)


async def emit(session: AsyncSession, organization_id: str, event_type: str,
               payload: dict, *, event_id: str | None = None) -> str:
    """通用的 outbox 写入。

    **用 ORM 而不是裸 SQL**：裸 INSERT 绕过 SQLAlchemy 的 Python 端默认值
    （`created_at` / `next_attempt_at` 都是 `default=utcnow`），在 SQLite 上
    直接 NOT NULL 失败，在 PG 上则要另写一套 server_default —— 两个方言
    各写一份的第一步。

    确定的 `event_id` 已经在表里（尚未投递或尚未清理）时不再写第二行；已投递
    并被清掉后再写一次也无妨 —— control 按 event_id 幂等落账。

    先查后插有竞态（两个并发事务同时查到"没有"，同时插同一 ID）：插入包在
    savepoint 里，败者只回滚到这里、返回胜者的 ID —— 调用方挂起的业务写入
    不受影响，照常随外层事务提交（否则整个 session 被污染，接下来的 commit
    直接变 PendingRollbackError，业务写入跟着丢）。

    调用方挂起的待写行先在 try 外刷出去：`begin_nested` 进入时会自动 flush
    会话里**全部**待写行，那一刷若落在下面的 try 里，调用方撞上的唯一约束
    会被当成"outbox 重复"吞掉（`queue.enqueue` 同一坑，见 P5-PG-VALIDATION）。
    那里的 IntegrityError 原样回到调用方，由它自己的冲突仲裁处理。

    **不 commit** —— 由调用方连同业务写入一起提交（见 `record_usage`）。
    """
    if event_id is not None and await session.get(CorpusOutbox, event_id) is not None:
        return event_id
    # 调用方挂起的待写行先在 try 外刷出去（见上面 docstring）：savepoint 里只剩本行。
    await session.flush()
    event = CorpusOutbox(id=event_id or new_id(), organization_id=organization_id,
                         type=event_type, payload=payload)
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        if event_id is None:
            # 随机 ID 撞主键：理论上不可能，真撞上说明 ID 生成器坏了，原样上抛
            raise
        # 确定 ID 的并发重复：胜者已在表里，返回它的 ID（幂等）
        return event_id
    return event.id
