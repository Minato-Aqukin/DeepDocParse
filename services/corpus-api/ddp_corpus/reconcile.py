"""对账 —— 回调丢失时的兜底，本层可靠性的底线。

gateway 的完成回调是尽力而为的：回调失败只记日志，任务照样落终态。
backend 恰好在重启/网络抖动时错过回调，结果就会随 service 的 24h TTL 永久消失。
因此除了回调路径，必须有一条主动对账：启动时立刻跑一次（补上停机期间的遗漏），
之后按 reconcile_interval 周期跑。

顺带承担第二个职责：把归档完成但还没建索引的文档推进到 ready
（索引任务可能因为进程重启丢了投递）。

第三个职责（P5 队列切片起）：**联邦回收清扫**。队列本身能接管崩溃 worker 的
任务，但"没有被任何 worker 领取、也没有终态"的执行并不会自己消失：
- 过期租约的 `federation_executions` 落 `failed` + `error=lease_expired`
  （resume 据此重做，而不是让目标永远停在"运行中"）；
- 卡在 running 超过 deadline 的协调任务落 `failed` + `coordinator_stalled`
  （覆盖账本原样保留，分母不因清扫消失）。

清扫只改**还在活跃**的行：终态（succeeded/failed/cancelled）一律不碰 ——
用超时理由覆盖一个已经落定的结果，就是把正确结论换成错误的。
"""
import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ddp_corpus.archive import archive_job, fail_job
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationExecution, FederationRequest
from ddp_corpus.gc import collect_deleted_objects
from ddp_corpus.indexing import index_document
from ddp_corpus.models import Document, ParseJob, Task, as_aware, utcnow
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import Storage

ACTIVE_STATES = ("pending", "running", "archiving")


async def reconcile_once(sessionmaker: async_sessionmaker, storage: Storage,
                         service: ServiceClient, http: httpx.AsyncClient) -> dict:
    """扫一遍未落终态的 job 与未建索引的文档。返回统计，便于日志与测试断言。"""
    stats = {"checked": 0, "archived": 0, "failed": 0, "expired": 0, "indexed": 0, "gc": 0}
    async with sessionmaker() as session:
        jobs = (await session.execute(
            select(ParseJob).where(ParseJob.status.in_(ACTIVE_STATES)).order_by(ParseJob.created_at)
        )).scalars().all()

        for job in jobs:
            stats["checked"] += 1
            # 超过 service 的暂存窗口就再也取不回来了，落终态并提示重传
            if as_aware(job.created_at) < utcnow() - timedelta(seconds=settings.result_ttl):
                await fail_job(session, job, "service result expired (>24h), please re-parse")
                stats["expired"] += 1
                continue
            if not job.service_task_id:
                continue

            try:
                status = await service.get_status(job.service_task_id)
            except Exception as exc:          # service 暂时不可达：下一轮再说
                print(f"[reconcile] status check failed for {job.id}: {exc}")
                continue

            if status.get("status") == "failed":
                await fail_job(session, job, status.get("error") or "parse failed")
                stats["failed"] += 1
            elif status.get("status") == "succeeded":
                document = await session.get(Document, job.document_id)
                if document is not None and document.origin == "external":
                    # 外部任务（经 /v1/* 代理提交，文件在调用方那儿）：只同步状态，
                    # 不归档别人的结果。页数计量在调用方取结果时做
                    job.status = "succeeded"
                    await session.commit()
                    continue
                try:
                    if await archive_job(session, storage, service, job.id):
                        stats["archived"] += 1
                except Exception as exc:      # 已在 archive 里退回 running，下一轮重试
                    print(f"[reconcile] archive failed for {job.id}: {exc}")
            elif job.status == "pending" and status.get("status") == "running":
                job.status = "running"
                await session.commit()

        # 归档完但索引没跟上的（投递丢失/上次失败后被手动重置）
        now = utcnow()
        pending_index = (await session.execute(select(ParseJob.document_id, ParseJob.id)
            .join(Document, Document.id == ParseJob.document_id).where(
                or_(ParseJob.index_status == "pending", and_(ParseJob.index_status == "indexing",
                    or_(ParseJob.index_lease_until.is_(None), ParseJob.index_lease_until < now))),
                Document.deleted_at.is_(None)).order_by(ParseJob.updated_at).limit(20))).all()
        for document_id, job_id in pending_index:
            try:
                if await index_document(session, storage, http, document_id, job_id=job_id):
                    stats["indexed"] += 1
            except Exception as exc:
                print(f"[reconcile] index failed for job {job_id}: {exc}")

    stats["gc"] = await collect_deleted_objects(sessionmaker, storage)
    return stats


async def reconcile_loop(sessionmaker: async_sessionmaker, storage: Storage,
                         service: ServiceClient, http: httpx.AsyncClient,
                         redis=None) -> None:
    """每个副本都跑这个循环，但每轮先抢一把 Redis 锁。

    不抢锁也不会错（归档与索引都有 DB claim，重复执行是空转），但 N 个副本
    同时全表扫描纯属浪费。配了 redis_url 就选主，没配就是单实例，直接跑。
    """
    while True:
        try:
            if await _acquire_tick(redis):
                stats = await reconcile_once(sessionmaker, storage, service, http)
                if any(stats[k] for k in ("archived", "failed", "expired", "indexed", "gc")):
                    print(f"[reconcile] {stats}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # 对账循环绝不能因单次异常退出
            print(f"[reconcile] loop error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(settings.reconcile_interval)


async def _acquire_tick(redis, key: str = "ddp:reconcile:tick") -> bool:
    if redis is None:
        return True
    try:
        # 锁的存活时间略短于一个周期：持锁副本挂了，下一轮别的副本就能接手
        return bool(await redis.set(key, "1", nx=True,
                                    ex=max(5, settings.reconcile_interval - 5)))
    except Exception:
        return True                            # Redis 不可用时退回"每个副本各跑各的"


# ---------------------------------------------------------------------------
# 联邦回收清扫
# ---------------------------------------------------------------------------

async def _sweep_dead_queue_executions(session, at) -> int:
    """把"队列任务已经死掉"的 queued 执行落成显式失败，返回标记条数。

    `federation.admit` 把执行行与 `federation_execute` 任务放在同一个事务里，
    正常路径下这两者总是一起存在。但任务在 `queue.fail` 里耗尽 `max_attempts`
    后落 failed 并**清空 dedupe_key**（腾出幂等键），执行行却没人管 ——
    它没有租约（`lease_until IS NULL`），过期租约清扫扫不到它，于是永久停在
    queued，`resume` 也不会重排它（`retry_execution` 只认清扫留下的显式事实）。
    这里按任务 payload 里的 `executor_task_id` 把执行行与队列任务对上账：

    - 任务 failed / cancelled -> 执行落 `failed/queue_task_failed` /
      `queue_task_cancelled`（可见原因，resume 据此重排一次新代次）；
    - 任务整个不见了（历史行/人工删除）且过了 `TASK_LEASE_SECONDS` 宽限 ->
      执行落 `failed/queue_task_missing`；
    - 任务还在活跃（queued/claimed/running）或已 succeeded -> 一律不碰。

    写入同样是条件 UPDATE（`state='queued' AND lease_until IS NULL`）并让
    generation 前移：查与写之间被 worker 领取时命中 0 行，绝不覆盖接管者。
    """
    queued = (await session.execute(select(FederationExecution).where(
        FederationExecution.state == "queued",
        FederationExecution.lease_until.is_(None)))).scalars().all()
    if not queued:
        return 0
    executor_ids = [row.executor_task_id for row in queued]
    tasks = (await session.execute(select(Task).where(
        Task.kind == "federation_execute",
        Task.payload["executor_task_id"].as_string().in_(executor_ids)))).scalars().all()
    by_executor: dict[str, Task] = {}
    active = ("queued", "claimed", "running")
    for task in tasks:
        executor_id = (task.payload or {}).get("executor_task_id")
        if not isinstance(executor_id, str):
            continue
        # 重排后同一 executor_task_id 会有"旧终态 + 新活跃"两行。**活跃行一律
        # 优先**：否则下一轮 sweep 会把刚重试的执行再按旧 failed 任务判死
        # （复验反例：retry 与 worker 领取之间夹一次 sweep）。没有活跃行时
        # 才采用终态行；多个活跃/多个终态理论上不该出现，保留先见的一条。
        current = by_executor.get(executor_id)
        if current is None:
            by_executor[executor_id] = task
        elif task.status in active and current.status not in active:
            by_executor[executor_id] = task
    grace = at - timedelta(seconds=settings.task_lease_seconds)
    marked = 0
    for execution in queued:
        task = by_executor.get(execution.executor_task_id)
        if task is not None:
            if task.status == "failed":
                error = "queue_task_failed"
            elif task.status == "cancelled":
                error = "queue_task_cancelled"
            else:
                continue
        elif as_aware(execution.updated_at) < grace:
            error = "queue_task_missing"
        else:
            continue
        changed = await session.execute(update(FederationExecution).where(
            FederationExecution.executor_task_id == execution.executor_task_id,
            FederationExecution.state == "queued",
            FederationExecution.lease_until.is_(None)).values(
            state="failed", error=error, generation=FederationExecution.generation + 1,
            updated_at=at).execution_options(synchronize_session=False))
        marked += changed.rowcount
    return marked


async def sweep_federation_once(sessionmaker: async_sessionmaker, *,
                                now=None) -> dict:
    """扫一遍联邦活性：过期租约的执行、队列已死的执行、卡死的协调任务。

    全部条件都在 UPDATE 的 WHERE 里再写一遍（而不是"先查后写"）：查与写
    之间可能有 worker 刚好完成/接管这条行，**条件 UPDATE 让"扫描快照"与
    "实际写入"之间的竞争变成写入 0 行，而不是覆盖一个刚落定的成功**。
    """
    from ddp_corpus import federation_tasks, queue  # 延迟导入避免模块环

    at = now or utcnow()
    stats = {"executions": 0, "requests": 0, "cancelled_tasks": 0}
    async with sessionmaker() as session:
        executor_ids = (await session.execute(select(
            FederationExecution.executor_task_id).where(
            FederationExecution.state.in_(("queued", "running")),
            FederationExecution.lease_until.is_not(None),
            FederationExecution.lease_until < at))).scalars().all()
        for executor_task_id in executor_ids:
            changed = await session.execute(update(FederationExecution).where(
                FederationExecution.executor_task_id == executor_task_id,
                FederationExecution.state.in_(("queued", "running")),
                FederationExecution.lease_until.is_not(None),
                FederationExecution.lease_until < at).values(
                # generation+1：崩溃前那个 worker 醒过来时终态写入也被围栏拦住，
                # 不会把"租约过期"这个事实改回 running/succeeded。
                state="failed", error="lease_expired", lease_until=None,
                generation=FederationExecution.generation + 1, updated_at=at)
                .execution_options(synchronize_session=False))
            stats["executions"] += changed.rowcount

        stats["executions"] += await _sweep_dead_queue_executions(session, at)

        stuck_before = at - timedelta(seconds=settings.federation_request_stuck_seconds)
        root_ids = (await session.execute(select(FederationRequest.root_task_id).where(
            FederationRequest.status == "running",
            FederationRequest.updated_at < stuck_before))).scalars().all()
        stalled: list[str] = []
        for root_task_id in root_ids:
            if await federation_tasks.mark_stalled(session, root_task_id, now=at):
                stalled.append(root_task_id)
                stats["requests"] += 1
        await session.commit()
        # 队列任务在这批行落 failed **之后**取消：先取消的话，正好在跑的
        # handler 会带着"任务被取消"的印象返回，而业务行的终态还没写。
        # 这个顺序保证读到的是"已卡死 + 队列已停"的一致状态。
        for root_task_id in stalled:
            if await queue.cancel_by_dedupe(
                    session, kind="federation_plan",
                    dedupe_key=f"federation-request:{root_task_id}"):
                stats["cancelled_tasks"] += 1
    return stats


async def sweep_federation_loop(sessionmaker: async_sessionmaker,
                                redis=None) -> None:
    """按 `federation_sweep_interval` 周期跑清扫；多副本靠 Redis 锁选主。"""
    while True:
        try:
            if await _acquire_tick(redis, key="ddp:federation-sweep:tick"):
                stats = await sweep_federation_once(sessionmaker)
                if any(stats.values()):
                    print(f"[federation-sweep] {stats}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # 清扫循环绝不能因单次异常退出
            print(f"[federation-sweep] loop error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(settings.federation_sweep_interval)
