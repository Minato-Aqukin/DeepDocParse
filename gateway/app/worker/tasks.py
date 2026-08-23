"""ARQ 后处理链 —— gateway 的编排主战场。

原则：不双重排队。推理级排队/多卡调度归 mineru 自己；
ARQ 只负责推理完成后的编排：

  run_extraction(task_id):            # v1.1 抽取链
    1. 备语料：向量索引 -> 已归档的解析结果派生 -> 触发解析并等它完成
    2. extraction.run（逐字段定位 -> 抽值 -> 裁剪核对）
    3. 存结果 + 回调 + 归还水位

  poll_and_archive(task_id):
    1. 轮询 mineru status 直到 succeeded/failed（指数退避，上限 POLL_TIMEOUT）
    2. fetch_result -> task_store.save_result（暂存 24h）
    3. callback_url 存在则 POST 通知 backend 取件（带 service token）
    4. 从在途集合里摘掉自己（归还队列水位，按 task_id 记名、幂等）
    v2(M4) 追加: 5. chunk_and_index(task_id)  # 结构感知分块 -> bge-m3 向量化 -> Redis 索引

启动：arq app.worker.tasks.WorkerSettings
"""
import asyncio
import hashlib
import json
import time

import httpx
import redis.asyncio as redis
from arq.connections import RedisSettings

from app.config import load_registry, settings
from app.services import extract_format, extraction
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




# ---------- v1.1：抽取链 ----------

# 等解析完成的轮询节奏。抽取任务本身不做推理，等的是解析平面 —— 不双重排队（铁律 2）
_PARSE_WAIT_INTERVAL = 2.0


async def run_extraction(ctx: dict, task_id: str) -> None:
    """抽取任务的入口。**任何异常都要落终态**，否则任务永远停在 pending、水位也不还。"""
    store: TaskStore = ctx["task_store"]
    task = await store.get_extract(task_id)
    if task is None:        # TTL 过期或被清理
        return
    try:
        await _run_extraction(ctx, task_id, task)
    except Exception as exc:  # noqa: BLE001
        await _finish_extract(ctx, task_id, task, "failed",
                              f"{type(exc).__name__}: {exc}")


async def _run_extraction(ctx: dict, task_id: str, task: dict) -> None:
    store: TaskStore = ctx["task_store"]
    payload = json.loads(task.get("payload") or "{}")
    doc_hash = task.get("doc_hash") or ""

    problems = extract_format.validate_schema(payload.get("schema"))
    if problems:
        # 受理时已经校验过一遍；能走到这里说明 payload 在 Redis 里被改过或跨版本了
        await _finish_extract(ctx, task_id, task, "failed",
                              "schema 不合法：" + "；".join(problems))
        return
    spec = extract_format.parse_schema(payload["schema"])

    await store.set_extract_status(task_id, "running", progress=0.0)
    corpus, error = await _ensure_corpus(ctx, doc_hash, payload)
    if corpus is None:
        await _finish_extract(ctx, task_id, task, "failed", error)
        return
    # 语料备好算 10%：后面按字段推进，用户看得见它在动
    await store.set_extract_status(task_id, "running", progress=0.1)

    async def on_progress(done: int, total: int) -> None:
        await store.set_extract_status(task_id, "running",
                                       progress=0.1 + 0.9 * done / max(total, 1))

    options = payload.get("options") or {}
    context = extraction.ExtractContext(
        store=store, http=ctx["http"], registry=ctx["registry"], doc_hash=doc_hash,
        file_url=payload.get("file_url") or "", verify=options.get("verify"),
        corpus=corpus, on_progress=on_progress)
    result = await extraction.run(context, spec)

    # 结果自检：不合契约就当失败，别把一份形状不对的结果交出去
    # （消费方会照着 docs/extract-format.md 写代码，形状不对是我们的问题不是它的）
    bad = extract_format.validate_result(result)
    if bad:
        await _finish_extract(ctx, task_id, task, "failed",
                              "抽取结果不合 DDP-Extract v1：" + "；".join(bad[:3]))
        return

    await store.save_extract_result(task_id, result)
    await _finish_extract(ctx, task_id, task, "succeeded")


async def _ensure_corpus(ctx: dict, doc_hash: str,
                         payload: dict) -> tuple[list[dict] | None, str | None]:
    """备好检索语料。三条路，依次退：

    1. **向量索引已建**（注册了 embedding 模型）：直接用，向量路可用
    2. **解析结果还在 24h 暂存里**：从 layout_json 现场派生分块。
       这条是无 GPU 部署下抽取平面能工作的唯一原因 —— 没注册 embedding 模型时
       worker 的 chunk_and_index 压根不跑，Redis 里没有任何分块
    3. **都没有但给了 file_url**：触发解析并等它完成，然后回到第 2 条
    """
    store: TaskStore = ctx["task_store"]

    chunks = await store.load_chunks(doc_hash)
    if chunks:
        return chunks, None

    derived = await _derive_corpus(store, doc_hash)
    if derived:
        return derived, None

    file_url = payload.get("file_url")
    if not file_url:
        return None, ("文档尚未解析：doc_hash 在 service 侧既没有分块索引也没有解析结果"
                      "（可能已过 24h 暂存期）。请先调 /v1/parse，或在请求里带上 file_url")

    error = await _parse_and_wait(ctx, doc_hash, payload)
    if error:
        return None, error
    derived = await _derive_corpus(store, doc_hash)
    if not derived:
        return None, "解析完成但没有可检索的文本块（多半是扫描件且未启用 OCR 引擎）"
    return derived, None


async def _derive_corpus(store: TaskStore, doc_hash: str) -> list[dict]:
    """从已归档的解析结果现场派生分块（带 seq，出处定位键才接得回去）。"""
    parse_task_id = await store.find_by_doc_hash(doc_hash)
    if not parse_task_id:
        return []
    result = await store.load_result(parse_task_id)
    if not result:
        return []
    chunks = layout_to_chunks(result.get("layout_json") or {})
    for i, chunk in enumerate(chunks):
        # seq 必须与 save_chunks 的键下标一致：出处的稳定定位键是 (doc_hash, seq)，
        # 两条路算出不同的 seq 会让历史抽取结果指向错误的块
        chunk["seq"] = i
        chunk.setdefault("similarity", None)
    return chunks


async def _parse_and_wait(ctx: dict, doc_hash: str, payload: dict) -> str | None:
    """触发一次解析并等到终态。返回错误信息，成功返回 None。

    **不重复实现排队**（铁律 2）：这里只是编排——提交给解析平面，然后等。
    """
    import uuid

    store: TaskStore = ctx["task_store"]
    registry = ctx["registry"]

    # **先复用已有任务**（与 routers/parse.py 的幂等块同一判据）。
    # 不复用的话：两个抽取任务打同一份未解析文档会提交两次解析、
    # 互相覆盖 doc:{doc_hash} 别名，还各占一个在途槽（绕过 parse_queue_max 准入）
    url_hash = hashlib.sha256(payload["file_url"].encode()).hexdigest()
    existing_id = await store.find_by_doc_hash(doc_hash)
    if existing_id:
        existing = await store.get(existing_id)
        if existing and existing.get("status") != "failed":
            # 复用路径也要补别名（与 routers/parse.py 的复用分支同一理由）：
            # 任务可能是上一轮还没带别名时建的，不补的话只拿得到裸 URL 的调用方
            # （ask_document）仍会另起一个任务重复解析
            if url_hash != doc_hash:
                await store.link_alias(url_hash, existing_id)
            return await _await_parse_task(store, existing_id)

    engines = registry.parse_engines
    engine_name = payload.get("engine") or ""
    if engine_name:
        entry = engines.get(engine_name)
        if entry is None:
            return f"unknown parse engine: {engine_name}"
    else:
        try:
            engine_name, entry = registry.default_of(engines)
        except LookupError:
            return "no parse engine registered (check models.yaml parse_engines)"

    try:
        engine = resolve_engine(entry, mineru_client=ctx["mineru_client"], http=ctx["http"])
        native_id = await engine.submit(entry.endpoint, payload["file_url"], dict(entry.options))
    except (LookupError, httpx.HTTPError, RuntimeError) as exc:
        return f"触发解析失败：{exc}"

    parse_task_id = uuid.uuid4().hex
    await store.create(parse_task_id, native_id, engine_name, None, doc_hash)
    # 只拿得到裸 URL 的调用方（ask_document）按 sha256(file_url) 来找；
    # 抽取请求带了 doc_id 时身份是 sha256(doc_id)，不挂别名它就找不到、会重复解析
    if url_hash != doc_hash:
        await store.link_alias(url_hash, parse_task_id)
    await ctx["redis"].enqueue_job("poll_and_archive", parse_task_id)
    return await _await_parse_task(store, parse_task_id)


async def _await_parse_task(store: TaskStore, parse_task_id: str) -> str | None:
    """等一个解析任务落终态。返回错误信息，成功返回 None。"""
    deadline = time.monotonic() + settings.poll_timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(_PARSE_WAIT_INTERVAL)
        task = await store.get(parse_task_id)
        if task is None:
            return "解析任务在等待期间消失了（Redis 被清或 TTL 过期）"
        if task.get("status") == "succeeded":
            return None
        if task.get("status") == "failed":
            return f"解析失败：{task.get('error') or '未知原因'}"
    return f"等待解析超时（{settings.poll_timeout:.0f}s）"


async def _finish_extract(ctx: dict, task_id: str, task: dict, status: str,
                          error: str | None = None) -> None:
    store: TaskStore = ctx["task_store"]
    await store.set_extract_status(task_id, status, error=error, progress=1.0)
    await store.release_slot(task_id)
    await _notify_callback(ctx["http"], task, task_id, status)


class WorkerSettings:
    functions = [poll_and_archive, chunk_and_index, run_extraction]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
