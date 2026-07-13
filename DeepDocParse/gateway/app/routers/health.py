"""探针。healthz=进程存活；readyz=依赖就绪（Redis + 各注册引擎连通）。"""
from fastapi import APIRouter, Response

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response):
    # TODO(M1): ping Redis；GET 各 parse_engines/vqa_models endpoint 的健康接口
    # 任一失败 -> response.status_code = 503
    return {"status": "ready", "checks": {"redis": "TODO", "engines": "TODO"}}
