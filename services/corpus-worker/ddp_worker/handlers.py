"""任务处理器 —— **薄壳，业务实现仍在 corpus-api。**

worker 不重新实现任何东西：它只负责"从队列里拿到参数、调那份唯一的实现、
把结果落回任务行"。两边各写一份的后果这个项目已经证明过三次
（关键词路 AND/OR 语义、重建索引指错块、抽取平面从不打 vision_unavailable）。
"""
import logging

from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Task

from ddp_worker.runner import WorkerState

log = logging.getLogger("ddp.worker.handlers")


async def index_document(task: Task, state: WorkerState) -> str | None:
    """分块 + 向量化 + 写索引。

    `index_document` 自己还有一层文档级的 claim/lease/generation
    （`documents.index_generation`）—— **两层不是重复**：
    任务级的解决"哪个 worker 跑这条任务"，文档级的解决"哪次索引的结果能落库"。
    同一份文档可能被重建索引、重解析、复活各触发一次，它们是不同的任务，
    但只有最新那次的结果该留下。
    """
    from ddp_corpus.indexing import index_document as run

    document_id = task.payload["document_id"]
    async with get_sessionmaker()() as session:
        written = await run(session, state.storage, state.http, document_id)
    log.info("索引完成 document=%s chunks=%d", document_id, written)
    return None


async def run_extraction(task: Task, state: WorkerState) -> str | None:
    """跑一个抽取批次。"""
    from ddp_corpus.routers.extractions import execute_run

    await execute_run(
        task.payload["run_id"], task.payload["document_ids"], task.payload["schema"],
        storage=state.storage, http=state.http, index=state.search_index,
        verify=task.payload.get("verify"),
    )
    return None


async def collect_garbage(task: Task, state: WorkerState) -> str | None:
    """回收软删除文档的对象。

    **全项目唯一会不可逆毁数据的地方**，两道防护（宽限期 + claim）在
    `ddp_corpus/gc.py` 里，这里只负责按时把它叫起来。
    """
    from ddp_corpus.gc import collect_deleted_objects

    cleaned = await collect_deleted_objects(get_sessionmaker(), state.storage)
    log.info("对象回收完成 documents=%d", cleaned)
    return None


#: kind -> handler。加一种任务要同时改这里、契约的 `task_kind`、
#: 以及 `main.py` 里的并发配置 —— 三处齐了才算真的加上。
HANDLERS = {
    "index": index_document,
    "extract": run_extraction,
    "gc": collect_garbage,
}
