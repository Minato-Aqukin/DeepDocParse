"""文档与解析任务（Web 前端视角，JWT 鉴权）。

链路：上传 -> MinIO -> 稳定文件 URL -> service /v1/parse -> 回调/对账 -> 归档 -> 索引 -> 预览/问答
注意：service 结果只暂存 24h，收到完成通知必须及时取回（见 archive.py / reconcile.py）。

Document 与 ParseJob 分离（ADR #15）：换引擎/参数重解析 = 同一 Document 下新增一个 job，
两个版本并存，用户显式切换 current_job 才会影响预览与索引。
"""
import hashlib
import json
import mimetypes
import secrets
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.archive import fail_job, image_base_url
from ddp_core.chunking import layout_to_chunks
from app.config import settings
from app.db import get_session
from app.deps import current_user, get_service_client, get_storage
from app.errors import APIError
from app.indexing import index_document
from app.models import (
    Chunk, Conversation, Document, FileToken, Message, ParseJob, User, new_id, utcnow,
)
from app.service_client import ServiceClient, ServiceError
from app.storage import Storage, prefix_of, source_key

router = APIRouter()

TERMINAL = ("succeeded", "failed")


def options_hash(engine: str, options: dict) -> str:
    """同参数重解析幂等命中已有 job；换参数才建新行。"""
    canonical = json.dumps(options or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{engine}|{canonical}".encode()).hexdigest()


class JobInfo(BaseModel):
    id: str
    engine: str
    options: dict
    status: str
    error: str | None
    page_count: int
    is_current: bool
    created_at: datetime
    archived_at: datetime | None


class DocumentInfo(BaseModel):
    id: str
    filename: str
    doc_id: str
    origin: str
    mime: str
    size_bytes: int
    page_count: int
    status: str                 # 取自 current_job / 最新 job
    error: str | None
    index_status: str
    index_error: str | None
    current_job_id: str | None
    created_at: datetime


def _doc_info(document: Document, job: ParseJob | None) -> DocumentInfo:
    return DocumentInfo(
        id=document.id, filename=document.filename, doc_id=document.doc_id,
        origin=document.origin, mime=document.mime, size_bytes=document.size_bytes,
        page_count=document.page_count,
        status=job.status if job else "pending", error=job.error if job else None,
        index_status=document.index_status, index_error=document.index_error,
        current_job_id=document.current_job_id, created_at=document.created_at,
    )


def _too_large() -> APIError:
    limit = settings.max_upload_bytes
    mib = limit / (1024 * 1024)
    shown = f"{mib:.0f} MiB" if mib >= 1 else f"{limit} bytes"
    return APIError(413, f"file exceeds the {shown} upload limit",
                    "invalid_request_error", "file_too_large")


async def _read_capped(file: UploadFile) -> bytes:
    """分片读取并卡上限。

    **不能用 `await file.read()`**：那会把整个上传体一次性拉进内存，而后续还要
    再持有一份（sha256 + put 到 MinIO）。没有上限时一个大文件就能 OOM 掉进程。

    两道判断：
    1. `file.size` 由 multipart 解析器按**实际落盘字节**累计，不是客户端的
       content-length —— 伪造请求头绕不过去，所以可以放心当快速失败用。
    2. 逐片累计是兜底：解析器没设 size 时（自定义 UploadFile / 别的解析路径）
       仍然卡得住，且超限当场中断，不把剩下的字节读完。
    """
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise _too_large()

    parts: list[bytes] = []
    total = 0
    while chunk := await file.read(settings.upload_chunk_bytes):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise _too_large()
        parts.append(chunk)
    return b"".join(parts)


async def _owned(document_id: str, user: User, session: AsyncSession) -> Document:
    document = await session.get(Document, document_id)
    # 别人的文档同样报 404，不泄露存在性
    if document is None or document.user_id != user.id or document.deleted_at is not None:
        raise APIError(404, f"document not found: {document_id}", "invalid_request_error",
                       "document_not_found")
    return document


async def _latest_job(session: AsyncSession, document: Document) -> ParseJob | None:
    if document.current_job_id:
        job = await session.get(ParseJob, document.current_job_id)
        if job is not None:
            return job
    return (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id)
        .order_by(ParseJob.created_at.desc()).limit(1)
    )).scalars().first()


async def _job_or_current(session: AsyncSession, document: Document,
                          job_id: str | None) -> ParseJob:
    job = await session.get(ParseJob, job_id) if job_id else await _latest_job(session, document)
    if job is None or job.document_id != document.id:
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    return job


async def _archived_job(session: AsyncSession, document: Document, job_id: str | None) -> ParseJob:
    job = await _job_or_current(session, document, job_id)
    if job.status == "failed":
        raise APIError(409, job.error or "parse failed", "upstream_error", "job_failed")
    if job.status != "succeeded" or not job.result_prefix:
        raise APIError(409, "result not ready yet, poll document status first",
                       "invalid_request_error", "result_not_ready")
    return job


async def _submit(session: AsyncSession, service: ServiceClient, document: Document,
                  job: ParseJob) -> None:
    """建/复用稳定文件 URL 并提交给 service。"""
    token = (await session.execute(
        select(FileToken).where(FileToken.document_id == document.id,
                                FileToken.scope == "source",
                                FileToken.revoked.is_(False))
    )).scalars().first()
    if token is None:
        token = FileToken(token=secrets.token_urlsafe(32), document_id=document.id)
        session.add(token)
    file_url = f"{settings.public_base_url}/files/{token.token}"
    # 重新提交失败的 job 时必须刷新提交时刻：对账按它判"是否已过 service 的 24h 暂存窗口"，
    # 沿用旧时间会让隔天重传的文档一提交就被判死
    job.created_at = utcnow()
    # 必须先落库：service 收到请求后会**立刻**回头下载这个 URL，
    # 令牌还在未提交的事务里就会吃 404（M5 真机 e2e 抓到过）
    await session.commit()

    try:
        job.service_task_id = await service.submit_parse(
            file_url=file_url, doc_id=document.doc_id,
            callback_url=f"{settings.public_base_url}/internal/parse-callback",
            engine=job.engine, options=job.options,
        )
    except ServiceError as exc:
        await fail_job(session, job, f"service rejected the task: {exc}")
        if exc.status_code == 429:
            raise APIError(429, "parse queue is full, retry later", "rate_limit_error",
                           "queue_full")
        raise APIError(502, f"parse service unavailable: {exc}", "upstream_error",
                       "service_unavailable")
    job.status = "pending"
    job.error = None
    await session.commit()


@router.post("", response_model=DocumentInfo, status_code=202)
async def upload(
    request: Request,
    tasks: BackgroundTasks,
    file: UploadFile,
    engine: str = Form(""),      # 留空取 settings.default_parse_engine（下方兜底）
    options: str = Form("{}"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
    service: ServiceClient = Depends(get_service_client),
):
    engine = engine or settings.default_parse_engine
    data = await _read_capped(file)
    if not data:
        raise APIError(400, "uploaded file is empty", "invalid_request_error", "empty_file")
    try:
        parsed_options = json.loads(options) if options else {}
        if not isinstance(parsed_options, dict):
            raise ValueError
    except ValueError:
        raise APIError(400, "options must be a JSON object", "invalid_request_error",
                       "bad_options")

    # doc_id = 文件内容 sha256：本层去重键，同时作为契约 doc_id 传给 service，
    # 让 service 的幂等与向量索引分块键不受 URL 变化影响
    doc_id = hashlib.sha256(data).hexdigest()
    filename = file.filename or "document.pdf"
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    document = (await session.execute(
        select(Document).where(Document.user_id == user.id, Document.doc_id == doc_id,
                               Document.origin == "web")
    )).scalar_one_or_none()

    if document is None:
        # id 显式生成而不是等 INSERT 的默认值：object_key 要在**第一次 commit 之前**
        # 就填好。先落一行 object_key 为空的 Document、再补写的话，中途崩溃就会留下
        # 一行"看着像外部提交"的 web 文档（空 object_key 是外部任务的标记），
        # reparse 会拒绝它且无从恢复。
        document = Document(id=new_id(), user_id=user.id, doc_id=doc_id, origin="web",
                            filename=filename, mime=mime, size_bytes=len(data))
        # 键里带 document.id 而不是内容哈希：按内容哈希拼的话，两个用户传同一份文件
        # 会共用一个对象，其中一方删除就会把另一方的原件也删掉
        document.object_key = source_key(document.id, filename)
        # 对象先落：行提交成功时它指向的对象一定已经存在
        await storage.put(document.object_key, data, mime)
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            # 并发上传同一文件：另一边先插入了，回退到复用分支
            await session.rollback()
            orphan = document.object_key      # 这一行没落库，它指向的对象无人引用
            document = (await session.execute(
                select(Document).where(Document.user_id == user.id, Document.doc_id == doc_id,
                                       Document.origin == "web")
            )).scalar_one_or_none()
            if document is None:
                raise
            # GC 只扫库里的行，扫不到这个孤儿对象，只能就地清掉
            try:
                await storage.delete(orphan)
            except Exception:       # noqa: BLE001 - 清不掉只是留个孤儿，不该让上传失败
                pass
    elif document.deleted_at is not None:       # 删过又传回来：复活这一行
        document.deleted_at = None
        document.object_key = document.object_key or source_key(document.id, filename)
        await storage.put(document.object_key, data, mime)
        # 删除时把 chunks 清空并置 index_status='none'。复活后如果解析结果还在，
        # 必须重新排队建索引 —— 否则文档看着好好的却永远不可问答，
        # 而对账只捞 pending，自愈不了，只能等用户自己发现去点"重建索引"
        if document.current_job_id:
            document.index_status = "pending"
            document.index_error = None
        await session.commit()

    digest = options_hash(engine, parsed_options)
    job = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id,
                               ParseJob.options_hash == digest)
    )).scalar_one_or_none()
    if job is not None and job.status != "failed":
        # 同文件同参数：直接复用，不再打 service。
        # 但复活的文档索引被清过，这里要把它推回去，否则永远不可问答
        if document.index_status == "pending":
            _schedule_index(tasks, request, document.id)
        return _doc_info(document, job)

    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=parsed_options,
                       options_hash=digest)
        session.add(job)
    else:
        job.status, job.error = "pending", None
    await session.commit()

    await _submit(session, service, document, job)
    return _doc_info(document, job)


def _latest_job_id():
    """每个 document 最新一条 job 的 id（相关子查询，每行恰好一个值）。

    **不要写成 `GROUP BY document_id HAVING max(created_at)` 再 join 回去**：
    两条 job 的 created_at 撞上（同一微秒）时那种写法会 join 出两行，
    列表页就会出现重复文档。这里 `LIMIT 1` 天然不会。
    排序补一个 id 兜底，保证撞车时的取值也是确定的。
    """
    return (
        select(ParseJob.id)
        .where(ParseJob.document_id == Document.id)
        .order_by(ParseJob.created_at.desc(), ParseJob.id.desc())
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )


@router.get("", response_model=list[DocumentInfo])
async def list_documents(user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session),
                         q: str = "", status: str = "", limit: int = 50, offset: int = 0):
    """列表页。

    过滤与分页**都在 SQL 里做**：先分页再用 Python 丢行的话，
    `?status=succeeded&limit=50` 返回的是"前 50 行里恰好成功的那些"，
    可能一条不返回而后面几页全是 —— 分页语义是坏的。
    同理 job 也要 join 出来，不能每行再查一次（一页 200 个文档 = 200+ 次往返）。
    """
    current = aliased(ParseJob)
    fallback = aliased(ParseJob)
    # 生效 job = current_job_id 指向的那条，没有就退回最新一条（与 _latest_job 一致）
    job_status = func.coalesce(current.status, fallback.status)

    stmt = (
        select(Document, current, fallback)
        .outerjoin(current, current.id == Document.current_job_id)
        .outerjoin(fallback, fallback.id == _latest_job_id())
        .where(Document.user_id == user.id, Document.deleted_at.is_(None))
    )
    if q:
        stmt = stmt.where(Document.filename.ilike(f"%{q}%"))
    if status:
        # 没有任何 job 的文档对外报 "pending"（见 _doc_info），过滤要跟着这个口径
        stmt = (stmt.where(job_status == status) if status != "pending"
                else stmt.where(or_(job_status == "pending", job_status.is_(None))))
    stmt = stmt.order_by(Document.created_at.desc()).limit(min(limit, 200)).offset(offset)

    return [_doc_info(document, current_job or fallback_job)
            for document, current_job, fallback_job in (await session.execute(stmt)).all()]


@router.get("/{document_id}", response_model=DocumentInfo)
async def get_document(document_id: str, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session),
                       service: ServiceClient = Depends(get_service_client)):
    """非终态时实时问一次 service，避免"库里还 pending 但 service 早跑完了"。"""
    document = await _owned(document_id, user, session)
    job = await _latest_job(session, document)
    if job is not None and job.status not in TERMINAL and job.service_task_id:
        try:
            live = await service.get_status(job.service_task_id)
        except Exception:
            return _doc_info(document, job)     # service 抖动：返回库里的状态，对账兜底
        if live.get("status") == "failed":
            await fail_job(session, job, live.get("error") or "parse failed")
        elif live.get("status") == "running" and job.status == "pending":
            job.status = "running"
            await session.commit()
        # succeeded 不在这里改：必须等归档完成才对外称 succeeded（结果要能立刻取）
    return _doc_info(document, job)


@router.get("/{document_id}/jobs", response_model=list[JobInfo])
async def list_jobs(document_id: str, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    document = await _owned(document_id, user, session)
    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id)
        .order_by(ParseJob.created_at.desc())
    )).scalars().all()
    return [JobInfo(id=j.id, engine=j.engine, options=j.options, status=j.status, error=j.error,
                    page_count=j.page_count, is_current=(j.id == document.current_job_id),
                    created_at=j.created_at, archived_at=j.archived_at) for j in jobs]


class ReparseRequest(BaseModel):
    engine: str = ""      # 留空取 settings.default_parse_engine（与 upload 对称）
    options: dict = {}


@router.post("/{document_id}/reparse", response_model=JobInfo, status_code=202)
async def reparse(document_id: str, req: ReparseRequest, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session),
                  service: ServiceClient = Depends(get_service_client)):
    """换引擎/参数重新解析。同参数命中已有 job 直接返回（幂等）。"""
    document = await _owned(document_id, user, session)
    # 按 origin 判，不要按 object_key 是否为空判：空 object_key 有两个含义
    # （外部提交 / 原件已被 GC 回收），混在一起会把"原件没了"报成"这是外部文档"，
    # 用户完全无从判断该怎么办
    if document.origin == "external":
        raise APIError(400, "external documents cannot be re-parsed here",
                       "invalid_request_error", "external_document")
    if not document.object_key:
        raise APIError(409, "original file is no longer available, please re-upload it",
                       "invalid_request_error", "source_missing")

    engine = req.engine or settings.default_parse_engine
    digest = options_hash(engine, req.options)
    job = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id,
                               ParseJob.options_hash == digest)
    )).scalar_one_or_none()
    if job is not None and job.status != "failed":
        return JobInfo(id=job.id, engine=job.engine, options=job.options, status=job.status,
                       error=job.error, page_count=job.page_count,
                       is_current=(job.id == document.current_job_id),
                       created_at=job.created_at, archived_at=job.archived_at)

    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=req.options,
                       options_hash=digest)
        session.add(job)
    else:
        job.status, job.error = "pending", None
    await session.commit()
    await _submit(session, service, document, job)
    return JobInfo(id=job.id, engine=job.engine, options=job.options, status=job.status,
                   error=job.error, page_count=job.page_count, is_current=False,
                   created_at=job.created_at, archived_at=job.archived_at)


class CurrentJobRequest(BaseModel):
    job_id: str


@router.put("/{document_id}/current-job", response_model=DocumentInfo)
async def set_current_job(document_id: str, req: CurrentJobRequest, tasks: BackgroundTasks,
                          request: Request,
                          user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    """切换生效的解析版本。索引跟着换版本重建——否则问答会引用到旧版本的块。"""
    document = await _owned(document_id, user, session)
    job = await session.get(ParseJob, req.job_id)
    if job is None or job.document_id != document.id:
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    if job.status != "succeeded":
        raise APIError(409, "only a succeeded job can be made current",
                       "invalid_request_error", "job_not_ready")

    document.current_job_id = job.id
    document.page_count = job.page_count
    document.index_status = "pending"
    document.index_error = None
    document.updated_at = utcnow()
    await session.commit()
    _schedule_index(tasks, request, document.id)
    return _doc_info(document, job)


@router.post("/{document_id}/reindex", response_model=DocumentInfo, status_code=202)
async def reindex(document_id: str, tasks: BackgroundTasks, request: Request,
                  user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    document = await _owned(document_id, user, session)
    job = await _latest_job(session, document)
    if job is None or job.status != "succeeded":
        raise APIError(409, "document has no archived result to index",
                       "invalid_request_error", "result_not_ready")
    document.index_status = "pending"
    document.index_error = None
    await session.commit()
    _schedule_index(tasks, request, document.id)
    return _doc_info(document, job)


def _schedule_index(tasks: BackgroundTasks, request: Request, document_id: str) -> None:
    """索引在请求返回后跑（分块+向量化对长文档要几十秒，不能让用户等）。

    进程内后台任务在多副本下也安全：index_document 自己 claim。
    """
    state = request.app.state

    async def run() -> None:
        from app.db import get_sessionmaker
        async with get_sessionmaker()() as session:
            try:
                await index_document(session, state.storage, state.http, document_id)
            except Exception as exc:      # 已在 index_document 里落 failed
                print(f"[index] {document_id} failed: {exc}")

    tasks.add_task(run)


@router.get("/{document_id}/result")
async def get_result(document_id: str, job: str = "", user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    document = await _owned(document_id, user, session)
    parse_job = await _archived_job(session, document, job or None)
    markdown = (await storage.get(f"{parse_job.result_prefix}document.md")).decode()
    images = [k.rsplit("/", 1)[-1]
              for k in await storage.list_prefix(f"{parse_job.result_prefix}images/")]
    return {"document_id": document.id, "job_id": parse_job.id, "filename": document.filename,
            "page_count": parse_job.page_count, "markdown": markdown, "images": images}


@router.get("/{document_id}/pages")
async def get_pages(document_id: str, job: str = "", user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """按页分组的块 —— 前端左右栏对齐与 bbox 高亮的数据源。

    优先读库里的 chunks（已索引），没有就现场从 layout.json 算，保证索引没跑完也能看。
    """
    document = await _owned(document_id, user, session)
    parse_job = await _archived_job(session, document, job or None)

    rows = (await session.execute(
        select(Chunk).where(Chunk.document_id == document.id,
                            Chunk.parse_job_id == parse_job.id).order_by(Chunk.seq)
    )).scalars().all()
    if rows:
        blocks = [{"chunk_id": c.id, "seq": c.seq, "page_idx": c.page_idx, "bbox": c.bbox,
                   "page_size": c.page_size, "text": c.text} for c in rows]
    else:
        layout = json.loads((await storage.get(f"{parse_job.result_prefix}layout.json")).decode())
        blocks = [{"chunk_id": None, **c} for c in
                  layout_to_chunks(layout, settings.chunk_max_chars)]
        for b in blocks:
            b.pop("char_len", None)

    pages: dict[int, list] = {}
    for block in blocks:
        pages.setdefault(block["page_idx"], []).append(block)
    return {"document_id": document.id, "job_id": parse_job.id,
            "page_count": parse_job.page_count,
            "pages": [{"page_idx": idx, "page_size": (blocks[0]["page_size"] if blocks else None),
                       "blocks": blocks}
                      for idx, blocks in sorted(pages.items())]}


@router.get("/{document_id}/layout")
async def get_layout(document_id: str, job: str = "", user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    document = await _owned(document_id, user, session)
    parse_job = await _archived_job(session, document, job or None)
    return Response(content=await storage.get(f"{parse_job.result_prefix}layout.json"),
                    media_type="application/json")


@router.get("/{document_id}/source-url")
async def source_url(document_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """原件的稳定 URL。

    前端 iframe/PDF 渲染与 MCP 调用方都拿它 —— <img>/<iframe>/fetch(no-auth) 发不出
    Authorization 头，而这个 URL 的凭证就是 token 本身（可撤销、不过期、每次都一样）。
    """
    document = await _owned(document_id, user, session)
    token = (await session.execute(
        select(FileToken).where(FileToken.document_id == document.id,
                                FileToken.scope == "source",
                                FileToken.revoked.is_(False))
    )).scalars().first()
    if token is None:
        raise APIError(404, "no active file link for this document", "invalid_request_error",
                       "file_token_missing")
    return {"url": f"{settings.public_base_url}/files/{token.token}",
            "path": f"/files/{token.token}", "mime": document.mime}


@router.get("/{document_id}/jobs/{job_id}/images/{name}")
async def get_image(document_id: str, job_id: str, name: str,
                    user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """归档后的 markdown 里的图片引用指向这里（受 JWT 保护，不用预签名，不会过期）。"""
    document = await _owned(document_id, user, session)
    if "/" in name or ".." in name:
        raise APIError(400, "invalid image name", "invalid_request_error", "invalid_name")
    job = await session.get(ParseJob, job_id)
    if job is None or job.document_id != document.id:
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    try:
        # 用 job 记下的真实前缀：迁移过来的老 job 产物不在 results/{job.id}/ 下
        data = await storage.get(f"{prefix_of(job)}images/{name}")
    except Exception:
        raise APIError(404, f"image not found: {name}", "invalid_request_error", "image_not_found")
    return Response(content=data, media_type=mimetypes.guess_type(name)[0] or "image/png")


@router.get("/{document_id}/download")
async def download(document_id: str, format: str = "md", job: str = "",
                   user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session),
                   storage: Storage = Depends(get_storage)):
    document = await _owned(document_id, user, session)
    stem = document.filename.rsplit(".", 1)[0]

    if format == "source":
        # 原件可能已被 GC 回收（软删除后重新可见的窗口、或对象存储侧被清理）
        try:
            data = await storage.get(document.object_key) if document.object_key else None
        except Exception:
            data = None
        if data is None:
            raise APIError(404, "original file is no longer available",
                           "invalid_request_error", "source_missing")
        return Response(content=data, media_type=document.mime, headers={
            "Content-Disposition": f'attachment; filename="{document.filename}"'})

    parse_job = await _archived_job(session, document, job or None)
    if format == "md":
        data, media, name = (await storage.get(f"{parse_job.result_prefix}document.md"),
                             "text/markdown; charset=utf-8", f"{stem}.md")
    elif format == "json":
        data, media, name = (await storage.get(f"{parse_job.result_prefix}layout.json"),
                             "application/json", f"{stem}.layout.json")
    elif format == "zip":
        data, media, name = await _bundle_zip(storage, parse_job), "application/zip", f"{stem}.zip"
    else:
        raise APIError(400, f"unknown format: {format}", "invalid_request_error", "bad_format")
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{name}"'})


async def _bundle_zip(storage: Storage, job: ParseJob) -> bytes:
    """markdown + 版面 + 图片打包。图片留在 images/ 下，markdown 里的引用同步改成相对路径，
    这样解压出来就能直接用编辑器打开看图。"""
    import io
    import zipfile

    markdown = (await storage.get(f"{job.result_prefix}document.md")).decode()
    image_keys = await storage.list_prefix(f"{job.result_prefix}images/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in image_keys:
            name = key.rsplit("/", 1)[-1]
            markdown = markdown.replace(f"{image_base_url(job.document_id, job.id)}/{name}",
                                        f"images/{name}")
            zf.writestr(f"images/{name}", await storage.get(key))
        zf.writestr("document.md", markdown)
        zf.writestr("layout.json", await storage.get(f"{job.result_prefix}layout.json"))
    return buf.getvalue()


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: str, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    """软删除：对象由 GC 任务回收。

    计量流水不跟着删——账单不能因为用户删了文档就消失。
    """
    document = await _owned(document_id, user, session)
    document.deleted_at = utcnow()
    await session.execute(update(FileToken).where(FileToken.document_id == document.id)
                          .values(revoked=True))
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    # 先删消息再删会话：messages 有指向 conversations 的外键，反过来会被 PG 拒掉
    await session.execute(delete(Message).where(Message.conversation_id.in_(
        select(Conversation.id).where(Conversation.document_id == document.id))))
    await session.execute(delete(Conversation).where(Conversation.document_id == document.id))
    document.index_status = "none"
    await session.commit()


@router.get("/stats/summary")
async def summary(user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    total, pages = (await session.execute(
        select(func.count(Document.id), func.coalesce(func.sum(Document.page_count), 0))
        .where(Document.user_id == user.id, Document.deleted_at.is_(None))
    )).one()
    ready = (await session.execute(
        select(func.count(Document.id)).where(Document.user_id == user.id,
                                              Document.deleted_at.is_(None),
                                              Document.index_status == "ready")
    )).scalar_one()
    return {"documents": total, "pages": pages, "askable": ready}
