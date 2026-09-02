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
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_contracts import USAGE_KIND_VALUES
from ddp_corpus.models import CorpusOutbox, new_id


async def record_usage(session: AsyncSession, *, actor_id: str, organization_id: str,
                       kind: str, api_key_id: str | None = None,
                       parse_job_id: str | None = None,
                       pages: int = 0, requests: int = 1) -> str:
    """把一笔用量写进 outbox，返回事件 ID。

    **不 commit** —— 由调用方连同业务写入一起提交。这是"同一个事务"的落点，
    也是最容易被改坏的地方：谁在这里加一句 `await session.commit()`，
    就把原子性拆掉了（有守卫钉着）。
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
    })


async def emit(session: AsyncSession, organization_id: str, event_type: str,
               payload: dict) -> str:
    """通用的 outbox 写入。

    **用 ORM 而不是裸 SQL**：裸 INSERT 绕过 SQLAlchemy 的 Python 端默认值
    （`created_at` / `next_attempt_at` 都是 `default=utcnow`），在 SQLite 上
    直接 NOT NULL 失败，在 PG 上则要另写一套 server_default —— 两个方言
    各写一份的第一步。
    """
    event = CorpusOutbox(id=new_id(), organization_id=organization_id,
                         type=event_type, payload=payload)
    session.add(event)
    await session.flush()
    return event.id
