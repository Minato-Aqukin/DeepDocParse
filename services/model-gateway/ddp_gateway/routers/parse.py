"""解析平面 /v1/parse —— 契约见 ../../openapi.yaml

职责边界（重要）：
- 推理排队/多卡调度 = mineru-api / mineru-router 自带，不重复实现
- 本路由只做：受理 -> task_id 映射 -> 转发 -> 状态透传
- 结果后处理（取回/归档/回调）在 worker/tasks.py 的 ARQ 链里
"""
import hashlib
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ddp_gateway.auth import require_service_token
from ddp_gateway.config import settings
from ddp_gateway.errors import APIError
from ddp_gateway.services.engines import resolve as resolve_engine
from ddp_gateway.services.mineru_client import MineruTaskNotFound

router = APIRouter(tags=["parse"], dependencies=[Depends(require_service_token)])

# 契约四态里的非终态（终态直接读 task hash，不再打扰 mineru）
_TERMINAL = {"succeeded", "failed"}


class ParseRequest(BaseModel):
    file_url: str          # 文件的可下载 URL（backend 侧生成），不传文件流
    doc_id: str | None = None   # 稳定文档标识（建议文件内容 sha256），见 _doc_hash
    # 留空取注册表里标了 default 的那条。**不要写死引擎名**：写死等于让路由层
    # 认识具体引擎（违反铁律 3），且会架空 models.yaml 的 default 标记 ——
    # models.cpu.yaml 只注册 borndigital 时，写死的 "mineru" 会让缺省请求 404
    engine: str = ""
    options: dict = {}     # 引擎透传选项（mineru: backend=pipeline|vlm 等）
    callback_url: str | None = None


def _doc_hash(file_url: str, doc_id: str | None = None) -> str:
    """文档身份（幂等复用任务 + v2 向量索引分块键）。

    优先用调用方给的 doc_id：URL 每次都变的场景（MinIO 预签名、临时下载链接）下，
    只哈希 URL 会让同一文档每次算出不同身份 —— 幂等失效、向量索引永不命中。
    缺省仍回退 sha256(file_url)，保持无 doc_id 调用方（如 mcp_server）的既有行为。
    """
    return hashlib.sha256((doc_id or file_url).encode()).hexdigest()


@router.post("/parse", status_code=202)
async def submit_parse(req: ParseRequest, request: Request):
    state = request.app.state

    engines = state.registry.parse_engines
    if req.engine:
        if req.engine not in engines:
            raise APIError(404, f"unknown parse engine: {req.engine}", "invalid_request_error",
                           "unknown_engine")
        engine_name, entry = req.engine, engines[req.engine]
    else:
        try:
            engine_name, entry = state.registry.default_of(engines)
        except LookupError:
            raise APIError(404, "no parse engine registered (check models.yaml parse_engines)",
                           "invalid_request_error", "unknown_engine")

    # 幂等：同一文档已有未失败任务则直接复用（ask_document 的重试模式依赖此行为）
    doc_hash = _doc_hash(req.file_url, req.doc_id)
    url_hash = _doc_hash(req.file_url)
    existing_id = await state.task_store.find_by_doc_hash(doc_hash)
    if existing_id:
        existing = await state.task_store.get(existing_id)
        if existing and existing.get("status") != "failed":
            # 复用路径也要补别名：文档可能是上一轮（还没带别名时）建的，
            # 不补的话 URL-only 调用方仍会另起一个任务
            if url_hash != doc_hash:
                await state.task_store.link_alias(url_hash, existing_id)
            return {"task_id": existing_id}

    # 队列水位：ARQ 不做推理排队（那是 mineru 的事），这里只挡洪峰
    if await state.task_store.queue_depth() >= settings.parse_queue_max:
        raise APIError(429, "parse queue is full, retry later", "rate_limit_error", "queue_full")

    merged_options = {**entry.options, **req.options}  # 注册表默认 + 请求覆盖
    # 用哪个适配器由注册表的 runtime 决定，路由层不认识任何具体引擎（铁律 3）
    try:
        engine = resolve_engine(entry, mineru_client=state.mineru_client, http=state.http)
    except LookupError as exc:
        # 注册表把 runtime 写成了没人认识的值 —— 这是部署配置错，不是调用方的错，
        # 所以是 5xx 且归到 upstream_error（invalid_request_error 会让调用方去改自己的请求）
        raise APIError(500, str(exc), "upstream_error", "unknown_runtime")
    try:
        native_task_id = await engine.submit(entry.endpoint, req.file_url, merged_options)
    except httpx.HTTPError as exc:
        raise APIError(502, f"parse engine unreachable: {exc}", "upstream_error", "engine_error")

    task_id = uuid.uuid4().hex
    await state.task_store.create(task_id, native_task_id, engine_name, req.callback_url, doc_hash)
    # 带 doc_id 时再按 URL 挂一个别名：只拿得到裸 URL 的调用方（ask_document）
    # 否则会算出另一个身份、重复解析同一份文档
    if url_hash != doc_hash:
        await state.task_store.link_alias(url_hash, task_id)
    await state.arq.enqueue_job("poll_and_archive", task_id)
    return {"task_id": task_id}


@router.get("/parse/{task_id}")
async def get_status(task_id: str, request: Request):
    state = request.app.state
    task = await state.task_store.get(task_id)
    if task is None:
        raise APIError(404, f"task not found: {task_id}", "invalid_request_error", "task_not_found")

    status = task.get("status", "pending")
    entry = state.registry.parse_engines.get(task.get("engine", ""))
    if status not in _TERMINAL and entry is not None:
        # 非终态实时透传 mineru（worker 归档前 hash 里只有受理时的状态）。
        # 引擎可能已经从 models.yaml 里摘掉，而它受理的任务还在 24h 窗口内 ——
        # 那时查不了实时状态，退回存储态即可，不该让状态查询 500
        try:
            engine = resolve_engine(entry, mineru_client=state.mineru_client, http=state.http)
            live = await engine.status(entry.endpoint,
                                       state.task_store.native_id_of(task))
            # 契约保证：status=succeeded ⇒ result 立即可取。mineru 已完成但 worker
            # 还没归档时对外仍报 running，避免调用方拿到 succeeded 却取不到结果
            status = "running" if live["status"] == "succeeded" else live["status"]
        except (httpx.HTTPError, MineruTaskNotFound, LookupError):
            pass  # 引擎暂不可达/任务被清理时退回存储态，由 worker 兜底落终态
    progress = {"pending": 0.0, "running": 0.5, "succeeded": 1.0, "failed": 1.0}[status]
    return {
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "error": task.get("error") or None,
        # 文档身份：v2 的分块索引就按它建键。只拿得到裸 URL 的调用方
        # （ask_document）必须用这里返回的值，自己哈希 URL 会算错（身份可能来自 doc_id）
        "doc_hash": task.get("doc_hash") or None,
    }


@router.get("/parse/{task_id}/result")
async def get_result(task_id: str, request: Request):
    state = request.app.state
    task = await state.task_store.get(task_id)
    if task is None:
        raise APIError(404, f"task not found: {task_id}", "invalid_request_error", "task_not_found")
    if task.get("status") == "failed":
        raise APIError(409, task.get("error") or "task failed", "upstream_error", "task_failed")

    result = await state.task_store.load_result(task_id)
    if result is None:
        raise APIError(409, "result not ready yet, poll status first", "invalid_request_error",
                       "result_not_ready")
    return result
