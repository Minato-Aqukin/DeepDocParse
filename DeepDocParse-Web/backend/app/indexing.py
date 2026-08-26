r"""向量索引管线：归档好的 layout.json -> 分块 -> 向量化 -> 落库。

状态机（documents.index_status）：
    none -> pending -> indexing -> ready
                               \-> failed（index_error 落库，UI 可见，可手动 reindex）

两条硬规矩：
1. **claim 幂等**：多副本/重复投递靠 UPDATE ... WHERE index_status IN (...) 抢占，
   抢不到直接返回（沿用 M5 归档 claim 的套路）。
2. **失败不自动重试**：重试要重跑全量 embedding，成本高且大概率同样失败。
   落 failed + 原因，等用户/运维显式 reindex —— 静默失败是这个项目吃过大亏的地方。
"""
import json

import httpx
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.chunking import layout_to_chunks
from app.config import settings
from app.metering import record_usage
from app.models import Chunk, Document, ParseJob, utcnow
from app.storage import Storage
from app.upstream import embed_texts


async def claim_for_indexing(session: AsyncSession, document_id: str) -> bool:
    result = await session.execute(
        update(Document)
        .where(Document.id == document_id,
               Document.index_status.in_(("none", "pending", "failed")))
        .values(index_status="indexing", index_error=None, updated_at=utcnow())
    )
    await session.commit()
    return result.rowcount > 0


async def index_document(session: AsyncSession, storage: Storage, http: httpx.AsyncClient,
                         document_id: str) -> int:
    """返回写入的 chunk 数；被别人 claim 或无可索引内容时返回 0。"""
    if not await claim_for_indexing(session, document_id):
        return 0

    document = await session.get(Document, document_id)
    if document is None:
        return 0
    job = await session.get(ParseJob, document.current_job_id) if document.current_job_id else None
    if job is None or not job.result_prefix:
        await _fail(session, document, "没有可用的解析结果")
        return 0

    try:
        raw = await storage.get(f"{job.result_prefix}layout.json")
        chunks = layout_to_chunks(json.loads(raw.decode()), settings.chunk_max_chars)
    except Exception as exc:
        await _fail(session, document, f"读取版面数据失败：{type(exc).__name__}: {exc}")
        return 0

    if not chunks:
        # 纯图片/无文本层的文档：不是错误，但要让用户知道它不可问答
        await _fail(session, document, "文档没有可检索的文本块（可能是纯图片扫描件）")
        return 0

    try:
        vectors = await _embed_all(http, [c["text"] for c in chunks])
    except Exception as exc:
        await _fail(session, document, f"向量化失败：{type(exc).__name__}: {exc}")
        return 0

    # 先删后插：重解析后残留的旧块会被检索命中，且 seq 会错位
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    session.add_all([
        Chunk(document_id=document.id, parse_job_id=job.id, seq=c["seq"], page_idx=c["page_idx"],
              bbox=c["bbox"], page_size=c["page_size"], text=c["text"], char_len=c["char_len"],
              # v1.1：块类型 + 表格结构 + 分词列。三者都在分块阶段算好，
              # 不在这里现算 —— 分块是唯一知道版面上下文的地方
              block_type=c.get("block_type", "text"), table_html=c.get("table_html"),
              text_tokenized=c.get("text_tokenized", ""),
              embedding=vec)
        for c, vec in zip(chunks, vectors)
    ])
    document.index_status = "ready"
    document.index_error = None
    document.updated_at = utcnow()
    # 记在发起这次索引的人头上；老 job 没这个信息时退回上传者
    await record_usage(session, user_id=(job.initiated_by if job else None)
                       or document.uploaded_by, kind="embed",
                       requests=_batch_count(len(chunks)))
    await session.commit()
    return len(chunks)


async def _embed_all(http: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """分批向量化。批大小必须低于运行时的 max-client-batch-size，否则整批被拒。"""
    vectors: list[list[float]] = []
    size = settings.embedding_batch_size
    for start in range(0, len(texts), size):
        vectors.extend(await embed_texts(http, texts[start:start + size]))
    return vectors


def _batch_count(total: int) -> int:
    size = settings.embedding_batch_size
    return (total + size - 1) // size


async def _fail(session: AsyncSession, document: Document, reason: str) -> None:
    document.index_status = "failed"
    document.index_error = reason
    document.updated_at = utcnow()
    await session.commit()


async def mark_pending(session: AsyncSession, document: Document) -> None:
    """归档成功后由调用方置位，随后投递 index_document。"""
    document.index_status = "pending"
    document.index_error = None
    document.updated_at = utcnow()
