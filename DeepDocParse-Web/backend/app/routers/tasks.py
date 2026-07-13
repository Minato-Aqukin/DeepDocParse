"""Web 端任务流（自家前端用，JWT 鉴权）。

链路：上传 -> MinIO -> 预签名 URL -> DeepDocParse POST /v1/parse
     -> 状态同步（service callback 或轮询）-> 结果取回永久归档 -> 前端预览/下载
注意：service 结果只暂存 24h，收到完成通知必须及时取回。
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/upload")
async def upload():
    """TODO: 存 MinIO -> 建 tasks 记录 -> 调 service -> 返回 task"""
    raise HTTPException(status_code=501, detail="TODO")


@router.get("")
async def list_tasks():
    """历史记录（永久保留）。TODO"""
    raise HTTPException(status_code=501, detail="TODO")


@router.get("/{task_id}")
async def get_task(task_id: str):
    raise HTTPException(status_code=501, detail="TODO")


@router.post("/callback")
async def service_callback():
    """接收 DeepDocParse 完成回调 -> 触发结果取回归档。TODO"""
    raise HTTPException(status_code=501, detail="TODO")
