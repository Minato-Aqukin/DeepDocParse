"""`GET /api/parse-engines` —— 上传与重解析表单"能选哪些引擎"的唯一来源。

以前前端写死了一张引擎表（mineru + borndigital），与网关注册表、`DEFAULT_PARSE_ENGINE`
三处靠人工对齐：有 DeepSeek-OCR-2（vlm-ocr）的部署选不到它，没有 mineru 的部署
（无 GPU / AutoDL）默认却选中 mineru，提交即 404 `unknown_engine`。现在注册表里
有什么就列什么，健康不好的引擎如实标出来（不变式 2：降级可见）。
"""
from fastapi import APIRouter, Depends, Request

from ddp_corpus.capabilities import parse_engine_options
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor, current_actor

router = APIRouter()


@router.get("/api/parse-engines")
async def list_parse_engines(request: Request, _: Actor = Depends(current_actor)):
    engines, status = await parse_engine_options(getattr(request.app.state, "http", None))
    return {"default_engine": settings.default_parse_engine,
            "capability_status": status, "engines": engines}
