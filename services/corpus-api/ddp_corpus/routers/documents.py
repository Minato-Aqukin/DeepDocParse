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

import httpx

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ddp_corpus.archive import fail_job, image_base_url
from ddp_core.chunking import layout_to_chunks
from ddp_corpus.config import settings
from ddp_corpus.control_client import ControlClient
from ddp_corpus.directory import display_names
from ddp_corpus.ingest import options_hash, submit_parse
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_service_client, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.indexing import index_document
from ddp_corpus.models import (
    Assertion, Chunk, Citation, Conversation, Document, DocumentUpload, Evidence, Message,
    ParseJob,
    as_aware, new_id, utcnow,
)
from ddp_corpus.service_client import ServiceClient, ServiceError
from ddp_corpus.storage import Storage, prefix_of, source_key
from ddp_corpus.versions import advance_index_generation, next_document_version
from ddp_core.anchor import digest_of, same_content
from ddp_core.compilation import (
    code_detection_of, compile_chunks, fingerprint, provider_of, source_anchor,
)

router = APIRouter()

TERMINAL = ("succeeded", "failed")



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
    document_version: int


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
    compile_status: str
    compile_degraded: list[str]
    compile_fingerprint: str
    layout_version: str
    code_detection: str
    current_job_id: str | None
    created_at: datetime
    # 语料共享之后（1b）文档库里会有别人传的东西，界面得说得清**这份是谁传的**、
    # 以及**当前用户能不能删** —— 否则用户只能全选、点删、然后吃一把 403。
    # `uploaders` 是全部上传者的用户名（同一份文件可能好几个人先后传过）
    uploaders: list[str] = []
    can_delete: bool = False


class IndexValidation(BaseModel):
    status: str                 # current | stale | uncompiled
    observed_fingerprints: list[str]
    expected_fingerprint: str
    reasons: list[str]
    citation_reconnectable: int
    citation_invalidations: int
    safe_to_reindex: bool


def _doc_info(document: Document, job: ParseJob | None, *,
              uploaders: list[str] | None = None, can_delete: bool = False) -> DocumentInfo:
    return DocumentInfo(
        id=document.id, filename=document.filename, doc_id=document.doc_id,
        origin=document.origin, mime=document.mime, size_bytes=document.size_bytes,
        page_count=document.page_count,
        status=job.status if job else "pending", error=job.error if job else None,
        index_status=document.index_status, index_error=document.index_error,
        compile_status=document.compile_status,
        compile_degraded=document.compile_degraded or [],
        compile_fingerprint=document.compile_fingerprint,
        layout_version=document.layout_version,
        code_detection=document.code_detection or "unavailable",
        current_job_id=document.current_job_id, created_at=document.created_at,
        uploaders=uploaders or [], can_delete=can_delete,
    )


async def _uploaders_of(session: AsyncSession, document_ids: list[str],
                        http: httpx.AsyncClient | None = None) -> dict[str, list[str]]:
    """document_id -> 上传者显示名列表。**一次查完，别在循环里查**。

    用户住在 control schema（Go 拥有），所以名字要问 control-api ——
    理由与代价见 `ddp_corpus/directory.py`。拿不到名字时退回占位名，
    不会让整个列表失败。
    """
    if not document_ids:
        return {}
    rows = (await session.execute(
        select(DocumentUpload.document_id, DocumentUpload.user_id)
        .where(DocumentUpload.document_id.in_(document_ids))
        .order_by(DocumentUpload.created_at)
    )).all()
    names = {}
    if http is not None:
        names = await display_names(http, [actor_id for _, actor_id in rows])
    out: dict[str, list[str]] = {}
    for doc_id, actor_id in rows:
        out.setdefault(doc_id, []).append(names.get(actor_id) or f"用户 {actor_id[:8]}")
    return out



async def _visible(document_id: str, session: AsyncSession) -> Document:
    """取一份语料里的文档。**不按用户过滤** —— 语料是整个部署共享的。

    从 `_owned` 改名而来（1b，plan.md §2 已定 2）：一次部署 = 一份语料 =
    一个知识库，账号层只管认证 / 计量 / 限速，**不管授权**。
    全站唯一残留的授权判断是删除权限，见 `_may_delete`。

    只剩软删除这一个可见性条件 —— 删掉的东西谁都不该再看见。
    """
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise APIError(404, f"document not found: {document_id}", "invalid_request_error",
                       "document_not_found")
    return document


async def _may_delete(document: Document, actor: Actor, session: AsyncSession) -> bool:
    """**全站唯一一处授权判断**：谁能删这份文档。

    判据是"传过它的人，或管理员"。注意判的是 `document_uploads` 整张表而不是
    `uploaded_by` 那一个字段 —— 全局去重之后，第二个传同一份文件的人不会产生
    新的 Document，但他确实也传过，凭什么不让他删。
    """
    if actor.can_delete_document:
        return True
    return bool(await session.scalar(
        select(DocumentUpload.id).where(DocumentUpload.document_id == document.id,
                                        DocumentUpload.user_id == actor.id).limit(1)))


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


# ---------------------------------------------------------------------------
# **旧的 `POST /api/documents` multipart 上传端点已删除。**
#
# 它把整份文件读进一个 `bytes` 再 put 到对象存储 —— 200MB 的文件就是 200MB
# 的常驻内存，而扩容应用等于放大对象存储的带宽中转（违反不变式 6）。
#
# 新链路见 `ddp_corpus/ingest.py` 的模块说明：浏览器凭预签名直传对象存储，
# control-api 校验后发 DocumentSubmitted 事件，本服务在
# `routers/internal.py` 里消费它。**字节流不再经过任何应用进程。**
# ---------------------------------------------------------------------------


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
async def list_documents(request: Request, actor: Actor = Depends(current_actor),
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
        .where(Document.deleted_at.is_(None))
    )
    if q:
        stmt = stmt.where(Document.filename.ilike(f"%{q}%"))
    if status:
        # 没有任何 job 的文档对外报 "pending"（见 _doc_info），过滤要跟着这个口径
        stmt = (stmt.where(job_status == status) if status != "pending"
                else stmt.where(or_(job_status == "pending", job_status.is_(None))))
    stmt = stmt.order_by(Document.created_at.desc()).limit(min(limit, 200)).offset(offset)

    rows = (await session.execute(stmt)).all()
    uploaders = await _uploaders_of(session, [d.id for d, _, _ in rows], request.app.state.http)
    mine = {d.id for d, _, _ in rows if actor.can_delete_document or actor.id in uploaders.get(d.id, [])}
    return [_doc_info(document, current_job or fallback_job,
                      uploaders=uploaders.get(document.id, []),
                      can_delete=document.id in mine)
            for document, current_job, fallback_job in rows]


@router.get("/{document_id}", response_model=DocumentInfo)
async def get_document(document_id: str, request: Request,
                       actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session),
                       service: ServiceClient = Depends(get_service_client)):
    """非终态时实时问一次 service，避免"库里还 pending 但 service 早跑完了"。"""
    document = await _visible(document_id, session)
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
    return _doc_info(document, job,
                     uploaders=(await _uploaders_of(session, [document.id], request.app.state.http)).get(document.id, []),
                     can_delete=await _may_delete(document, actor, session))


@router.get("/{document_id}/jobs", response_model=list[JobInfo])
async def list_jobs(document_id: str, actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    document = await _visible(document_id, session)
    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id)
        .order_by(ParseJob.created_at.desc())
    )).scalars().all()
    return [JobInfo(id=j.id, engine=j.engine, options=j.options, status=j.status, error=j.error,
                    page_count=j.page_count, is_current=(j.id == document.current_job_id),
                    created_at=j.created_at, archived_at=j.archived_at,
                    document_version=j.document_version) for j in jobs]


class ReparseRequest(BaseModel):
    engine: str = ""      # 留空取 settings.default_parse_engine（与 upload 对称）
    options: dict = {}


@router.post("/{document_id}/reparse", response_model=JobInfo, status_code=202)
async def reparse(document_id: str, req: ReparseRequest, request: Request,
                  actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session),
                  service: ServiceClient = Depends(get_service_client)):
    """换引擎/参数重新解析。同参数命中已有 job 直接返回（幂等）。"""
    document = await _visible(document_id, session)
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
                       created_at=job.created_at, archived_at=job.archived_at,
                       document_version=job.document_version)

    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=req.options,
                       initiated_by=actor.id,
                       options_hash=digest,
                       document_version=await next_document_version(session, document.id))
        session.add(job)
    else:
        job.status, job.error = "pending", None
    await session.commit()
    await submit_parse(session, ControlClient(request.app.state.http), service, document, job)
    return JobInfo(id=job.id, engine=job.engine, options=job.options, status=job.status,
                   error=job.error, page_count=job.page_count, is_current=False,
                   created_at=job.created_at, archived_at=job.archived_at,
                   document_version=job.document_version)


class CurrentJobRequest(BaseModel):
    job_id: str
    acknowledge_invalidations: bool = False


def _index_lease_active(document: Document) -> bool:
    lease = as_aware(document.index_lease_until)
    return document.index_status == "indexing" and lease is not None and lease > utcnow()


@router.put("/{document_id}/current-job", response_model=DocumentInfo)
async def set_current_job(document_id: str, req: CurrentJobRequest, tasks: BackgroundTasks,
                          request: Request,
                          actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session),
                          storage: Storage = Depends(get_storage)):
    """切换生效的解析版本。索引跟着换版本重建——否则问答会引用到旧版本的块。"""
    document = await _visible(document_id, session)
    job = await session.get(ParseJob, req.job_id)
    if job is None or job.document_id != document.id:
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    if job.status != "succeeded":
        raise APIError(409, "only a succeeded job can be made current",
                       "invalid_request_error", "job_not_ready")
    validated_current_job_id = document.current_job_id
    validation = await _validate_index(document, job, session, storage)
    if not validation.safe_to_reindex and not req.acknowledge_invalidations:
        raise APIError(
            409,
            f"切换版本会使 {validation.citation_invalidations} 条当前出处显式失效；"
            "请先查看版本校验结果并确认",
            "invalid_request_error", "index_version_unsafe")

    generation = await advance_index_generation(
        session, document.id, expected_current_job_id=validated_current_job_id,
        deleted=False, values={
            "current_job_id": job.id, "page_count": job.page_count,
            "index_status": "pending", "index_error": None,
            "compile_status": "pending", "compile_degraded": [],
            "index_lease_until": None, "updated_at": utcnow(),
        })
    if generation is None:
        raise APIError(409, "current parse version changed during validation; retry",
                       "invalid_request_error", "index_version_changed")
    await session.refresh(document)
    await session.commit()
    _schedule_index(tasks, request, document.id)
    return _doc_info(document, job)


@router.post("/{document_id}/reindex", response_model=DocumentInfo, status_code=202)
async def reindex(document_id: str, tasks: BackgroundTasks, request: Request,
                  acknowledge_invalidations: bool = False,
                  actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session),
                  storage: Storage = Depends(get_storage)):
    document = await _visible(document_id, session)
    job = await _latest_job(session, document)
    if job is None or job.status != "succeeded":
        raise APIError(409, "document has no archived result to index",
                       "invalid_request_error", "result_not_ready")
    if document.index_status == "pending" or _index_lease_active(document):
        raise APIError(409, "document index build is already in progress",
                       "invalid_request_error", "index_in_progress")
    validation = await _validate_index(document, job, session, storage)
    if not validation.safe_to_reindex and not acknowledge_invalidations:
        raise APIError(
            409,
            f"重建会使 {validation.citation_invalidations} 条历史出处显式失效；"
            "请先查看版本校验结果，确认后带 acknowledge_invalidations=true 重试",
            "invalid_request_error", "index_version_unsafe")
    # validate 期间旧 worker 可能刚好续租/完成；锁住并重读，不能拿检查前的过期
    # lease 去误杀一个仍活着的 generation。
    document = (await session.execute(
        select(Document).where(Document.id == document_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    if document.current_job_id != job.id:
        raise APIError(409, "current parse version changed during validation; retry",
                       "invalid_request_error", "index_version_changed")
    if document.index_status == "pending" or _index_lease_active(document):
        raise APIError(409, "document index build is already in progress",
                       "invalid_request_error", "index_in_progress")
    generation = await advance_index_generation(
        session, document.id, expected_current_job_id=job.id, deleted=False,
        values={
            "index_status": "pending", "index_error": None,
            "compile_status": "pending", "compile_degraded": [],
            "index_lease_until": None, "updated_at": utcnow(),
        })
    if generation is None:
        raise APIError(409, "document state changed during validation; retry",
                       "invalid_request_error", "index_version_changed")
    await session.refresh(document)
    await session.commit()
    _schedule_index(tasks, request, document.id)
    return _doc_info(document, job)


@router.post("/{document_id}/validate-index", response_model=IndexValidation)
async def validate_index(document_id: str, job_id: str = "",
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage)):
    """只读校验 provider 与老出处回接；绝不在背后触发重建。"""
    document = await _visible(document_id, session)
    job = await session.get(ParseJob, job_id) if job_id else await _latest_job(session, document)
    if job is not None and job.document_id != document.id:
        job = None
    if job is None or job.status != "succeeded" or not job.result_prefix:
        raise APIError(409, "document has no archived result to validate",
                       "invalid_request_error", "result_not_ready")
    return await _validate_index(document, job, session, storage)


async def _validate_index(document: Document, job: ParseJob, session: AsyncSession,
                          storage: Storage) -> IndexValidation:
    layout = json.loads((await storage.get(f"{job.result_prefix}layout.json")).decode())
    expected_provider = provider_of(
        layout=layout, parse_options_hash=job.options_hash,
        embedding_model=settings.embedding_model, vision_model=settings.chat_model)
    expected = fingerprint(expected_provider)
    rows = (await session.execute(
        select(Chunk).where(Chunk.document_id == document.id,
                            Chunk.parse_job_id == job.id).order_by(Chunk.seq)
    )).scalars().all()
    observed = sorted({c.provider_fingerprint for c in rows if c.provider_fingerprint})
    reasons: list[str] = []
    if not rows:
        status = "uncompiled"
        reasons.append("no_compiled_chunks")
    elif not expected_provider["provider_resolved"] or any(
            not (c.provider or {}).get("provider_resolved", False) for c in rows):
        status = "unresolved"
        reasons.append("provider_unresolved")
    elif observed == [expected]:
        status = "current"
    else:
        status = "stale"
        if len(observed) > 1:
            reasons.append("mixed_provider_fingerprints")
        current_provider = rows[0].provider or {}
        for field, value in expected_provider.items():
            if current_provider.get(field) != value:
                reasons.append(f"{field}_changed")
        if not reasons:
            reasons.append("provider_fingerprint_changed")
    if code_detection_of(layout) == "unavailable":
        reasons.append("code_detection_unavailable")

    candidate = {}
    for chunk in compile_chunks(
            layout, max_chars=settings.chunk_max_chars, provider=expected_provider):
        candidate[source_anchor(
            seq=chunk["seq"], content_digest=digest_of(chunk["text"]),
            page_idx=chunk["page_idx"], bbox=chunk.get("bbox"))] = chunk
    cited = (await session.execute(
        select(Citation, Evidence).join(Evidence, Citation.evidence_id == Evidence.id)
        .where(Evidence.parse_job_id == job.id)
    )).all()
    reconnectable = invalidations = 0
    for citation, evidence in cited:
        chunk = candidate.get(source_anchor(
            seq=evidence.seq, content_digest=evidence.content_digest,
            page_idx=evidence.page_idx, bbox=evidence.bbox))
        # 生成理解每次调用模型都可能变化，不能拿旧描述冒充“必定能接回”。
        if evidence.derived_from:
            invalidations += 1
            continue
        if chunk is not None and same_content(
                snippet=citation.snippet, chunk_text=chunk["text"],
                digest=citation.content_digest):
            reconnectable += 1
        else:
            invalidations += 1
    if document.current_job_id and document.current_job_id != job.id:
        replacement_invalidations = await _resolved_citation_count(
            session, document.current_job_id)
        if replacement_invalidations:
            invalidations += replacement_invalidations
            reasons.append("current_version_citations_will_invalidate")
    if invalidations:
        reasons.append("historical_citations_will_invalidate")
    reasons = list(dict.fromkeys(reasons))
    return IndexValidation(
        status=status, observed_fingerprints=observed,
        expected_fingerprint=expected, reasons=reasons,
        citation_reconnectable=reconnectable, citation_invalidations=invalidations,
        safe_to_reindex=(invalidations == 0),
    )


async def _resolved_citation_count(session: AsyncSession, job_id: str) -> int:
    """切版本时统计当前仍能接回、会因替换整份 Chunk 集而失效的出处。"""
    cited = (await session.execute(
        select(Citation, Evidence).join(Evidence, Citation.evidence_id == Evidence.id)
        .where(Evidence.parse_job_id == job_id)
    )).all()
    if not cited:
        return 0
    chunks = {
        c.seq: c for c in (await session.execute(
            select(Chunk).where(Chunk.parse_job_id == job_id)
        )).scalars().all()
    }
    total = 0
    for citation, evidence in cited:
        chunk = chunks.get(evidence.seq)
        if chunk is None:
            continue
        live_id = chunk.derived_evidence_id if evidence.derived_from else chunk.evidence_id
        live_text = (chunk.derived_text or "") if evidence.derived_from else chunk.text
        if live_id == evidence.id and same_content(
                snippet=citation.snippet, chunk_text=live_text,
                digest=citation.content_digest):
            total += 1
    return total


def _schedule_index(tasks: BackgroundTasks, request: Request, document_id: str) -> None:
    """索引在请求返回后跑（分块+向量化对长文档要几十秒，不能让用户等）。

    进程内后台任务在多副本下也安全：index_document 自己 claim。
    """
    state = request.app.state

    async def run() -> None:
        from ddp_corpus.db import get_sessionmaker
        async with get_sessionmaker()() as session:
            try:
                await index_document(session, state.storage, state.http, document_id)
            except Exception as exc:      # 已在 index_document 里落 failed
                print(f"[index] {document_id} failed: {exc}")

    tasks.add_task(run)


@router.get("/{document_id}/result")
async def get_result(document_id: str, job: str = "", actor: Actor = Depends(current_actor),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    document = await _visible(document_id, session)
    parse_job = await _archived_job(session, document, job or None)
    markdown = (await storage.get(f"{parse_job.result_prefix}document.md")).decode()
    images = [k.rsplit("/", 1)[-1]
              for k in await storage.list_prefix(f"{parse_job.result_prefix}images/")]
    return {"document_id": document.id, "job_id": parse_job.id, "filename": document.filename,
            "page_count": parse_job.page_count, "markdown": markdown, "images": images}


@router.get("/{document_id}/pages")
async def get_pages(document_id: str, job: str = "", actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """按页分组的块 —— 前端左右栏对齐与 bbox 高亮的数据源。

    优先读库里的 chunks（已索引），没有就现场从 layout.json 算，保证索引没跑完也能看。
    """
    document = await _visible(document_id, session)
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
async def get_layout(document_id: str, job: str = "", actor: Actor = Depends(current_actor),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    document = await _visible(document_id, session)
    parse_job = await _archived_job(session, document, job or None)
    return Response(content=await storage.get(f"{parse_job.result_prefix}layout.json"),
                    media_type="application/json")


@router.get("/{document_id}/source-url")
async def source_url(document_id: str, request: Request,
                     actor: Actor = Depends(current_actor),
                     session: AsyncSession = Depends(get_session)):
    """原件的**稳定** URL。

    凭证住在 control schema（`file_grants`，Go 拥有），所以这里问 control-api 要。
    **不要换成预签名**：URL 一变，模型网关的幂等与向量索引分块键全部失效
    （ADR #11/#12，这个项目踩过两次）。浏览器要的短期 URL 是另一条
    （control-api 的 `/api/documents/{id}/download-url`），两者刻意分开。
    """
    document = await _visible(document_id, session)
    if not document.object_key:
        raise APIError(404, "no active file link for this document", "invalid_request_error",
                       "file_token_missing")
    url = await ControlClient(request.app.state.http).stable_file_url(
        organization_id=document.organization_id, document_id=document.id,
        object_key=document.object_key, mime=document.mime)
    return {"url": url, "path": url[url.find("/files/"):] if "/files/" in url else url,
            "mime": document.mime}


@router.get("/{document_id}/jobs/{job_id}/images/{name}")
async def get_image(document_id: str, job_id: str, name: str,
                    actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """归档后的 markdown 里的图片引用指向这里（受 JWT 保护，不用预签名，不会过期）。"""
    document = await _visible(document_id, session)
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
                   actor: Actor = Depends(current_actor),
                   session: AsyncSession = Depends(get_session),
                   storage: Storage = Depends(get_storage)):
    document = await _visible(document_id, session)
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
async def delete_document(document_id: str, actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session)):
    """软删除：对象由 GC 任务回收。

    计量流水不跟着删——账单不能因为用户删了文档就消失。

    **这是全站唯一一处授权判断**（plan.md §2 已定 2）：语料共享之后
    "看得见"不再需要判谁，但"能不能删"必须判 —— 否则任何人都能删掉
    别人传进来的语料。
    """
    document = (await session.execute(
        select(Document).where(Document.id == document_id,
                               Document.deleted_at.is_(None)).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if document is None:
        raise APIError(404, f"document not found: {document_id}",
                       "invalid_request_error", "document_not_found")
    if not await _may_delete(document, actor, session):
        # 403 而不是 404：文档本来就是全员可见的，装作不存在没有任何意义，
        # 只会让人以为自己找错了 id
        raise APIError(403, "只有上传过这份文档的人或管理员能删除它",
                       "invalid_request_error", "not_uploader")
    deleted_at = utcnow()
    # 文件凭证住在 control schema，本服务无权写 —— 由 DocumentDeleted 事件
    # 通知 control-api 撤销（本函数末尾写 outbox）
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    # 先删消息再删会话：messages 有指向 conversations 的外键，反过来会被 PG 拒掉
    message_ids = select(Message.id).where(Message.conversation_id.in_(
        select(Conversation.id).where(Conversation.document_id == document.id)))
    assertion_ids = select(Assertion.id).where(Assertion.message_id.in_(message_ids))
    await session.execute(delete(Citation).where(
        Citation.source_kind == "assertion", Citation.source_id.in_(assertion_ids)))
    await session.execute(delete(Message).where(Message.conversation_id.in_(
        select(Conversation.id).where(Conversation.document_id == document.id))))
    await session.execute(delete(Conversation).where(Conversation.document_id == document.id))
    generation = await advance_index_generation(
        session, document.id, deleted=False, values={
            "deleted_at": deleted_at, "index_status": "none",
            "index_lease_until": None, "updated_at": deleted_at,
        })
    if generation is None:
        raise APIError(409, "document state changed during deletion; retry",
                       "invalid_request_error", "document_state_changed")
    await session.refresh(document)
    await session.commit()


@router.get("/stats/summary")
async def summary(actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session)):
    total, pages = (await session.execute(
        select(func.count(Document.id), func.coalesce(func.sum(Document.page_count), 0))
        .where(Document.deleted_at.is_(None))
    )).one()
    ready = (await session.execute(
        select(func.count(Document.id)).where(Document.deleted_at.is_(None),
                                              Document.index_status == "ready")
    )).scalar_one()
    return {"documents": total, "pages": pages, "askable": ready}
