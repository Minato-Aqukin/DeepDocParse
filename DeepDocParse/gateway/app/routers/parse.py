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

from app.auth import require_service_token
from app.config import settings
from app.errors import APIError
from app.services.mineru_client import MineruTaskNotFound

router = APIRouter(tags=["parse"], dependencies=[Depends(require_service_token)])

# 契约四态里的非终态（终态直接读 task hash，不再打扰 mineru）
_TERMINAL = {"succeeded", "failed"}


class ParseRequest(BaseModel):
    file_url: str          # backend 侧 MinIO 预签名 URL，不传文件流
    engine: str = "mineru"
    options: dict = {}     # 引擎透传选项（mineru: backend=pipeline|vlm 等）
    callback_url: str | None = None


def _doc_hash(file_url: str) -> str:
    return hashlib.sha256(file_url.encode()).hexdigest()


@router.post("/parse", status_code=202)
async def submit_parse(req: ParseRequest, request: Request):
    state = request.app.state

    engines = state.registry.parse_engines
    if req.engine not in engines:
        raise APIError(404, f"unknown parse engine: {req.engine}", "invalid_request_error",
                       "unknown_engine")

    # 幂等：同一 file_url 已有未失败任务则直接复用（ask_document 的重试模式依赖此行为）
    doc_hash = _doc_hash(req.file_url)
    existing_id = await state.task_store.find_by_doc_hash(doc_hash)
    if existing_id:
        existing = await state.task_store.get(existing_id)
        if existing and existing.get("status") != "failed":
            return {"task_id": existing_id}

    # 队列水位：ARQ 不做推理排队（那是 mineru 的事），这里只挡洪峰
    if await state.task_store.queue_depth() >= settings.parse_queue_max:
        raise APIError(429, "parse queue is full, retry later", "rate_limit_error", "queue_full")

    endpoint = engines[req.engine].endpoint
    try:
        mineru_task_id = await state.mineru_client.submit(endpoint, req.file_url, req.options)
    except httpx.HTTPError as exc:
        raise APIError(502, f"parse engine unreachable: {exc}", "upstream_error", "engine_error")

    task_id = uuid.uuid4().hex
    await state.task_store.create(task_id, mineru_task_id, req.engine, req.callback_url, doc_hash)
    await state.arq.enqueue_job("poll_and_archive", task_id)
    return {"task_id": task_id}


@router.get("/parse/{task_id}")
async def get_status(task_id: str, request: Request):
    state = request.app.state
    task = await state.task_store.get(task_id)
    if task is None:
        raise APIError(404, f"task not found: {task_id}", "invalid_request_error", "task_not_found")

    status = task.get("status", "pending")
    if status not in _TERMINAL:
        # 非终态实时透传 mineru（worker 归档前 hash 里只有受理时的状态）
        endpoint = state.registry.parse_engines[task["engine"]].endpoint
        try:
            live = await state.mineru_client.status(endpoint, task["mineru_task_id"])
            status = live["status"]
        except (httpx.HTTPError, MineruTaskNotFound):
            pass  # mineru 暂不可达/任务被清理时退回存储态，由 worker 兜底落终态
    progress = {"pending": 0.0, "running": 0.5, "succeeded": 1.0, "failed": 1.0}[status]
    return {
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "error": task.get("error") or None,
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
