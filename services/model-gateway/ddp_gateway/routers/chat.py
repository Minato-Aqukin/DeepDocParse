"""VQA 平面 /v1/chat/completions —— OpenAI 协议透传。

这是"视觉子代理"的本体：图片 + 任务 prompt 进，针对性答案出。
gateway 在这里只做三件事：验 token、按 model 字段查注册表、流式反向代理。
协议本身不解析（除了取 model 字段），保证与 OpenAI 生态（LiteLLM/one-api）兼容。
"""
import asyncio

import httpx
from ddp_core.verification import TRANSCRIBE_PROMPT
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ddp_gateway.auth import require_service_token
from ddp_gateway.config import chat_request_defaults
from ddp_gateway.errors import APIError
from ddp_gateway.services.extraction import NO_INSTRUCT, VISION

router = APIRouter(tags=["vqa"], dependencies=[Depends(require_service_token)])

# 反代时逐跳头不透传（RFC 9110 §7.6.1；content-length 由流式重新计算）
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


def _request_has_images(body: dict) -> bool:
    """OpenAI messages 里有没有 image_url 部件。有图就必须走看得见图的模型。"""
    try:
        for message in body.get("messages") or []:
            content = (message or {}).get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
    except (AttributeError, TypeError):
        return False
    return False


def _default_chat_model(registry, *, has_images: bool) -> str:
    """按能力词挑问答缺省：跳过 no_instruct，有图时还要 vision。

    挑不到会遵循指令的条目 -> 400 no_instruct_model（可见失败，不是 200 垃圾答案）。
    有图却没有 vision 条目 -> 400 vision_unavailable。
    """
    section = registry.vqa_models
    if not section:
        raise APIError(404, "no VQA model registered (check models.yaml vqa_models)",
                       "invalid_request_error", "model_not_found")
    usable = {n: e for n, e in section.items()
              if NO_INSTRUCT not in (e.capabilities or [])}
    if not usable:
        raise APIError(400,
                       "no instruct-capable chat model registered "
                       "(vqa_models 全是 no_instruct 的 OCR 专用模型)",
                       "invalid_request_error", "no_instruct_model")
    if has_images:
        usable = {n: e for n, e in usable.items()
                  if VISION in (e.capabilities or [])}
        if not usable:
            raise APIError(400,
                           "no vision-capable chat model registered for image input",
                           "invalid_request_error", "vision_unavailable")
    try:
        name, _ = registry.default_of(usable)
    except LookupError:
        raise APIError(404, "no VQA model registered (check models.yaml vqa_models)",
                       "invalid_request_error", "model_not_found")
    return name


@router.post("/chat/completions")
async def chat_completions(request: Request):
    state = request.app.state

    try:
        body = await request.json()
    except ValueError:  # 同时覆盖 JSONDecodeError 与 UnicodeDecodeError（非 UTF-8 body）
        raise APIError(400, "request body is not valid UTF-8 JSON", "invalid_request_error",
                       "invalid_json")
    if not isinstance(body, dict):
        raise APIError(400, "request body must be a JSON object", "invalid_request_error", "invalid_json")

    registry = state.registry
    model = body.get("model")
    has_images = _request_has_images(body)
    if not model:
        model = _default_chat_model(registry, has_images=has_images)
        body["model"] = model
    entry = registry.vqa_models.get(model)
    if entry is None:
        raise APIError(404, f"model not found: {model}", "invalid_request_error", "model_not_found")

    body = {**chat_request_defaults(entry.options), **body}

    # 并发上限：满载快速失败（挡洪峰；真正的推理排队在运行时自己的 batch 里）。
    # locked() 与 acquire() 之间没有 await：asyncio 单线程里 locked() 为假时
    # acquire() 走同步快路径立即拿到 permit，不存在竞态，也不会排队。
    sem: asyncio.Semaphore = state.vqa_semaphore
    if sem.locked():
        raise APIError(429, "VQA concurrency limit reached, retry later",
                       "rate_limit_error", "vqa_overloaded")
    await sem.acquire()

    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            released = True
            sem.release()

    try:
        upstream_req = state.http.build_request(
            "POST", f"{entry.endpoint}/v1/chat/completions", json=body,
        )
        upstream = await state.http.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        release_once()
        raise APIError(502, f"vqa runtime unreachable: {exc}", "upstream_error", "vqa_unreachable")
    except BaseException:  # CancelledError 等：acquire 之后的任何退出都不能漏 permit
        release_once()
        raise

    async def cleanup() -> None:
        await upstream.aclose()
        release_once()

    async def relay():
        # 清理放 finally：上游中途断流（ReadError 等）时 Starlette 不会执行
        # BackgroundTask，只有这里能保证 permit 归还 + 连接关闭（验收回归项）
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await cleanup()

    # SSE / JSON 原样透传（状态码、content-type 一并保留）
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(cleanup),  # 兜底：响应体从未被消费时由它清理
    )


@router.get("/models")
async def list_models(request: Request):
    """OpenAI 兼容模型列表，来源 models.yaml。"""
    registry = request.app.state.registry
    default = registry.default_of(registry.vqa_models)[0] if registry.vqa_models else None
    vision_models = {name: entry for name, entry in registry.vqa_models.items()
                     if "vision" in (entry.capabilities or [])}
    vision_default = registry.default_of(vision_models)[0] if vision_models else None
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "DeepDocParse",
             "capabilities": entry.capabilities or [],
             "default": name == (vision_default if name in vision_models else default),
             **({"transcribe_prompt": str((entry.options or {}).get("transcribe_prompt")
                                         or TRANSCRIBE_PROMPT)}
                if "vision" in (entry.capabilities or []) else {})}
            for name, entry in registry.vqa_models.items()
        ],
    }
