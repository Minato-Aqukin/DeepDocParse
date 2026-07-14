"""ARQ 后处理链 —— gateway 的编排主战场。

原则：不双重排队。推理级排队/多卡调度归 mineru 自己；
ARQ 只负责推理完成后的编排：

  poll_and_archive(task_id):
    1. 轮询 mineru status 直到 succeeded/failed（指数退避，上限 POLL_TIMEOUT）
    2. fetch_result -> task_store.save_result（暂存 24h）
    3. callback_url 存在则 POST 通知 backend 取件（带 service token）
    4. queue_depth -1
    v2(M4) 追加: 5. chunk_and_index(task_id)  # 结构感知分块 -> bge-m3 向量化 -> Redis 索引

启动：arq app.worker.tasks.WorkerSettings
"""
import asyncio
import time

import httpx
import redis.asyncio as redis
from arq.connections import RedisSettings

from app.config import load_registry, settings
from app.services.mineru_client import MineruClient, MineruTaskNotFound
from app.services.task_store import TaskStore


async def _notify_callback(http: httpx.AsyncClient, task: dict, task_id: str, status: str) -> None:
    """回调 backend（尽力而为：失败只记日志，不影响任务终态）。"""
    callback_url = task.get("callback_url")
    if not callback_url:
        return
    try:
        await http.post(
            callback_url,
            json={"task_id": task_id, "status": status},
            headers={"Authorization": f"Bearer {settings.service_token}"},
        )
    except httpx.HTTPError as exc:  # pragma: no cover - 网络毛刺不应打断归档
        print(f"[worker] callback failed for {task_id}: {exc}")


async def _finish(ctx: dict, task_id: str, task: dict, status: str, error: str | None = None) -> None:
    store: TaskStore = ctx["task_store"]
    await store.set_status(task_id, status, error)
    await store.dec_queue_depth()
    await _notify_callback(ctx["http"], task, task_id, status)


async def poll_and_archive(ctx: dict, task_id: str) -> None:
    store: TaskStore = ctx["task_store"]
    client: MineruClient = ctx["mineru_client"]

    task = await store.get(task_id)
    if task is None:  # TTL 过期或被清理
        return
    endpoint = ctx["registry"].parse_engines[task["engine"]].endpoint

    delay = settings.poll_initial_delay
    deadline = time.monotonic() + settings.poll_timeout
    while True:
        try:
            live = await client.status(endpoint, task["mineru_task_id"])
        except MineruTaskNotFound:
            await _finish(ctx, task_id, task, "failed", "mineru task disappeared")
            return
        except httpx.HTTPError:
            live = {"status": "running", "error": None}  # 引擎暂不可达：继续退避重试

        if live["status"] == "failed":
            await _finish(ctx, task_id, task, "failed", live.get("error") or "parse failed")
            return
        if live["status"] == "succeeded":
            break
        if time.monotonic() > deadline:
            await _finish(ctx, task_id, task, "failed",
                          f"parse timed out after {settings.poll_timeout:.0f}s")
            return
        if live["status"] == "running" and task.get("status") == "pending":
            await store.set_status(task_id, "running")
            task["status"] = "running"
        await asyncio.sleep(delay)
        delay = min(delay * 2, settings.poll_max_delay)

    try:
        result = await client.fetch_result(endpoint, task["mineru_task_id"])
    except (RuntimeError, MineruTaskNotFound, httpx.HTTPError) as exc:
        await _finish(ctx, task_id, task, "failed", str(exc))
        return
    if result is None:  # completed 但结果尚未落地：给 mineru 一次喘息后重取
        await asyncio.sleep(settings.poll_initial_delay)
        result = await client.fetch_result(endpoint, task["mineru_task_id"])
        if result is None:
            await _finish(ctx, task_id, task, "failed", "mineru reported success but no result")
            return

    await store.save_result(task_id, result)
    await _finish(ctx, task_id, task, "succeeded")


async def chunk_and_index(ctx: dict, task_id: str) -> None:
    """v2 (M4)：按 layout_json 结构分块（chunk 携带页码+bbox）-> embedding -> 写向量索引。"""
    raise NotImplementedError  # TODO(M4)


async def startup(ctx: dict) -> None:
    ctx["http"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    ctx["redis_store"] = redis.from_url(settings.redis_url)
    ctx["task_store"] = TaskStore(ctx["redis_store"], settings.result_ttl)
    ctx["mineru_client"] = MineruClient(ctx["http"])
    ctx["registry"] = load_registry(settings.models_config)


async def shutdown(ctx: dict) -> None:
    await ctx["http"].aclose()
    await ctx["redis_store"].aclose()


class WorkerSettings:
    functions = [poll_and_archive]  # M4: + chunk_and_index
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
