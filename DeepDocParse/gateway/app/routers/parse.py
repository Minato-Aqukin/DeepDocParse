"""解析平面 /v1/parse —— 契约见 ../../openapi.yaml

职责边界（重要）：
- 推理排队/多卡调度 = mineru-api / mineru-router 自带，不重复实现
- 本路由只做：受理 -> task_id 映射 -> 转发 -> 状态透传
- 结果后处理（取回/归档/回调）在 worker/tasks.py 的 ARQ 链里
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_service_token

router = APIRouter(tags=["parse"], dependencies=[Depends(require_service_token)])


class ParseRequest(BaseModel):
    file_url: str          # backend 侧 MinIO 预签名 URL，不传文件流
    engine: str = "mineru"
    options: dict = {}     # 引擎透传选项（mineru: backend=pipeline|vlm 等）
    callback_url: str | None = None


@router.post("/parse", status_code=202)
async def submit_parse(req: ParseRequest, request: Request):
    """受理解析任务。

    TODO(M1):
    1. 队列水位检查（task_store.queue_depth() >= PARSE_QUEUE_MAX -> 429）
    2. registry.parse_engines[req.engine] 取 endpoint
    3. mineru_client.submit(endpoint, req.file_url, req.options) -> mineru_task_id
    4. task_store.create(our_task_id, mineru_task_id, req.callback_url)
    5. arq.enqueue_job("poll_and_archive", our_task_id)   # 后处理链
    6. return {"task_id": our_task_id}
    """
    raise HTTPException(status_code=501, detail="TODO(M1)")


@router.get("/parse/{task_id}")
async def get_status(task_id: str, request: Request):
    """状态查询：task_store 取映射 -> mineru_client.status() 透传。TODO(M1)"""
    raise HTTPException(status_code=501, detail="TODO(M1)")


@router.get("/parse/{task_id}/result")
async def get_result(task_id: str, request: Request):
    """结果获取：暂存(Redis/本地盘, TTL=RESULT_TTL)读出。TODO(M1)"""
    raise HTTPException(status_code=501, detail="TODO(M1)")
