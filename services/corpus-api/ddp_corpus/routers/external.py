"""对外解析平面（`/v1/parse*`）在语料侧的那一半。

## 为什么这条路要经过语料 API，而不是直接代到网关

`/v1/*` 的其余端点（chat / embeddings / models / rerank）是**纯算力**，
入口直接代给模型网关就行。但 `/v1/parse` 不是：它会在语料里留下
一份 Document 与一条 ParseJob —— 调用方的历史在 Web 端也看得到，
按页计量也需要一个锚点。而 documents / parse_jobs 是语料的表，
Go 一个字都写不了（企业边界 5）。

所以入口的路由是：

    POST /v1/parse            -> corpus-api（本模块）-> 模型网关
    GET  /v1/parse/{id}       -> corpus-api（本模块）-> 模型网关
    GET  /v1/parse/{id}/result-> corpus-api（本模块）-> 模型网关 + 按页计量
    其余 /v1/*                -> 模型网关（入口直接代）

**对外契约一个字没变**（非目标 §3.2：不改变现有 `/v1/*` 语义）。

## 鉴权与限速不在这里

API key 校验、配额、按次限速全在入口（Go）。本模块只信任 actor 上下文头，
和其它语料端点一样。
"""
import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.ingest import options_hash
from ddp_corpus.models import Document, DocumentUpload, ParseJob, UsageClaim, new_id
from ddp_corpus.usage import record_usage
from ddp_corpus.versions import next_document_version

router = APIRouter()

# 逐跳头不能透传（RFC 9110 §7.6.1）；content-length 由流式重新计算
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_RESULT_PATH = re.compile(r"([^/]+)/result")


def _forward_headers(request: Request) -> dict:
    """透传原请求头，但把调用方的凭据换成内网服务凭据。

    **调用方的 Authorization 到此为止** —— 网关不认识用户 key，
    透传只会让它把一个 sk- 当成 service token 去比对。
    """
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != "authorization"}
    headers["Authorization"] = f"Bearer {settings.service_token}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


async def _relay(request: Request, method: str, url: str, body: bytes) -> Response:
    """流式反代：大 JSON 原样透传，状态码与 content-type 一并保留。"""
    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream_req = http.build_request(method, url, content=body or None,
                                          headers=_forward_headers(request))
        upstream = await http.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error",
                       "upstream_unreachable")

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code,
                             headers=_response_headers(upstream),
                             background=BackgroundTask(upstream.aclose))


@router.post("/v1/parse")
async def submit(request: Request, actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session)) -> Response:
    """转发解析提交，并在语料里建一条任务记录。

    建记录有两个作用：调用方的历史在 Web 端也看得到；按页计量有个锚点。
    第三方的文件在他们自己那儿，本服务**不下载、不归档**，
    `object_key` 留空标识"外部任务"。
    """
    body = await request.body()
    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream = await http.post(f"{settings.service_url}/v1/parse",
                                   content=body or None, headers=_forward_headers(request))
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error",
                       "upstream_unreachable")

    if upstream.status_code == 202:
        await _record_external_job(session, actor, body, upstream)

    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=_response_headers(upstream))


async def _record_external_job(session: AsyncSession, actor: Actor, body: bytes,
                               upstream: httpx.Response) -> None:
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    file_url = str(payload.get("file_url") or "")
    doc_id = str(payload.get("doc_id") or hashlib.sha256(file_url.encode()).hexdigest())
    # 已知不精确：body 是**原样转发**的，调用方没传 engine 时真正决定用哪个引擎的是
    # 网关的注册表 default，而这里只能拿本层的配置去猜。两者配歪了，这行记的
    # engine 就与实际用的不一致（只影响本层的展示与 options_hash，不影响解析本身）。
    # 根治要靠契约把「这个任务用了哪个引擎」回给调用方 —— 那是向后兼容的新增，
    # 但契约目前没有这个能力，先记在这里而不是假装它准
    engine = str(payload.get("engine") or settings.default_parse_engine)
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    service_task_id = upstream.json().get("task_id")

    # 只认外部平面的行：Web 上传的 Document 有 object_key 与已归档结果，
    # 复用它会把状态打回 pending -> 对账重新归档 -> 同一批页数被重复计费
    document = (await session.execute(
        select(Document).where(Document.doc_id == doc_id, Document.origin == "external")
    )).scalar_one_or_none()
    if document is None:
        document = Document(
            id=new_id(), uploaded_by=actor.id, organization_id=actor.organization_id,
            doc_id=doc_id, origin="external",
            filename=PurePosixPath(urlparse(file_url).path).name or "remote-document",
            object_key="")     # 外部任务：文件在调用方那儿，本层不下载不归档
        session.add(document)
        await session.commit()

    # **外部平面也要记归属。** 漏了这一行的后果很别扭：`_may_delete` 只查
    # `document_uploads`，于是**提交者自己删不掉自己提交的文档**（403）。
    if not await session.scalar(
            select(DocumentUpload.id).where(DocumentUpload.document_id == document.id,
                                            DocumentUpload.user_id == actor.id).limit(1)):
        try:
            async with session.begin_nested():
                session.add(DocumentUpload(id=new_id(), document_id=document.id,
                                           user_id=actor.id))
        except IntegrityError:
            pass        # 并发下另一边先记上了，正是想要的结果
        await session.commit()

    digest = options_hash(engine, options)
    job = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document.id,
                               ParseJob.options_hash == digest)
    )).scalar_one_or_none()
    if job is None:
        job = ParseJob(document_id=document.id, engine=engine, options=options,
                       initiated_by=actor.id, options_hash=digest,
                       document_version=await next_document_version(session, document.id))
        session.add(job)
    job.api_key_id = actor.api_key_id
    job.service_task_id = service_task_id
    job.status = "pending"
    job.error = None
    await session.commit()


@router.get("/v1/parse/{path:path}")
async def status_or_result(path: str, request: Request,
                           actor: Actor = Depends(current_actor),
                           session: AsyncSession = Depends(get_session)) -> Response:
    """状态查询原样透传；取结果时顺手按页计量。

    结果是 JSON（不是流），缓冲它不额外花成本；页数在这里才第一次可见。
    """
    url = f"{settings.service_url}/v1/parse/{path}"
    match = _RESULT_PATH.fullmatch(path)
    if match is None:
        return await _relay(request, "GET", url, b"")

    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream = await http.get(url, headers=_forward_headers(request))
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error",
                       "upstream_unreachable")

    if upstream.status_code == 200:
        await _meter_result(session, actor, match.group(1), upstream)

    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=_response_headers(upstream))


async def _meter_result(session: AsyncSession, actor: Actor, service_task_id: str,
                        upstream: httpx.Response) -> None:
    # 同一个网关任务可能对应本层多个 job（网关按 doc_id 去重，
    # 同一份文档既从 Web 传过、又用 key 提交过）。
    # 只认外部平面的 job：Web 上传的 job 复用它会被置成 succeeded，
    # 而 succeeded 不满足 claimable_condition —— 那份文档从此永不归档、永不索引
    rows = (await session.execute(
        select(ParseJob).join(Document, Document.id == ParseJob.document_id)
        .where(ParseJob.service_task_id == service_task_id,
               Document.origin == "external").order_by(ParseJob.created_at)
    )).scalars().all()

    # **只认这把 key 自己的 job，没有就不记账。**
    # 语料共享之后 documents 不再按用户分行，于是同一个网关任务会对上**别人**的
    # job —— 简单的 `rows[0]` 兜底会把 A 的解析算到 B 头上，而 A 从此永不被计费
    # （实测：A used_pages=0、B used_pages=3）。
    job = (next((j for j in rows if j.api_key_id and j.api_key_id == actor.api_key_id), None)
           or next((j for j in rows if j.initiated_by == actor.id), None)
           # **第三层兜底：他提交过吗？** 前两层只认得"最后一个提交者"
           # （`api_key_id` 每次提交都被覆盖）和"第一个发起人"
           # （`initiated_by` 只在建 job 时写）—— 三个人以上共享同一份外部文档时，
           # **中间那些人两层都对不上，一分钱不记且绕过配额**。
           # `document_uploads` 记的是"谁提交过"，正好补上这个洞；
           # 而没提交过的人不在那张表里，所以这一层不会重新打开
           # "拿别人的 task_id 白嫖结果"那个口子
           or await _submitted_it(session, actor.id, rows))
    if job is None:
        return

    # 按 (actor, job) 判重 —— 理由见 models.UsageClaim 的 docstring
    try:
        async with session.begin_nested():
            session.add(UsageClaim(id=new_id(), actor_id=actor.id, parse_job_id=job.id))
    except IntegrityError:
        return      # 这个人已经为这个 job 付过费了

    try:
        layout = upstream.json().get("layout_json") or {}
        pages = len(layout.get("pdf_info") or []) or 1
    except ValueError:
        pages = 1
    job.page_count = job.page_count or pages
    job.status = "succeeded"
    await record_usage(session, actor_id=actor.id, organization_id=actor.organization_id,
                       api_key_id=actor.api_key_id, parse_job_id=job.id,
                       kind="parse", pages=pages, requests=0)
    await session.commit()


async def _submitted_it(session: AsyncSession, actor_id: str,
                        rows: list[ParseJob]) -> ParseJob | None:
    """这些 job 里，有没有一个的文档是这个人提交过的。

    见调用点的注释：`api_key_id` 只留得住最后一个提交者、`initiated_by` 只留得住
    第一个，中间的人要靠 `document_uploads` 才认得出来。
    """
    if not rows:
        return None
    doc_ids = {j.document_id for j in rows}
    mine = set((await session.execute(
        select(DocumentUpload.document_id).where(
            DocumentUpload.user_id == actor_id,
            DocumentUpload.document_id.in_(doc_ids)))).scalars().all())
    return next((j for j in rows if j.document_id in mine), None)
