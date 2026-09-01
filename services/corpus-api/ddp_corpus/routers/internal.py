"""service -> backend 的内网回调（SERVICE_TOKEN 鉴权，不是用户凭据）。

回调是"尽力而为"的（gateway 回调失败只记日志），所以这里只是快路径，
真正的可靠性由 reconcile.py 的对账保证。归档在请求内同步做完：
gateway 的回调读超时是 300s，而归档只是一次 GET + 几次 put。

索引则丢到后台：分块 + 向量化对长文档要几十秒，不能把 gateway 的回调挂住。
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive import archive_job, fail_job
from app.db import get_session
from app.deps import get_service_client, get_storage, require_service_token
from app.indexing import index_document
from app.models import Document, ParseJob
from app.service_client import ServiceClient
from app.storage import Storage

router = APIRouter(dependencies=[Depends(require_service_token)])


class ParseCallback(BaseModel):
    task_id: str        # service 侧的 task_id
    status: str         # succeeded | failed


@router.post("/internal/parse-callback")
async def parse_callback(body: ParseCallback, request: Request, tasks: BackgroundTasks,
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage),
                         service: ServiceClient = Depends(get_service_client)):
    # 同一个 service 任务可能对应本层多个 job（service 按 doc_id 去重，
    # 同一份文档从 Web 与对外 API 都提交过）——全部推进
    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.service_task_id == body.task_id)
    )).scalars().all()
    if not jobs:
        # 未知任务不报错：service 不该因为 backend 的记账问题重试回调
        return {"ok": False, "reason": "unknown task"}

    archived = 0
    for job in jobs:
        if body.status == "failed":
            try:
                live = await service.get_status(body.task_id)
                error = live.get("error") or "parse failed"
            except Exception:
                error = "parse failed"
            await fail_job(session, job, error)
            continue

        document = await session.get(Document, job.document_id)
        if document is None or document.origin == "external":
            job.status = "succeeded"        # 外部任务：文件在调用方那儿，不归档别人的结果
            await session.commit()
            continue
        if await archive_job(session, storage, service, job.id):
            archived += 1
            _schedule_index(tasks, request, document.id)

    return {"ok": True, "archived": archived}


def _schedule_index(tasks: BackgroundTasks, request: Request, document_id: str) -> None:
    state = request.app.state

    async def run() -> None:
        from app.db import get_sessionmaker
        async with get_sessionmaker()() as session:
            try:
                await index_document(session, state.storage, state.http, document_id)
            except Exception as exc:        # 已在 index_document 里落 failed
                print(f"[index] {document_id} failed: {exc}")

    tasks.add_task(run)
