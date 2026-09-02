"""把一次**已经完成并通过校验的上传**变成语料里的一份文档。

## 与合仓前的区别：字节流不再经过本进程

旧的 `POST /api/documents` 收 multipart、`_read_capped` 把整份文件读进
一个 `bytes`、再 `storage.put` 上去 —— 200MB 的文件就是 200MB 的常驻内存，
而扩容应用等于放大对象存储的带宽中转（违反不变式 6）。

现在的链路（§9.1）：

    浏览器 --预签名--> 对象存储        （字节流完全不经过应用进程）
    浏览器 --finalize--> control-api  （核对真实大小，异步流式校验摘要）
    control-api --DocumentSubmitted--> corpus-api   （只有元数据）
                                          └─ 本模块

所以这里拿到的是**对象键 + 已验证的 sha256**，一个字节都不读。

## 幂等

事件投递是"至少一次"的，所以本模块必须能被同一个事件重复调用而结果不变。
两道保证：
  1. `processed_events` 表按 event_id 去重（调用方 `routers/internal.py` 做）
  2. 本模块自己按 `(doc_id, origin)` 全局去重，并发下靠唯一约束兜
"""
import hashlib
import json

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.archive import fail_job
from ddp_corpus.config import settings
from ddp_corpus.errors import APIError
from ddp_corpus.models import (
    Citation, Document, DocumentUpload, Evidence, ParseJob, new_id, utcnow,
)
from ddp_corpus.control_client import ControlClient
from ddp_corpus.service_client import ServiceClient, ServiceError
from ddp_corpus.storage import Storage
from ddp_corpus.versions import advance_index_generation, next_document_version


def options_hash(engine: str, options: dict) -> str:
    """同参数重解析要幂等命中已有 job，换参数才建新行。"""
    payload = json.dumps({"engine": engine, "options": options}, sort_keys=True,
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


async def ingest_document(
    session: AsyncSession,
    storage: Storage,
    service: ServiceClient,
    control: ControlClient,
    *,
    organization_id: str,
    actor_id: str,
    object_key: str,
    filename: str,
    mime: str,
    size_bytes: int,
    doc_id: str,
    engine: str = "",
    options: dict | None = None,
) -> tuple[Document, ParseJob | None]:
    """建（或复用）Document 并排一次解析。返回 (document, job)。

    `doc_id` 是 **control-api 流式重算并验证过的**内容 sha256 —— 不是客户端
    声明的那个。它同时作为契约 `doc_id` 传给模型网关，让网关的幂等与向量
    索引分块键不受 URL 变化影响（ADR #11/#12，这个项目踩过两次）。
    """
    engine = engine or settings.default_parse_engine
    options = options or {}

    document = (await session.execute(
        select(Document).where(Document.doc_id == doc_id, Document.origin == "web")
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()

    if document is None:
        document = Document(
            id=new_id(), uploaded_by=actor_id, organization_id=organization_id,
            doc_id=doc_id, origin="web", filename=filename, mime=mime,
            size_bytes=size_bytes, object_key=object_key,
        )
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            # 并发：另一边先插入了，回退到复用分支。
            # **本次上传的对象成了孤儿** —— GC 只扫库里的行，扫不到它，
            # 所以就地清掉（与合仓前那段并发处理是同一条逻辑）
            await session.rollback()
            try:
                await storage.delete(object_key)
            except Exception:      # noqa: BLE001 —— 清不掉只是留个孤儿，不该让入库失败
                pass
            document = (await session.execute(
                select(Document).where(Document.doc_id == doc_id, Document.origin == "web")
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if document is None:
                raise
    elif document.deleted_at is not None:
        await _revive(session, document, object_key)
    elif document.object_key and document.object_key != object_key:
        # 同一份内容再传一次：Document 复用旧对象，新对象是多余的。
        # **不留着**：它无人引用，GC 扫不到，会永久占着存储与账单
        try:
            await storage.delete(object_key)
        except Exception:      # noqa: BLE001
            pass

    # **归属：谁传过都记一笔。** 全局去重之后第二个人传同一份文件不会产生新的
    # Document，但"他也传过"这件事不能丢 —— 删除权限判它，界面上也要说得清
    # 这份语料从哪来。用 SAVEPOINT + 唯一约束兜并发。
    if not await session.scalar(
            select(DocumentUpload.id).where(DocumentUpload.document_id == document.id,
                                            DocumentUpload.user_id == actor_id).limit(1)):
        try:
            async with session.begin_nested():
                session.add(DocumentUpload(id=new_id(), document_id=document.id,
                                           user_id=actor_id))
        except IntegrityError:
            pass        # 并发下另一边先记上了，正是想要的结果
        await session.commit()

    digest = options_hash(engine, options)
    job = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id,
                               ParseJob.options_hash == digest)
    )).scalar_one_or_none()
    if job is not None and job.status != "failed":
        return document, job        # 同文件同参数：复用，不再打网关

    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=options,
                       initiated_by=actor_id, options_hash=digest,
                       document_version=await next_document_version(session, document.id))
        session.add(job)
    else:
        job.status, job.error = "pending", None
    await session.commit()

    await submit_parse(session, control, service, document, job)
    return document, job


async def _revive(session: AsyncSession, document: Document, object_key: str) -> None:
    """删过又传回来。

    删除时把 chunks 清空并置 `index_status='none'`。复活后如果解析结果还在，
    必须重新排队建索引 —— 否则文档看着好好的却永远不可问答，
    而对账只捞 pending，自愈不了，只能等用户自己发现去点"重建索引"。
    """
    if document.current_job_id:
        has_citations = bool(await session.scalar(
            select(func.count(Citation.id)).join(
                Evidence, Citation.evidence_id == Evidence.id
            ).where(Evidence.document_id == document.id)
        ))
        if has_citations:
            # 老出处按旧分块切出来的 seq 存的，直接重建会指错块。
            # 所以标 failed 并要求先做版本校验 —— 这是"不静默"的一种
            values = {
                "deleted_at": None, "object_key": object_key,
                "index_status": "failed",
                "index_error": "文档已复活且存在历史出处；请先执行版本校验，确认后再重建索引",
                "compile_status": "failed",
                "compile_degraded": ["reindex_validation_required"],
                "index_lease_until": None, "updated_at": utcnow(),
            }
        else:
            values = {
                "deleted_at": None, "object_key": object_key,
                "index_status": "pending", "index_error": None,
                "compile_status": "pending", "compile_degraded": [],
                "index_lease_until": None, "updated_at": utcnow(),
            }
        generation = await advance_index_generation(session, document.id, deleted=True,
                                                    values=values)
        if generation is None:
            raise APIError(409, "document revival raced with another request; retry",
                           "invalid_request_error", "document_state_changed")
    else:
        revived = await session.execute(
            update(Document).where(Document.id == document.id,
                                   Document.deleted_at.is_not(None)).values(
                deleted_at=None, object_key=object_key, updated_at=utcnow()))
        if revived.rowcount == 0:
            raise APIError(409, "document revival raced with another request; retry",
                           "invalid_request_error", "document_state_changed")
    await session.refresh(document)
    await session.commit()


async def submit_parse(session: AsyncSession, control: ControlClient,
                       service: ServiceClient, document: Document, job: ParseJob) -> None:
    """把任务交给模型网关。

    **传的是稳定文件 URL**（`/files/{token}`，由 control-api 提供），不是预签名 ——
    URL 一变，网关的幂等与向量索引分块键全部失效（ADR #11/#12）。
    """
    file_url = await control.stable_file_url(
        organization_id=document.organization_id, document_id=document.id,
        object_key=document.object_key, mime=document.mime)

    # 重新提交失败的 job 时必须刷新提交时刻：对账按它判"是否已过网关的 24h
    # 暂存窗口"，沿用旧时间会让隔天重传的文档一提交就被判死
    job.created_at = utcnow()
    # **必须先落库**：网关收到请求后会立刻回头下载这个 URL，
    # 令牌还在未提交的事务里就会吃 404（M5 真机 e2e 抓到过）
    await session.commit()

    try:
        task_id = await service.submit_parse(
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
    job.service_task_id = task_id
    job.status = "pending"
    job.error = None
    await session.commit()
