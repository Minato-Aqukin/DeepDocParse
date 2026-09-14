"""任务处理器 —— **薄壳，业务实现仍在 corpus-api。**

worker 不重新实现任何东西：它只负责"从队列里拿到参数、调那份唯一的实现、
把结果落回任务行"。两边各写一份的后果这个项目已经证明过三次
（关键词路 AND/OR 语义、重建索引指错块、抽取平面从不打 vision_unavailable）。
"""
import asyncio
import logging

from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Task

from ddp_worker.runner import WorkerState

log = logging.getLogger("ddp.worker.handlers")


async def index_document(task: Task, state: WorkerState) -> str | None:
    """分块 + 向量化 + 写索引。

    `index_document` 自己还有一层解析版本级的 claim/lease/generation
    （`parse_jobs.index_generation`）—— **两层不是重复**：
    任务级的解决"哪个 worker 跑这条任务"，解析版本级的解决"哪次索引的结果能落库"。
    同一份文档可能被重建索引、重解析、复活各触发一次，它们是不同的任务，
    但只有最新那次的结果该留下。
    """
    from ddp_corpus.indexing import index_document as run

    document_id = task.payload["document_id"]
    async with get_sessionmaker()() as session:
        written = await run(session, state.storage, state.http, document_id,
                            job_id=task.payload.get("job_id"))
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


async def federation_execute(task: Task, state: WorkerState) -> str | None:
    """执行一个已受理的联邦 step（`federation-execute:<executor_task_id>`）。

    执行行与这条任务在受理时**同一个事务**落库（`federation.admit`），所以
    这里拿到的 payload 是持久事实，不是网络输入。actor 从 payload 里的
    最小绑定重建 —— 组织/身份/类型/角色/principal 五项之外一个不带；
    `federation.execute` 里的资源授权与组织过滤会拿着它再跑一遍（重新判权）。
    执行行租约的续期由 `execute(heartbeat=True)` 自己负责（长检索不被清扫误杀）。
    """
    from ddp_corpus import federation
    from ddp_corpus.models import utcnow

    payload = task.payload or {}
    executor_task_id = str(payload.get("executor_task_id") or "")
    if not executor_task_id:
        # 明确的坏 payload 比"空转成功"好：空转会让执行行永远停在 queued。
        raise ValueError("federation_execute payload is missing executor_task_id")
    actor = federation.actor_from_binding(payload.get("actor"))
    async with get_sessionmaker()() as session:
        execution = await federation.require_execution(session, actor, executor_task_id)
        await federation.execute(session, actor, execution, now=utcnow(),
                                 http=state.http, index=state.search_index,
                                 heartbeat=True)
    log.info("联邦执行完成 executor_task=%s", executor_task_id)
    return None


async def federation_plan(task: Task, state: WorkerState) -> str | None:
    """推进一个已受理的联邦协调任务（`federation-plan:<root_task_id>`）。

    与节点执行分池：协调者在本地目标上要等 `federation_execute` 落终态，
    同池会把"等子任务的父任务"和执行任务互相饿死。
    """
    from ddp_corpus import federation, federation_tasks
    from ddp_corpus.config import settings
    from ddp_corpus.models import utcnow

    payload = task.payload or {}
    root_task_id = str(payload.get("root_task_id") or "")
    if not root_task_id:
        raise ValueError("federation_plan payload is missing root_task_id")
    actor = federation.actor_from_binding(payload.get("actor"))
    sessionmaker = get_sessionmaker()
    stop = asyncio.Event()

    async def beat() -> None:
        """续协调行的活性戳：清扫按 updated_at 判卡死，不续会误杀长计划。"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), settings.task_heartbeat_seconds)
                return
            except TimeoutError:
                pass
            async with sessionmaker() as session:
                if not await federation_tasks.heartbeat_request(session, root_task_id):
                    return

    beater = asyncio.create_task(beat())
    try:
        async with sessionmaker() as session:
            await federation_tasks.run_queued(
                session, actor, root_task_id, retry_only=bool(payload.get("retry_only")),
                now=utcnow(), http=state.http, index=state.search_index)
    finally:
        stop.set()
        beater.cancel()
        try:
            await beater
        except asyncio.CancelledError:
            pass
    log.info("联邦计划执行完成 root_task=%s", root_task_id)
    return None


#: kind -> handler。加一种任务要同时改这里、契约的 `task_kind`、
#: 以及 `main.py` 里的并发配置 —— 三处齐了才算真的加上。
HANDLERS = {
    "index": index_document,
    "extract": run_extraction,
    "gc": collect_garbage,
    "federation_execute": federation_execute,
    "federation_plan": federation_plan,
}
