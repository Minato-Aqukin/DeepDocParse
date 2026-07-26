"""service -> backend 的内网回调（SERVICE_TOKEN 鉴权，不是用户凭据）。

回调是"尽力而为"的（gateway 回调失败只记日志），所以这里只是快路径，
真正的可靠性由 reconcile.py 的对账保证。归档在请求内同步做完：
gateway 的回调读超时是 300s，而归档只是一次 GET + 几次 put。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive import archive_task, fail_task
from app.db import get_session
from app.deps import get_service_client, get_storage, require_service_token
from app.models import Task
from app.service_client import ServiceClient
from app.storage import Storage

router = APIRouter(dependencies=[Depends(require_service_token)])


class ParseCallback(BaseModel):
    task_id: str        # service 侧的 task_id
    status: str         # succeeded | failed


@router.post("/internal/parse-callback")
async def parse_callback(body: ParseCallback, request: Request,
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage),
                         service: ServiceClient = Depends(get_service_client)):
    # 一个 service 任务可能对应本层多行：service 按 doc_id 去重，不同用户上传同一份
    # 文件会拿到同一个 service task_id。每一行都要归档，不能只处理一行。
    tasks = (await session.execute(
        select(Task).where(Task.service_task_id == body.task_id)
    )).scalars().all()
    if not tasks:
        # 未知任务不报错：service 不该因为 backend 的记账问题重试回调
        return {"ok": False, "reason": "unknown task"}

    if body.status == "failed":
        try:
            live = await service.get_status(body.task_id)
            error = live.get("error") or "parse failed"
        except Exception:
            error = "parse failed"
        for task in tasks:
            await fail_task(session, task, error)
        return {"ok": True, "status": "failed", "tasks": len(tasks)}

    archived = 0
    for task in tasks:
        if not task.object_key:
            continue    # 外部任务（经 /v1/* 提交，文件不在本层）不归档别人的结果
        if await archive_task(session, storage, service, task.id):
            archived += 1
    return {"ok": True, "archived": archived}
