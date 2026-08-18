"""ARQ 后处理链 —— gateway 的编排主战场。

原则：不双重排队。推理级排队/多卡调度归 mineru 自己；
ARQ 只负责推理完成后的编排：

  poll_and_archive(task_id):
    1. 轮询 mineru status 直到 succeeded/failed（指数退避，上限 POLL_TIMEOUT）
    2. fetch_result -> task_store.save_result（暂存 24h）
    3. callback_url 存在则 POST 通知 backend 取件（带 service token）
    4. 从在途集合里摘掉自己（归还队列水位，按 task_id 记名、幂等）
    v2(M4) 追加: 5. chunk_and_index(task_id)  # 结构感知分块 -> bge-m3 向量化 -> Redis 索引

启动：arq app.worker.tasks.WorkerSettings
"""
import asyncio
import time

import httpx
import redis.asyncio as redis
from arq.connections import RedisSettings

from app.config import load_registry, settings
from app.services.chunking import layout_to_chunks
from app.services.engines import resolve as resolve_engine
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
    await store.release_slot(task_id)
    await _notify_callback(ctx["http"], task, task_id, status)


async def poll_and_archive(ctx: dict, task_id: str) -> None:
    store: TaskStore = ctx["task_store"]

    task = await store.get(task_id)
    if task is None:  # TTL 过期或被清理
        return
    entry = ctx["registry"].parse_engines.get(task.get("engine", ""))
    if entry is None:
        # 引擎在任务受理后被从 models.yaml 摘掉了。落终态并归还水位，
        # 否则这个任务会一直挂在在途集合里（直接 KeyError 会让 ARQ 重试 5 次后放弃，
        # 任务永远停在 pending，水位也不还）
        await _finish(ctx, task_id, task, "failed",
                      f"parse engine no longer registered: {task.get('engine')}")
        return
    try:
        client = resolve_engine(entry, mineru_client=ctx["mineru_client"], http=ctx["http"])
    except LookupError as exc:
        await _finish(ctx, task_id, task, "failed", str(exc))
        return
    endpoint = entry.endpoint
    native_id = store.native_id_of(task)

    delay = settings.poll_initial_delay
    deadline = time.monotonic() + settings.poll_timeout
    while True:
        try:
            live = await client.status(endpoint, native_id)
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
        result = await client.fetch_result(endpoint, native_id)
    except (RuntimeError, MineruTaskNotFound, httpx.HTTPError) as exc:
        await _finish(ctx, task_id, task, "failed", str(exc))
        return
    if result is None:  # completed 但结果尚未落地：给 mineru 一次喘息后重取
        await asyncio.sleep(settings.poll_initial_delay)
        result = await client.fetch_result(endpoint, native_id)
        if result is None:
            await _finish(ctx, task_id, task, "failed", "mineru reported success but no result")
            return

    await store.save_result(task_id, result)
    await _finish(ctx, task_id, task, "succeeded")

    # v2：注册了 embedding 模型才追加分块索引链（注册表驱动开关）
    if ctx["registry"].embedding_models:
        await ctx["redis"].enqueue_job("chunk_and_index", task_id)


async def chunk_and_index(ctx: dict, task_id: str) -> None:
    """v2 (M4)：按 layout_json 结构分块（chunk 携带页码+bbox）-> embedding -> 写向量索引。

    尽力而为：失败只记日志、不抛（否则 ARQ 默认重试 5 次，每次重跑全量 embedding）。
    索引是可重建缓存，检索方（ask_document）缺索引时自动回退 BM25。
    """
    try:
        await _chunk_and_index(ctx, task_id)
    except Exception as exc:  # noqa: BLE001 - 索引失败不得影响任务终态
        print(f"[worker] chunk_and_index failed for {task_id}: {type(exc).__name__}: {exc}")


async def _chunk_and_index(ctx: dict, task_id: str) -> None:
    store: TaskStore = ctx["task_store"]
    registry = ctx["registry"]
    if not registry.embedding_models:
        return
    task = await store.get(task_id)
    if task is None or not task.get("doc_hash"):
        return
    result = await store.load_result(task_id)
    if result is None:
        return

    chunks = layout_to_chunks(result.get("layout_json") or {})
    if not chunks:
        return

    name, entry = registry.default_of(registry.embedding_models)
    # 分批：embedding 运行时对单请求条数有上限（TEI 的 max-client-batch-size），
    # 长文档一次性提交会被整批拒绝，故按 embedding_batch_size 切分后按序拼接
    vectors: list[list[float]] = []
    batch = settings.embedding_batch_size
    for start in range(0, len(chunks), batch):
        texts = [c["text"] for c in chunks[start:start + batch]]
        resp = await ctx["http"].post(
            f"{entry.endpoint}/v1/embeddings",
            json={"model": name, "input": texts},
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        if len(data) != len(texts):
            raise RuntimeError(f"embedding runtime returned {len(data)} vectors for {len(texts)} inputs")
        vectors.extend(d["embedding"] for d in data)

    await store.ensure_chunk_index(dim=len(vectors[0]))
    await store.save_chunks(task["doc_hash"], chunks, vectors)


async def startup(ctx: dict) -> None:
    ctx["http"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    ctx["redis_store"] = redis.from_url(settings.redis_url)
    ctx["task_store"] = TaskStore(ctx["redis_store"], settings.result_ttl,
                                  settings.queue_inflight_ttl)
    ctx["mineru_client"] = MineruClient(ctx["http"])
    ctx["registry"] = load_registry(settings.models_config)


async def shutdown(ctx: dict) -> None:
    await ctx["http"].aclose()
    await ctx["redis_store"].aclose()


class WorkerSettings:
    functions = [poll_and_archive, chunk_and_index]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
