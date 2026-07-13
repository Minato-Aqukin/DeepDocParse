"""VQA 平面 /v1/chat/completions —— OpenAI 协议透传。

这是"视觉子代理"的本体：图片 + 任务 prompt 进，针对性答案出。
gateway 在这里只做三件事：验 token、按 model 字段查注册表、流式反向代理。
协议本身不解析（除了取 model 字段），保证与 OpenAI 生态（LiteLLM/one-api）兼容。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.auth import require_service_token

router = APIRouter(tags=["vqa"], dependencies=[Depends(require_service_token)])


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """TODO(M2):
    1. body = await request.json(); model = body.get("model") 或注册表 default
    2. registry.vqa_models[model] -> endpoint；未知 model 返回 404（OpenAI 风格错误体）
    3. 并发信号量（VQA_MAX_CONCURRENCY），满载 429
    4. httpx 流式转发到 {endpoint}/v1/chat/completions，SSE 原样透传
       （dev: deepseek-ocr.rs；prod: vLLM —— 同一协议，注册表切换）
    """
    raise HTTPException(status_code=501, detail="TODO(M2)")


@router.get("/models")
async def list_models(request: Request):
    """OpenAI 兼容模型列表，来源 models.yaml。TODO(M2)"""
    registry = request.app.state.registry
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "DeepDocParse"}
            for name in registry.vqa_models
        ],
    }
