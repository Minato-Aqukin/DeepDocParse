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
from dataclasses import replace

import httpx

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ddp_corpus.archive import fail_job, image_base_url
from ddp_core.chunking import layout_to_chunks
from ddp_corpus.config import settings
from ddp_corpus.control_client import ControlClient
from ddp_corpus.directory import display_names
from ddp_corpus.document_context import (
    DocumentContext, document_context, presentation, scoped_jobs,
)
from ddp_corpus.ingest import options_hash, submit_parse
from ddp_corpus.indexing import mark_index_pending
from ddp_corpus.queue import enqueue
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor, get_service_client, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.policy import document_resource_id, require_document, require_resource, resource_condition, visible_document_condition
from ddp_corpus.resources import tombstone_resource
from ddp_corpus.models import (
    Assertion, Chunk, Citation, Conversation, Document, DocumentUpload, Evidence, Message,
    ParseJob, Resource, ResourceVersion,
    as_aware, new_id, utcnow,
)
from ddp_corpus.service_client import ServiceClient, ServiceError
from ddp_corpus.storage import Storage, prefix_of, source_key
from ddp_corpus.versions import next_document_version
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
    resource_id: str | None = None
    source_version_id: str | None = None
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
              uploaders: list[str] | None = None, can_delete: bool = False,
              context: DocumentContext | None = None) -> DocumentInfo:
    context = context or presentation(document, None)
    index_state = job if context.resource_id else document
    return DocumentInfo(
        id=document.id, resource_id=context.resource_id, source_version_id=context.version_id,
        filename=context.filename, doc_id=document.doc_id,
        origin=document.origin, mime=document.mime, size_bytes=document.size_bytes,
        page_count=job.page_count if job else 0,
        status=job.status if job else "pending", error=job.error if job else None,
        index_status=getattr(index_state, "index_status", "none"),
        index_error=getattr(index_state, "index_error", None),
        compile_status=getattr(index_state, "compile_status", "pending"),
        compile_degraded=getattr(index_state, "compile_degraded", None) or [],
        compile_fingerprint=getattr(index_state, "compile_fingerprint", ""),
        layout_version=getattr(index_state, "layout_version", ""),
        code_detection=getattr(index_state, "code_detection", None) or "unavailable",
        current_job_id=context.parse_job_id, created_at=context.created_at,
        uploaders=uploaders or [], can_delete=can_delete,
    )


async def _uploaders_of(session: AsyncSession, document_ids: list[str],
                        http: httpx.AsyncClient | None = None, *, actor: Actor) -> dict[str, list[str]]:
    """document_id -> 上传者显示名列表。**一次查完，别在循环里查**。

    用户住在 control schema（Go 拥有），所以名字要问 control-api ——
    理由与代价见 `ddp_corpus/directory.py`。拿不到名字时退回占位名，
    不会让整个列表失败。
    """
    if not document_ids:
        return {}
    rows = (await session.execute(
        select(ResourceVersion.document_id, Resource.uploaded_by).join(Resource).where(
            ResourceVersion.document_id.in_(document_ids), ResourceVersion.deleted_at.is_(None),
            resource_condition(actor)).distinct()
    )).all()
    names = {}
    if http is not None:
        names = await display_names(http, [actor_id for _, actor_id in rows])
    out: dict[str, list[str]] = {}
    for doc_id, actor_id in rows:
        out.setdefault(doc_id, []).append(names.get(actor_id) or f"用户 {actor_id[:8]}")
    return out



async def _visible(document_id: str, session: AsyncSession, actor: Actor) -> Document:
    return await require_document(session, actor, document_id)


async def _owned_asset_document(session: AsyncSession, actor: Actor, document_id: str) -> Document:
    document = await require_document(session, actor, document_id)
    rid = await document_resource_id(session, actor, document_id)
    if rid is None:
        raise APIError(409, "resource context required", "invalid_request_error", "resource_context_required")
    await require_resource(session, actor, rid, write=True)
    # Same lock order as upload/GC and per-job index mutations.
    return await session.scalar(select(Document).where(Document.id == document.id)
        .with_for_update().execution_options(populate_existing=True))


async def _may_delete(document: Document, actor: Actor, session: AsyncSession) -> bool:
    return bool(await session.scalar(select(Resource.id).join(ResourceVersion).where(
        ResourceVersion.document_id == document.id, ResourceVersion.deleted_at.is_(None),
        resource_condition(actor, write=True)))) or (
        actor.principal_id is not None and document.uploaded_by == actor.principal_id
        and document.organization_id == actor.organization_id)


async def _latest_job(session: AsyncSession, document: Document,
                      actor: Actor | None = None) -> ParseJob | None:
    # Internal callers without an actor use the content cache. HTTP callers always
    # select the asset's frozen parse or its own pending attempt.
    context = (await document_context(session, actor, document) if actor
               else presentation(document, None))
    if context.parse_job_id:
        job = await session.scalar(select(ParseJob).where(
            ParseJob.id == context.parse_job_id, scoped_jobs(document.id, context)))
        if job is not None:
            return job
    return await session.scalar(select(ParseJob).where(scoped_jobs(document.id, context))
        .order_by(ParseJob.created_at.desc(), ParseJob.id.desc()).limit(1))


async def _job_or_current(session: AsyncSession, document: Document,
                          job_id: str | None, actor: Actor) -> ParseJob:
    context = await document_context(session, actor, document)
    job = (await session.scalar(select(ParseJob).where(
        ParseJob.id == job_id, scoped_jobs(document.id, context))) if job_id
        else await _latest_job(session, document, actor))
    if job is None or (getattr(actor, "version_id", None) and job.id != context.parse_job_id):
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")
    return job


async def _archived_job(session: AsyncSession, document: Document, job_id: str | None,
                         actor: Actor) -> ParseJob:
    job = await _job_or_current(session, document, job_id, actor)
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
    # Choose a readable asset before filtering/paging. Metadata must never come
    # from the first uploader of a deduplicated Document.
    selected_version = (select(ResourceVersion.id).join(Resource).where(
        ResourceVersion.document_id == Document.id,
        ResourceVersion.deleted_at.is_(None), resource_condition(actor))
        .order_by((Resource.owner_id == actor.principal_id).desc(),
                  ResourceVersion.created_at.desc(), ResourceVersion.id.desc())
        .limit(1).correlate(Document).scalar_subquery())
    selected_job = (select(ParseJob.id).where(ParseJob.document_id == Document.id,
        or_(ParseJob.resource_id == ResourceVersion.resource_id,
            ResourceVersion.id.is_(None)))
        .order_by(ParseJob.created_at.desc(), ParseJob.id.desc())
        .limit(1).correlate(Document, ResourceVersion).scalar_subquery())
    current = aliased(ParseJob)
    fallback = aliased(ParseJob)
    job_status = func.coalesce(current.status, fallback.status)
    stmt = (select(Document, ResourceVersion, Resource, current, fallback).select_from(Document)
        .outerjoin(ResourceVersion, ResourceVersion.id == selected_version)
        .outerjoin(Resource, Resource.id == ResourceVersion.resource_id)
        .outerjoin(current, current.id == func.coalesce(
            ResourceVersion.parse_job_id,
            # A pending mapped asset must not inherit the content cache's job.
            case((ResourceVersion.id.is_(None), Document.current_job_id))))
        .outerjoin(fallback, fallback.id == selected_job)
        .where(visible_document_condition(actor)))
    if q:
        stmt = stmt.where(func.coalesce(ResourceVersion.filename, Document.filename).ilike(f"%{q}%"))
    if status:
        stmt = (stmt.where(job_status == status) if status != "pending"
                else stmt.where(or_(job_status == "pending", job_status.is_(None))))
    stmt = stmt.order_by(func.coalesce(ResourceVersion.created_at, Document.created_at).desc(),
                         Document.id).limit(max(1, min(limit, 200))).offset(max(0, offset))
    rows = (await session.execute(stmt)).all()
    uploader_ids = [r.uploaded_by if r else d.uploaded_by for d, _, r, _, _ in rows]
    names = await display_names(request.app.state.http, uploader_ids) if uploader_ids else {}
    return [_doc_info(d, current_job or fallback_job,
            context=presentation(d, version),
            uploaders=[names.get(r.uploaded_by if r else d.uploaded_by)
                       or f"用户 {(r.uploaded_by if r else d.uploaded_by)[:8]}"],
            can_delete=(actor.principal_id == (r.owner_id if r else d.uploaded_by)
                        and actor.organization_id == (r.organization_id if r else d.organization_id)))
        for d, version, r, current_job, fallback_job in rows]


@router.get("/{document_id}", response_model=DocumentInfo)
async def get_document(document_id: str, request: Request,
                       actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session),
                       service: ServiceClient = Depends(get_service_client)):
    """非终态时实时问一次 service，避免"库里还 pending 但 service 早跑完了"。"""
    document = await _visible(document_id, session, actor)
    job = await _latest_job(session, document, actor)
    if job is not None and job.status not in TERMINAL and job.service_task_id:
        try:
            live = await service.get_status(job.service_task_id)
        except Exception:
            return _doc_info(document, job, context=await document_context(session, actor, document))     # service 抖动：返回库里的状态，对账兜底
        if live.get("status") == "failed":
            await fail_job(session, job, live.get("error") or "parse failed")
        elif live.get("status") == "running" and job.status == "pending":
            job.status = "running"
            await session.commit()
        # succeeded 不在这里改：必须等归档完成才对外称 succeeded（结果要能立刻取）
    return _doc_info(document, job, context=await document_context(session, actor, document),
                     uploaders=(await _uploaders_of(session, [document.id], request.app.state.http, actor=actor)).get(document.id, []),
                     can_delete=await _may_delete(document, actor, session))


@router.get("/{document_id}/jobs", response_model=list[JobInfo])
async def list_jobs(document_id: str, actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    document = await _visible(document_id, session, actor)
    context = await document_context(session, actor, document)
    jobs = (await session.execute(
        select(ParseJob).where(scoped_jobs(document.id, context))
        .order_by(ParseJob.created_at.desc())
    )).scalars().all()
    return [JobInfo(id=j.id, engine=j.engine, options=j.options, status=j.status, error=j.error,
                    page_count=j.page_count, is_current=(j.id == context.parse_job_id),
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
    document = await _owned_asset_document(session, actor, document_id)
    # 按 origin 判，不要按 object_key 是否为空判：空 object_key 有两个含义
    # （外部提交 / 原件已被 GC 回收），混在一起会把"原件没了"报成"这是外部文档"，
    # 用户完全无从判断该怎么办
    if document.origin == "external":
        raise APIError(400, "external documents cannot be re-parsed here",
                       "invalid_request_error", "external_document")
    if not document.object_key:
        raise APIError(409, "original file is no longer available, please re-upload it",
                       "invalid_request_error", "source_missing")

    resource_id = await document_resource_id(session, actor, document.id)
    engine = req.engine or settings.default_parse_engine
    digest = options_hash(engine, req.options)
    job = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id,
                               ParseJob.resource_id == resource_id, ParseJob.options_hash == digest)
    )).scalar_one_or_none()
    if job is not None and job.status != "failed":
        return JobInfo(id=job.id, engine=job.engine, options=job.options, status=job.status,
                       error=job.error, page_count=job.page_count,
                       is_current=(job.id == document.current_job_id),
                       created_at=job.created_at, archived_at=job.archived_at,
                       document_version=job.document_version)

    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=req.options,
                       initiated_by=actor.principal_id,
                       resource_id=await document_resource_id(session, actor, document.id),
                       options_hash=digest,
                       document_version=await next_document_version(session, document.id))
        session.add(job)
    else:
        job.status, job.error = "pending", None
        job.initiated_by = actor.principal_id
        job.resource_id = await document_resource_id(session, actor, document.id)
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
async def set_current_job(document_id: str, req: CurrentJobRequest,
                          request: Request,
                          actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session),
                          storage: Storage = Depends(get_storage)):
    """切换生效的解析版本。索引跟着换版本重建——否则问答会引用到旧版本的块。"""
    document = await _owned_asset_document(session, actor, document_id)
    job = await _job_or_current(session, document, req.job_id, replace(actor, version_id=None))
    if job.status != "succeeded":
        raise APIError(409, "only a succeeded job can be made current",
                       "invalid_request_error", "job_not_ready")
    rid = await document_resource_id(session, actor, document.id)
    resource = await session.scalar(select(Resource).where(Resource.id == rid)
        .with_for_update().execution_options(populate_existing=True))
    previous = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == rid, ResourceVersion.document_id == document.id,
        ResourceVersion.deleted_at.is_(None)).order_by(ResourceVersion.version_no.desc()).limit(1))
    if actor.version_id and previous and previous.id != actor.version_id:
        raise APIError(409, "resource version changed; reload before selecting a parse",
                       "invalid_request_error", "resource_version_changed")
    # No old Chunk is replaced when selecting another fixed parse. Historical
    # evidence remains resolvable; a selection alone cannot invalidate citations.
    if previous and previous.parse_job_id != job.id:
        next_no = (await session.scalar(select(func.max(ResourceVersion.version_no)).where(
            ResourceVersion.resource_id == rid)) or 0) + 1
        session.add(ResourceVersion(resource_id=rid, version_no=next_no,
            document_id=document.id, source_digest=previous.source_digest,
            filename=previous.filename, size_bytes=previous.size_bytes, parse_job_id=job.id))
        resource.updated_at = utcnow()
    # Keep the old document pointer only as the cache for its existing asset.
    mirrored = await session.get(ParseJob, document.current_job_id) if document.current_job_id else None
    if mirrored is None or mirrored.resource_id == rid:
        document.current_job_id = job.id
        document.page_count = job.page_count
    if job.index_status not in ("ready", "pending", "indexing"):
        if job.resource_id != rid:
            raise APIError(409, "copy needs its own parse before rebuilding",
                           "invalid_request_error", "shared_parse_write_unsupported")
        await mark_index_pending(session, job.id)
    if job.index_status == "pending":
        await _schedule_index(session, document.id, actor.organization_id, job_id=job.id)
    await session.commit()
    return _doc_info(document, job, context=await document_context(
        session, replace(actor, version_id=None), document))


@router.post("/{document_id}/reindex", response_model=DocumentInfo, status_code=202)
async def reindex(document_id: str, request: Request,
                  acknowledge_invalidations: bool = False,
                  actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session),
                  storage: Storage = Depends(get_storage)):
    document = await _owned_asset_document(session, actor, document_id)
    job = await _latest_job(session, document, actor)
    if job is None or job.status != "succeeded":
        raise APIError(409, "document has no archived result to index",
                       "invalid_request_error", "result_not_ready")
    rid = await document_resource_id(session, actor, document.id)
    if job.resource_id != rid:
        raise APIError(409, "copy needs its own parse before rebuilding",
                       "invalid_request_error", "shared_parse_write_unsupported")
    if job.index_status == "pending" or _index_lease_active(job):
        raise APIError(409, "resource version index build is already in progress",
                       "invalid_request_error", "index_in_progress")
    validation = await _validate_index(document, job, session, storage)
    if not validation.safe_to_reindex and not acknowledge_invalidations:
        raise APIError(409,
            f"重建会使 {validation.citation_invalidations} 条历史出处显式失效；请先查看校验结果并确认",
            "invalid_request_error", "index_version_unsafe")
    # The index worker also serializes final publication through the Document lock.
    # Re-read the job after validation: only its own lease matters.
    await session.refresh(job)
    if job.index_status == "pending" or _index_lease_active(job):
        raise APIError(409, "resource version index build is already in progress",
                       "invalid_request_error", "index_in_progress")
    generation = await mark_index_pending(session, job.id)
    if generation is None:
        raise APIError(409, "resource version became unavailable", "invalid_request_error", "index_version_changed")
    await _schedule_index(session, document.id, actor.organization_id, job_id=job.id)
    await session.commit()
    return _doc_info(document, job, context=await document_context(session, actor, document))


@router.post("/{document_id}/validate-index", response_model=IndexValidation)
async def validate_index(document_id: str, job_id: str = "",
                         actor: Actor = Depends(current_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage)):
    """只读校验 provider 与老出处回接；绝不在背后触发重建。"""
    document = await _visible(document_id, session, actor)
    job = await _job_or_current(session, document, job_id or None, actor)
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


async def _schedule_index(session: AsyncSession, document_id: str,
                          organization_id: str = "", *, job_id: str) -> None:
    """排一次索引任务。

    合仓前这里是 `BackgroundTasks.add_task` —— 也就是**API 进程的内存**。
    滚动发布把进程换掉的那一刻，在途索引静默消失（企业边界 7），
    文档一直停在 indexing，而对账只捞 pending，自愈不了。

    现在它落进 `corpus.tasks`，由 `services/corpus-worker` 领。
    `dedupe_key` 让"连点三次重建索引"只排一次队。
    **不 commit** —— 与业务写入同一个事务提交。
    """
    await enqueue(session, kind="index", payload={"document_id": document_id, "job_id": job_id},
                  organization_id=organization_id, dedupe_key=f"index:{job_id}")


@router.get("/{document_id}/result")
async def get_result(document_id: str, job: str = "", actor: Actor = Depends(current_actor),
                     session: AsyncSession = Depends(get_session),
                     storage: Storage = Depends(get_storage)):
    document = await _visible(document_id, session, actor)
    parse_job = await _archived_job(session, document, job or None, actor)
    markdown = (await storage.get(f"{parse_job.result_prefix}document.md")).decode()
    images = [k.rsplit("/", 1)[-1]
              for k in await storage.list_prefix(f"{parse_job.result_prefix}images/")]
    return {"document_id": document.id, "job_id": parse_job.id, "filename": (await document_context(session, actor, document)).filename,
            "page_count": parse_job.page_count, "markdown": markdown, "images": images}


@router.get("/{document_id}/pages")
async def get_pages(document_id: str, job: str = "", actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """按页分组的块 —— 前端左右栏对齐与 bbox 高亮的数据源。

    优先读库里的 chunks（已索引），没有就现场从 layout.json 算，保证索引没跑完也能看。
    """
    document = await _visible(document_id, session, actor)
    parse_job = await _archived_job(session, document, job or None, actor)

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
    document = await _visible(document_id, session, actor)
    parse_job = await _archived_job(session, document, job or None, actor)
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
    document = await _visible(document_id, session, actor)
    if not document.object_key:
        raise APIError(404, "no active file link for this document", "invalid_request_error",
                       "file_token_missing")
    context = await document_context(session, actor, document)
    url = await ControlClient(request.app.state.http).stable_file_url(
        organization_id=actor.organization_id, document_id=document.id,
        object_key=document.object_key, mime=document.mime,
        filename=context.filename, subject_id=actor.principal_id, resource_id=context.resource_id)
    return {"url": url, "path": url[url.find("/files/"):] if "/files/" in url else url,
            "mime": document.mime}


@router.get("/{document_id}/jobs/{job_id}/images/{name}")
async def get_image(document_id: str, job_id: str, name: str,
                    actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session),
                    storage: Storage = Depends(get_storage)):
    """归档后的 markdown 里的图片引用指向这里（受 JWT 保护，不用预签名，不会过期）。"""
    document = await _visible(document_id, session, actor)
    if "/" in name or ".." in name:
        raise APIError(400, "invalid image name", "invalid_request_error", "invalid_name")
    job = await _job_or_current(session, document, job_id, actor)
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
    document = await _visible(document_id, session, actor)
    stem = (await document_context(session, actor, document)).filename.rsplit(".", 1)[0]

    if format == "source":
        # **不变式 6**：原件不整份进应用进程内存，也不由应用进程中转下载流量。
        # 这里只做鉴权与存在性判断（exists 是一次 HEAD，不取字节），
        # 然后 302 到一条短期直读 URL —— 字节从对象存储直接到客户端。
        #
        # 与 `/source-url` 的**稳定** URL 刻意分开：那条的路径必须永远不变
        # （模型网关的 doc_hash 幂等靠它，ADR #11/#12），这条每次都换。
        # 前端不要用 XHR 跟这个跳转（Authorization 头会跟到对象存储去），
        # 走 `/api/documents/{id}/download-url` 拿地址再直接导航。
        exists = await storage.exists(document.object_key) if document.object_key else False
        if not exists:
            # 原件可能已被 GC 回收（软删除后重新可见的窗口、或对象存储侧被清理）
            raise APIError(404, "original file is no longer available",
                           "invalid_request_error", "source_missing")
        url = await storage.presigned_get(
            document.object_key, expires_seconds=settings.source_url_ttl_seconds,
            filename=(await document_context(session, actor, document)).filename, content_type=document.mime)
        return RedirectResponse(url, status_code=302)

    parse_job = await _archived_job(session, document, job or None, actor)
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
    document = await _visible(document_id, session, actor)
    stmt = select(Resource).join(ResourceVersion).where(
        ResourceVersion.document_id == document.id, ResourceVersion.deleted_at.is_(None),
        resource_condition(actor, write=True))
    if actor.resource_id:
        stmt = stmt.where(Resource.id == actor.resource_id)
    resources = list((await session.execute(stmt.distinct())).scalars())
    if len(resources) > 1:
        raise APIError(409, "resource_id is required", "invalid_request_error", "resource_context_required")
    if resources:
        await tombstone_resource(session, resources[0])
    else:
        # Legacy rows without mappings retain their owner-only deletion path.
        mapped = await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.document_id == document.id).limit(1))
        if mapped or not await _may_delete(document, actor, session):
            raise APIError(404, "document not found", "invalid_request_error", "document_not_found")
        document.deleted_at = utcnow()
    await session.commit()


@router.get("/stats/summary")
async def summary(actor: Actor = Depends(current_actor),
                  session: AsyncSession = Depends(get_session)):
    total, pages = (await session.execute(
        select(func.count(Document.id), func.coalesce(func.sum(Document.page_count), 0))
        .where(visible_document_condition(actor))
    )).one()
    ready = (await session.execute(
        select(func.count(Document.id)).where(visible_document_condition(actor),
                                              Document.index_status == "ready")
    )).scalar_one()
    return {"documents": total, "pages": pages, "askable": ready}
