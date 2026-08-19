"""对外 API 与 MCP 代理（方案 A：所有外部流量经 backend）。

第三方开发者视角的入口：
  POST /v1/parse, GET /v1/parse/{id}[, /result]   （异步任务式，同 service 契约）
  POST /v1/chat/completions, /v1/embeddings        （OpenAI 协议，支持 SSE 流式）
  /mcp                                             （Streamable HTTP 反代 -> mcp-server）

统一流程：验 API key -> 限速 -> 额度 -> 换 SERVICE_TOKEN 转发 -> 记 usage。
未来流量大时演进方案 B：backend 签短期 JWT，service 本地验签（见 ARCHITECTURE.md 决策 #3）。

两个实现要点：
- **计量必须在转发前记**：流式响应的 relay 跑在响应体生成器里，那时请求作用域的
  DB session 已经关了，在里面写库会炸。按次计量在入口记，按页计量在取结果时记（那是 JSON，可缓冲）。
- **逐跳头不能透传**（RFC 9110 §7.6.1），content-length 由流式重新计算。
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
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.config import settings
from app.db import get_session
from app.deps import require_api_key
from app.errors import APIError
from app.metering import check_quota, record_usage
from app.models import ApiKey, Document, ParseJob, utcnow
from app.routers.documents import options_hash

router = APIRouter()

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_RESULT_PATH = re.compile(r"parse/([^/]+)/result")


def _forward_headers(request: Request) -> dict:
    """透传原请求头，但把调用方的 API key 换成内网 service token。"""
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != "authorization"}
    headers["Authorization"] = f"Bearer {settings.service_token}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


async def _relay(request: Request, method: str, url: str, body: bytes) -> Response:
    """流式反代：SSE 与大 JSON 都原样透传，状态码与 content-type 一并保留。"""
    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream_req = http.build_request(method, url, content=body or None,
                                          headers=_forward_headers(request))
        upstream = await http.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error", "upstream_unreachable")

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code,
                             headers=_response_headers(upstream),
                             background=BackgroundTask(upstream.aclose))


def _kind_of(path: str) -> str:
    if path.startswith("chat"):
        return "chat"
    if path.startswith("embeddings"):
        return "embeddings"
    return "parse"


@router.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def api_proxy(path: str, request: Request, key: ApiKey = Depends(require_api_key),
                    session: AsyncSession = Depends(get_session)):
    await request.app.state.rate_limiter.check(key.id, key.rate_limit_per_min)
    check_quota(key)
    key.last_used_at = utcnow()

    body = await request.body()
    url = f"{settings.service_url}/v1/{path}"

    # 入口按次计量（流式响应里没法写库，见模块 docstring）
    await record_usage(session, user_id=key.user_id, api_key_id=key.id, kind=_kind_of(path))
    await session.commit()

    if request.method == "POST" and path == "parse":
        return await _proxy_parse_submit(request, session, key, body, url)
    match = _RESULT_PATH.fullmatch(path)
    if request.method == "GET" and match:
        return await _proxy_parse_result(request, session, key, url, match.group(1))
    return await _relay(request, request.method, url, body)


async def _proxy_parse_submit(request: Request, session: AsyncSession, key: ApiKey,
                              body: bytes, url: str) -> Response:
    """转发解析提交，并在本层建一条任务记录。

    建记录有两个作用：调用方的历史在 Web 端也看得到；页数计量有个锚点（见 _proxy_parse_result）。
    第三方的文件在他们自己那儿，本层不下载、不归档，object_key 留空标识"外部任务"。
    """
    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream = await http.post(url, content=body or None, headers=_forward_headers(request))
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error", "upstream_unreachable")

    if upstream.status_code == 202:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        file_url = str(payload.get("file_url") or "")
        doc_id = str(payload.get("doc_id") or hashlib.sha256(file_url.encode()).hexdigest())
        # 已知不精确：body 是**原样转发**的，调用方没传 engine 时真正决定用哪个引擎的是
        # service 的注册表 default，而这里只能拿本层的配置去猜。两者配歪了，这行记的
        # engine 就与实际用的不一致（只影响本层的展示与 options_hash，不影响解析本身）。
        # 根治要靠契约把「这个任务用了哪个引擎」回给调用方 —— 那是向后兼容的新增，
        # 但 openapi.yaml 目前没有，先记在这里而不是假装它准
        engine = str(payload.get("engine") or settings.default_parse_engine)
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        service_task_id = upstream.json().get("task_id")

        # 只认外部平面的行：Web 上传的 Document 有 object_key 与已归档结果，
        # 复用它会把状态打回 pending -> 对账重新归档 -> 同一批页数被重复计费
        document = (await session.execute(
            select(Document).where(Document.user_id == key.user_id, Document.doc_id == doc_id,
                                   Document.origin == "external")
        )).scalar_one_or_none()
        if document is None:
            document = Document(
                user_id=key.user_id, doc_id=doc_id, origin="external",
                filename=PurePosixPath(urlparse(file_url).path).name or "remote-document",
                object_key="")     # 外部任务：文件在调用方那儿，本层不下载不归档
            session.add(document)
            await session.commit()

        digest = options_hash(engine, options)
        job = (await session.execute(
            select(ParseJob).where(ParseJob.document_id == document.id,
                                   ParseJob.options_hash == digest)
        )).scalar_one_or_none()
        if job is None:
            job = ParseJob(document_id=document.id, engine=engine, options=options,
                           options_hash=digest)
            session.add(job)
        job.api_key_id = key.id
        job.service_task_id = service_task_id
        job.status = "pending"
        job.error = None
        await session.commit()

    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=_response_headers(upstream))


async def _proxy_parse_result(request: Request, session: AsyncSession, key: ApiKey,
                              url: str, service_task_id: str) -> Response:
    """转发取结果，顺手按页计量。

    结果是 JSON（不是流），缓冲它不额外花成本；页数在这里才第一次可见。
    以任务行的 page_count 作为去重锚点：同一任务重复取结果不得重复计费。
    """
    http: httpx.AsyncClient = request.app.state.http
    try:
        upstream = await http.get(url, headers=_forward_headers(request))
    except httpx.HTTPError as exc:
        raise APIError(502, f"upstream unreachable: {exc}", "upstream_error", "upstream_unreachable")

    if upstream.status_code == 200:
        # 同一个 service 任务可能对应本层多个 job（service 按 doc_id 去重，
        # 同一用户既从 Web 传过、又用 key 提交过）—— 优先记在这把 key 自己的 job 上
        # 只认外部平面的 job：Web 上传的 job 复用它会被置成 succeeded，
        # 而 succeeded 不满足 claimable_condition —— 那份文档从此永不归档、永不索引
        rows = (await session.execute(
            select(ParseJob).join(Document, Document.id == ParseJob.document_id)
            .where(ParseJob.service_task_id == service_task_id,
                   Document.user_id == key.user_id,
                   Document.origin == "external").order_by(ParseJob.created_at)
        )).scalars().all()
        job = next((j for j in rows if j.api_key_id == key.id), rows[0] if rows else None)
        if job is not None and job.page_count == 0:
            try:
                layout = upstream.json().get("layout_json") or {}
                pages = len(layout.get("pdf_info") or []) or 1
            except ValueError:
                pages = 1
            job.page_count = pages
            job.status = "succeeded"
            await record_usage(session, user_id=key.user_id, api_key_id=key.id,
                               parse_job_id=job.id, kind="parse", pages=pages, requests=0)
            await session.commit()

    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=_response_headers(upstream))


@router.api_route("/mcp{path:path}", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(path: str, request: Request, key: ApiKey = Depends(require_api_key),
                    session: AsyncSession = Depends(get_session)):
    """MCP Streamable HTTP 反代。

    会话头 mcp-session-id 必须双向透传（请求头由 _forward_headers 带过去，
    响应头由 _response_headers 带回来），否则客户端建不起会话；
    GET 是服务端推流通道（SSE），DELETE 是关会话。
    """
    await request.app.state.rate_limiter.check(key.id, key.rate_limit_per_min)
    check_quota(key)
    key.last_used_at = utcnow()
    await record_usage(session, user_id=key.user_id, api_key_id=key.id, kind="mcp")
    await session.commit()

    body = await request.body()
    return await _relay(request, request.method, f"{settings.mcp_url}/mcp{path}", body)
