"""文档解析版本号与索引 fencing generation 的原子分配。"""
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ParseJob


_UNSET = object()


async def advance_index_generation(
        session: AsyncSession, document_id: str, *, values: dict[str, Any],
        expected_current_job_id: str | None | object = _UNSET,
        deleted: bool | None = None) -> int | None:
    """原子推进索引 fencing token，并可把状态变更绑在同一条 UPDATE 上。

    不能对先前加载的 ``document.index_generation`` 做 ``+= 1``：校验/VLM 等长操作
    期间另一个 worker 可能已经推进 generation，陈旧 ORM 对象会把新值写回旧值，
    让两个 worker 拿到相同 token。这里始终以数据库当前值 ``+ 1``。
    """
    conditions = [Document.id == document_id]
    if expected_current_job_id is not _UNSET:
        conditions.append(Document.current_job_id == expected_current_job_id)
    if deleted is True:
        conditions.append(Document.deleted_at.is_not(None))
    elif deleted is False:
        conditions.append(Document.deleted_at.is_(None))
    result = await session.execute(
        update(Document).where(*conditions).values(
            **values, index_generation=Document.index_generation + 1,
        ).returning(Document.index_generation)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none()


async def next_document_version(session: AsyncSession, document_id: str) -> int:
    # max+1 必须在同一份 Document 的行锁内计算。否则两个不同参数的
    # 并发重解析都可以读到同一个 max，最终产生两个看似同版本的 Evidence。
    # SQLite 单测会忽略 FOR UPDATE，生产 PostgreSQL 会按 document_id 串行化。
    await session.execute(
        select(Document.id).where(Document.id == document_id).with_for_update())
    current = await session.scalar(
        select(func.coalesce(func.max(ParseJob.document_version), 0))
        .where(ParseJob.document_id == document_id))
    return int(current or 0) + 1
