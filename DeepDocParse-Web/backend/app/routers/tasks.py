"""Web 端任务流（自家前端用，JWT 鉴权）。

链路：上传 -> MinIO -> 稳定文件 URL -> service /v1/parse -> 回调/对账 -> 归档 -> 预览/下载
注意：service 结果只暂存 24h，收到完成通知必须及时取回（见 archive.py / reconcile.py）。
"""
import hashlib
import mimetypes
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive import fail_task
from app.config import settings
from app.db import get_session
from app.deps import current_user, get_service_client, get_storage
from app.errors import APIError
from app.models import FileToken, Task, UsageRecord, User, utcnow
from app.service_client import ServiceClient, ServiceError
from app.storage import Storage, result_prefix, source_key

router = APIRouter()

TERMINAL = ("succeeded", "failed")


class TaskInfo(BaseModel):
    id: str
    filename: str
    doc_id: str
    status: str
    error: str | None
    page_count: int
    size_bytes: int
    mime: str
    engine: str
    created_at: datetime
    archived_at: datetime | None


def _info(task: Task) -> TaskInfo:
    return TaskInfo(**{f: getattr(task, f) for f in TaskInfo.model_fields})


async def _owned_task(task_id: str, user: User, session: AsyncSession) -> Task:
    task = await session.get(Task, task_id)
    # 别人的任务同样报 404，不泄露存在性
    if task is None or task.user_id != user.id:
        raise APIError(404, f"task not found: {task_id}", "invalid_request_error", "task_not_found")
    return task


@router.post("/upload", response_model=TaskInfo, status_code=202)
async def upload(
    request: Request,
    file: UploadFile,
    engine: str = Form("mineru"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
    service: ServiceClient = Depends(get_service_client),
):
    data = await file.read()
    if not data:
        raise APIError(400, "uploaded file is empty", "invalid_request_error", "empty_file")
    # doc_id = 文件内容 sha256：本层去重键，同时作为契约的 doc_id 传给 service，
    # 让 service 的幂等与向量索引分块键不受 URL 变化影响
    doc_id = hashlib.sha256(data).hexdigest()

    # 只认本平面（origin=web）的既有任务：外部任务的文件不在本层，复用它会得到
    # 一条永远取不到结果的空壳任务
    existing = (await session.execute(
        select(Task).where(Task.user_id == user.id, Task.doc_id == doc_id,
                           Task.origin == "web")
    )).scalar_one_or_none()
    if existing is not None and existing.status != "failed":
        return _info(existing)      # 已在跑或已归档：直接复用，不再打 service

    filename = file.filename or "document.pdf"
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    task = existing or Task(user_id=user.id, doc_id=doc_id, origin="web", filename=filename)
    task.filename, task.mime, task.size_bytes = filename, mime, len(data)
    task.engine, task.status, task.error = engine, "pending", None
    # 重传失败的任务时必须刷新提交时刻：对账按它判"是否已过 service 的 24h 暂存窗口"，
    # 沿用旧时间会让隔天重传的文档一提交就被判死
    task.created_at = utcnow()
    task.object_key = task.object_key or ""     # 先落库拿 id，再用 id 拼对象键
    session.add(task)
    try:
        await session.commit()
    except IntegrityError:
        # 并发上传同一文件：另一边先插入了，回退到复用分支
        await session.rollback()
        concurrent = (await session.execute(
            select(Task).where(Task.user_id == user.id, Task.doc_id == doc_id,
                               Task.origin == "web")
        )).scalar_one_or_none()
        if concurrent is None:
            raise
        return _info(concurrent)

    task.object_key = source_key(task.id, filename)
    await storage.put(task.object_key, data, mime)

    # 稳定文件 URL：service 要能下载它，且 URL 必须长期不变
    # （预签名 URL 每次签名不同 -> MCP 平面的检索缓存永远命中不到）
    token = (await session.execute(
        select(FileToken).where(FileToken.task_id == task.id, FileToken.revoked.is_(False))
    )).scalars().first()
    if token is None:
        token = FileToken(token=secrets.token_urlsafe(32), task_id=task.id)
        session.add(token)
    file_url = f"{settings.public_base_url}/files/{token.token}"
    # 必须先落库：service 收到请求后会**立刻**回头下载这个 URL，
    # 令牌还在未提交的事务里就会吃 404（真机 e2e 抓到过）
    await session.commit()

    try:
        task.service_task_id = await service.submit_parse(
            file_url=file_url, doc_id=doc_id,
            callback_url=f"{settings.public_base_url}/internal/parse-callback",
            engine=engine,
        )
    except ServiceError as exc:
        await fail_task(session, task, f"service rejected the task: {exc}")
        if exc.status_code == 429:
            raise APIError(429, "parse queue is full, retry later", "rate_limit_error",
                           "queue_full")
        raise APIError(502, f"parse service unavailable: {exc}", "upstream_error",
                       "service_unavailable")
    await session.commit()
    return _info(task)


@router.get("", response_model=list[TaskInfo])
async def list_tasks(user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session),
                     limit: int = 50, offset: int = 0):
    tasks = (await session.execute(
        select(Task).where(Task.user_id == user.id)
        .order_by(Task.created_at.desc()).limit(min(limit, 200)).offset(offset)
    )).scalars().all()
    return [_info(t) for t in tasks]


@router.get("/{task_id}", response_model=TaskInfo)
async def get_task(task_id: str, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session),
                   service: ServiceClient = Depends(get_service_client)):
    """非终态时实时问一次 service，避免"库里还 pending 但 service 早跑完了"。"""
    task = await _owned_task(task_id, user, session)
    if task.status not in TERMINAL and task.service_task_id:
        try:
            live = await service.get_status(task.service_task_id)
        except Exception:
            return _info(task)      # service 抖动：返回库里的状态，对账兜底
        if live.get("status") == "failed":
            await fail_task(session, task, live.get("error") or "parse failed")
        elif live.get("status") == "running" and task.status == "pending":
            task.status = "running"
            await session.commit()
        # succeeded 不在这里改：必须等归档完成才对外称 succeeded（结果要能立刻取）
    return _info(task)


@router.get("/{task_id}/result")
async def get_result(task_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    task = await _owned_task(task_id, user, session)
    if task.status == "failed":
        raise APIError(409, task.error or "task failed", "upstream_error", "task_failed")
    if task.status != "succeeded" or not task.result_prefix:
        raise APIError(409, "result not ready yet, poll task status first",
                       "invalid_request_error", "result_not_ready")
    markdown = (await storage.get(f"{task.result_prefix}document.md")).decode()
    images = [k.rsplit("/", 1)[-1]
              for k in await storage.list_prefix(f"{task.result_prefix}images/")]
    return {"task_id": task.id, "filename": task.filename, "page_count": task.page_count,
            "markdown": markdown, "images": images}


@router.get("/{task_id}/source-url")
async def source_url(task_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """原件的稳定 URL。

    前端 iframe / MCP 调用方都拿它 —— <img>/<iframe> 发不出 Authorization 头，
    而这个 URL 的凭证就是 token 本身（可撤销、不过期、每次都一样）。
    """
    task = await _owned_task(task_id, user, session)
    token = (await session.execute(
        select(FileToken).where(FileToken.task_id == task.id, FileToken.revoked.is_(False))
    )).scalars().first()
    if token is None:
        raise APIError(404, "no active file link for this task", "invalid_request_error",
                       "file_token_missing")
    return {"url": f"{settings.public_base_url}/files/{token.token}",
            "path": f"/files/{token.token}", "mime": task.mime}


@router.get("/{task_id}/layout")
async def get_layout(task_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    task = await _owned_task(task_id, user, session)
    if task.status != "succeeded" or not task.result_prefix:
        raise APIError(409, "result not ready yet", "invalid_request_error", "result_not_ready")
    return Response(content=await storage.get(f"{task.result_prefix}layout.json"),
                    media_type="application/json")


@router.get("/{task_id}/images/{name}")
async def get_image(task_id: str, name: str, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """归档后的 markdown 里的图片引用指向这里（受 JWT 保护，不用预签名，不会过期）。"""
    task = await _owned_task(task_id, user, session)
    if "/" in name or ".." in name:
        raise APIError(400, "invalid image name", "invalid_request_error", "invalid_name")
    try:
        data = await storage.get(f"{result_prefix(task.id)}images/{name}")
    except Exception:
        raise APIError(404, f"image not found: {name}", "invalid_request_error", "image_not_found")
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(content=data, media_type=media)


@router.get("/{task_id}/download")
async def download(task_id: str, format: str = "md", user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session),
                   storage: Storage = Depends(get_storage)):
    task = await _owned_task(task_id, user, session)
    if format == "source":
        data, media, ext = await storage.get(task.object_key), task.mime, ""
    elif task.status != "succeeded" or not task.result_prefix:
        raise APIError(409, "result not ready yet", "invalid_request_error", "result_not_ready")
    elif format == "md":
        data, media, ext = (await storage.get(f"{task.result_prefix}document.md"),
                            "text/markdown; charset=utf-8", ".md")
    elif format == "json":
        data, media, ext = (await storage.get(f"{task.result_prefix}layout.json"),
                            "application/json", ".layout.json")
    else:
        raise APIError(400, f"unknown format: {format}", "invalid_request_error", "bad_format")
    stem = task.filename.rsplit(".", 1)[0]
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{stem}{ext or "." + task.filename.rsplit(".", 1)[-1]}"',
    })


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """删除任务记录与其文件访问凭证（对象存储里的文件由清理任务回收）。

    计量流水不跟着删：账单不能因为用户删了任务就消失，只把外键解开。
    """
    task = await _owned_task(task_id, user, session)
    await session.execute(delete(FileToken).where(FileToken.task_id == task.id))
    await session.execute(
        update(UsageRecord).where(UsageRecord.task_id == task.id).values(task_id=None)
    )
    await session.delete(task)
    await session.commit()
