"""对账 —— 回调丢失时的兜底，本层可靠性的底线。

gateway 的完成回调是尽力而为的：回调失败只记日志，任务照样落终态。
backend 恰好在重启/网络抖动时错过回调，结果就会随 service 的 24h TTL 永久消失。
因此除了回调路径，必须有一条主动对账：启动时立刻跑一次（补上停机期间的遗漏），
之后按 reconcile_interval 周期跑。
"""
import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.archive import archive_task, fail_task
from app.config import settings
from app.models import Task, as_aware, utcnow
from app.service_client import ServiceClient
from app.storage import Storage

ACTIVE_STATES = ("pending", "running", "archiving")


async def reconcile_once(sessionmaker: async_sessionmaker, storage: Storage,
                         service: ServiceClient) -> dict:
    """扫一遍未落终态的任务。返回本轮统计，便于日志与测试断言。"""
    stats = {"checked": 0, "archived": 0, "failed": 0, "expired": 0}
    async with sessionmaker() as session:
        tasks = (await session.execute(
            select(Task).where(Task.status.in_(ACTIVE_STATES)).order_by(Task.created_at)
        )).scalars().all()

        for task in tasks:
            stats["checked"] += 1
            # 超过 service 的暂存窗口就再也取不回来了，落终态并提示重传
            if as_aware(task.created_at) < utcnow() - timedelta(seconds=settings.result_ttl):
                await fail_task(session, task,
                                "service result expired (>24h), please upload again")
                stats["expired"] += 1
                continue
            if not task.service_task_id:
                continue

            try:
                status = await service.get_status(task.service_task_id)
            except Exception as exc:          # service 暂时不可达：下一轮再说
                print(f"[reconcile] status check failed for {task.id}: {exc}")
                continue

            if status.get("status") == "failed":
                await fail_task(session, task, status.get("error") or "parse failed")
                stats["failed"] += 1
            elif status.get("status") == "succeeded" and task.origin == "external":
                # 外部任务（经 /v1/* 代理提交，文件在调用方那儿）：只同步状态，不归档别人的结果。
                # 页数计量在调用方取结果时做（见 proxy._proxy_parse_result）
                task.status = "succeeded"
                await session.commit()
            elif status.get("status") == "succeeded":
                try:
                    if await archive_task(session, storage, service, task.id):
                        stats["archived"] += 1
                except Exception as exc:      # 已在 archive 里退回 running，下一轮重试
                    print(f"[reconcile] archive failed for {task.id}: {exc}")
            elif task.status == "pending" and status.get("status") == "running":
                task.status = "running"
                await session.commit()
    return stats


async def reconcile_loop(sessionmaker: async_sessionmaker, storage: Storage,
                         service: ServiceClient) -> None:
    while True:
        try:
            stats = await reconcile_once(sessionmaker, storage, service)
            if stats["archived"] or stats["failed"] or stats["expired"]:
                print(f"[reconcile] {stats}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # 对账循环绝不能因单次异常退出
            print(f"[reconcile] loop error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(settings.reconcile_interval)
