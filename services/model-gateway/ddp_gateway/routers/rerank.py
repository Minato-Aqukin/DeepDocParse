"""重排序平面 /v1/rerank —— 交叉编码器透传（v1.1）。

协议对齐 TEI 的 `/rerank`：`{query, texts}` 进，`[{index, score}]` 出。
选它是因为**同一类容器换个 model 就行**（TEI 已经在跑 bge-m3 做 embedding），
几乎零新增运维面 —— 这正是 plan.md D1 选中它的理由。

**未注册 rerank_models 时返回 404，而不是静默跳过重排。**
调用方据此打一个可见的降级标记。悄悄不重排会让"上了 rerank 之后没变好"
变成一个查不出原因的悬案（M4a 向量检索静默退回 BM25 就是这么拖了很久）。
"""
import json

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ddp_gateway.auth import require_service_token
from ddp_gateway.errors import APIError

router = APIRouter(tags=["rerank"], dependencies=[Depends(require_service_token)])


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    texts: list[str]
    model: str | None = None
    top_n: int | None = None


@router.post("/rerank")
async def rerank_documents(req: RerankRequest, request: Request):
    state = request.app.state
    registry = state.registry

    if not registry.rerank_models:
        raise APIError(404, "no rerank model registered (add models.yaml rerank_models)",
                       "invalid_request_error", "model_not_found")
    if not req.texts:
        return JSONResponse(content=[])

    model = req.model
    if not model:
        model, _ = registry.default_of(registry.rerank_models)
    entry = registry.rerank_models.get(model)
    if entry is None:
        raise APIError(404, f"model not found: {model}", "invalid_request_error",
                       "model_not_found")

    payload = {"query": req.query, "texts": req.texts, **entry.options}
    try:
        upstream = await state.http.post(f"{entry.endpoint}/rerank", json=payload)
        upstream.raise_for_status()
        data = upstream.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise APIError(502, f"rerank runtime unreachable: {exc}", "upstream_error",
                       "rerank_unreachable")

    if not isinstance(data, list):
        raise APIError(502, "rerank runtime returned an unexpected shape",
                       "upstream_error", "rerank_bad_response")
    # 只保留契约承诺的两个字段：TEI 还会带 text 回来，透传等于把整批候选原样回吐一遍，
    # 长文档下响应体会翻好几倍，而调用方手上本来就有这些文本
    ranked = [{"index": int(item.get("index", i)), "score": float(item.get("score", 0.0))}
              for i, item in enumerate(data) if isinstance(item, dict)]
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return JSONResponse(content=ranked[:req.top_n] if req.top_n else ranked)
