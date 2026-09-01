"""对账 —— 回调丢失时的兜底，本层可靠性的底线。

gateway 的完成回调是尽力而为的：回调失败只记日志，任务照样落终态。
backend 恰好在重启/网络抖动时错过回调，结果就会随 service 的 24h TTL 永久消失。
因此除了回调路径，必须有一条主动对账：启动时立刻跑一次（补上停机期间的遗漏），
之后按 reconcile_interval 周期跑。

顺带承担第二个职责：把归档完成但还没建索引的文档推进到 ready
（索引任务可能因为进程重启丢了投递）。
"""
import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.archive import archive_job, fail_job
from app.config import settings
from app.gc import collect_deleted_objects
from app.indexing import index_document
from app.models import Document, ParseJob, as_aware, utcnow
from app.service_client import ServiceClient
from app.storage import Storage

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
        pending_index = (await session.execute(
            select(Document.id).where(
                or_(Document.index_status == "pending",
                    and_(Document.index_status == "indexing",
                         or_(Document.index_lease_until.is_(None),
                             Document.index_lease_until < now))),
                                      Document.deleted_at.is_(None)).limit(20)
        )).scalars().all()
        for document_id in pending_index:
            try:
                if await index_document(session, storage, http, document_id):
                    stats["indexed"] += 1
            except Exception as exc:
                print(f"[reconcile] index failed for {document_id}: {exc}")

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


async def _acquire_tick(redis) -> bool:
    if redis is None:
        return True
    try:
        # 锁的存活时间略短于一个周期：持锁副本挂了，下一轮别的副本就能接手
        return bool(await redis.set("ddp:reconcile:tick", "1", nx=True,
                                    ex=max(5, settings.reconcile_interval - 5)))
    except Exception:
        return True                            # Redis 不可用时退回"每个副本各跑各的"
