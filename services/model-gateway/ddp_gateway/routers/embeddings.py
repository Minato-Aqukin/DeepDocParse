"""Embedding 平面 /v1/embeddings —— OpenAI 协议透传（v2/M4）。

内部消费方：worker 的 chunk_and_index（分块向量化）、mcp_server 的 ask_document
（查询向量化）。gateway 只做：验 token、查注册表、转发。
"""
import json

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import require_service_token
from app.errors import APIError

router = APIRouter(tags=["embeddings"], dependencies=[Depends(require_service_token)])


@router.post("/embeddings")
async def create_embeddings(request: Request):
    state = request.app.state

    try:
        body = await request.json()
    except ValueError:  # 同时覆盖 JSONDecodeError 与 UnicodeDecodeError（非 UTF-8 body）
        raise APIError(400, "request body is not valid UTF-8 JSON", "invalid_request_error",
                       "invalid_json")
    if not isinstance(body, dict):
        raise APIError(400, "request body must be a JSON object", "invalid_request_error", "invalid_json")

    registry = state.registry
    if not registry.embedding_models:
        raise APIError(404, "no embedding model registered (enable models.yaml embedding_models)",
                       "invalid_request_error", "model_not_found")
    model = body.get("model")
    if not model:
        model, _ = registry.default_of(registry.embedding_models)
        body["model"] = model
    entry = registry.embedding_models.get(model)
    if entry is None:
        raise APIError(404, f"model not found: {model}", "invalid_request_error", "model_not_found")

    try:
        upstream = await state.http.post(f"{entry.endpoint}/v1/embeddings", json=body)
        payload = upstream.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise APIError(502, f"embedding runtime unreachable: {exc}", "upstream_error",
                       "embedding_unreachable")
    return JSONResponse(status_code=upstream.status_code, content=payload)
