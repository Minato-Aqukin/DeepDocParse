"""文档问答（会话式，JWT 鉴权）。

对外是 SSE 流：前端用 fetch + ReadableStream 消费（EventSource 发不出 Authorization 头）。

  event: meta       {"message_id","retrieval":{"chunk_ids":[...]}}
  event: delta      {"text":"…"}                （多帧）
  event: citations  {"citations":[{…}]}
  event: done       {"message_id","verified","degraded","confidence"}
  event: error      {"message","code"}

**落库在生成器里另开 session**：请求作用域的 session 在响应体开始流之前就已经关了，
在流里复用它必炸（M5 在 proxy 上踩过，见 proxy.py 模块 docstring）。
"""
import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session, get_sessionmaker
from app.deps import current_user, get_storage
from app.errors import APIError
from app.metering import record_usage
from app.models import Conversation, Document, Message, ParseJob, User, utcnow
from app.qa import (
    Retrieval, answer_model_meta, attach_crops, attach_resolution, build_messages,
    load_citation_targets, retrieval_confidence, retrieve, verify_parse_consistency,
)
from app.storage import Storage, crop_key as build_crop_key
from app.upstream import chat_request

router = APIRouter()


class ConversationInfo(BaseModel):
    id: str
    document_id: str
    title: str
    created_at: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _citation_out(document_id: str, citation: dict) -> dict:
    """把对象键换成前端能直接取的 URL。"""
    out = dict(citation)
    out.setdefault("resolved", True)     # 流式返回的这一轮必然指向刚检索到的 chunk
    key = out.pop("crop_key", None)
    if key:
        job_id, name = key.split("/")[1], key.rsplit("/", 1)[-1]
        out["crop_url"] = f"/api/documents/{document_id}/crops/{job_id}/{name}"
    else:
        out["crop_url"] = None
    return out


async def _owned_document(document_id: str, user: User, session: AsyncSession) -> Document:
    """取文档。**不判归属**（1b）—— 语料整个部署共享，谁都能对任一文档发起问答。

    名字保留是为了少改调用点；判据只剩"存在且未删"。
    """
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise APIError(404, "document not found", "invalid_request_error", "document_not_found")
    return document


async def _owned_conversation(cid: str, user: User, session: AsyncSession) -> Conversation:
    conversation = await session.get(Conversation, cid)
    if conversation is None or conversation.user_id != user.id:
        raise APIError(404, "conversation not found", "invalid_request_error",
                       "conversation_not_found")
    return conversation


@router.post("/documents/{document_id}/conversations", status_code=201)
async def create_conversation(document_id: str, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    document = await _owned_document(document_id, user, session)
    conversation = Conversation(user_id=user.id, document_id=document.id, title="新会话")
    session.add(conversation)
    await session.commit()
    return {"id": conversation.id, "document_id": document.id, "title": conversation.title,
            "created_at": conversation.created_at}


@router.get("/conversations")
async def list_conversations(document: str = "", user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if document:
        stmt = stmt.where(Conversation.document_id == document)
    rows = (await session.execute(stmt.order_by(Conversation.updated_at.desc()))).scalars().all()
    return [{"id": c.id, "document_id": c.document_id, "title": c.title,
             "created_at": c.created_at, "updated_at": c.updated_at} for c in rows]


@router.get("/conversations/{cid}/messages")
async def list_messages(cid: str, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    conversation = await _owned_conversation(cid, user, session)
    rows = (await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )).scalars().all()
    # 出处要接回**当前**索引：chunk_id 每次 reindex 都重铸，直接回放旧值全是悬空指针。
    # 全会话的定位键合起来查**一次**（每条 message 查一次的话，长会话就是 N+1）
    lookup = await load_citation_targets(
        session, conversation.document_id,
        [c for m in rows for c in (m.citations or [])])
    resolved = {m.id: [attach_resolution(c, lookup) for c in (m.citations or [])]
                for m in rows if m.citations}
    return [{"id": m.id, "role": m.role, "content": m.content,
             "citations": [_citation_out(conversation.document_id, c)
                           for c in resolved.get(m.id, [])],
             "verified": m.verified, "degraded": m.degraded, "model_meta": m.model_meta or {},
             "confidence": retrieval_confidence(resolved.get(m.id, [])),
             "created_at": m.created_at}
            for m in rows]


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: str, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    conversation = await _owned_conversation(cid, user, session)
    await session.execute(delete(Message).where(Message.conversation_id == conversation.id))
    await session.delete(conversation)
    await session.commit()


@router.get("/documents/{document_id}/crops/{job_id}/{name}")
async def get_crop(document_id: str, job_id: str, name: str, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session),
                   storage: Storage = Depends(get_storage)):
    """出处区域截图。名字里已含 bbox 摘要，路径只做归属校验。"""
    from fastapi.responses import Response

    document = await _owned_document(document_id, user, session)
    if "/" in name or ".." in name:
        raise APIError(400, "invalid crop name", "invalid_request_error", "invalid_name")
    # job 也要校验归属：不然路径里的 job_id 会被原样拼进对象键
    job = await session.get(ParseJob, job_id)
    if job is None or job.document_id != document.id:
        raise APIError(404, "parse job not found", "invalid_request_error", "job_not_found")

    page_part, _, digest = name.removesuffix(".png").partition("_")
    if not page_part.isdigit() or not digest:
        raise APIError(400, "invalid crop name", "invalid_request_error", "invalid_name")
    try:
        data = await storage.get(build_crop_key(job.id, int(page_part), digest))
    except Exception:
        raise APIError(404, "crop not found", "invalid_request_error", "crop_not_found")
    return Response(content=data, media_type="image/png")


@router.post("/conversations/{cid}/ask")
async def ask(cid: str, req: AskRequest, request: Request, user: User = Depends(current_user),
              session: AsyncSession = Depends(get_session),
              storage: Storage = Depends(get_storage)):
    conversation = await _owned_conversation(cid, user, session)
    document = await _owned_document(conversation.document_id, user, session)
    await request.app.state.rate_limiter.check(f"qa:{user.id}", settings.qa_rate_per_min)

    if document.index_status != "ready":
        raise APIError(409, {
            "none": "文档还没有建立索引",
            "pending": "索引正在排队，请稍候重试",
            "indexing": "索引正在建立，请稍候重试",
            "failed": f"索引建立失败：{document.index_error or '未知原因'}",
        }.get(document.index_status, "文档尚不可问答"), "invalid_request_error", "index_not_ready")

    job = await session.get(ParseJob, document.current_job_id)
    if job is None or not job.result_prefix:
        raise APIError(409, "文档没有可用的解析结果", "invalid_request_error", "result_not_ready")

    # 历史（最近 N 条）与本轮提问都在请求作用域内落库
    history = list(reversed((await session.execute(
        select(Message).where(Message.conversation_id == cid)
        .order_by(Message.created_at.desc()).limit(settings.qa_history_turns)
    )).scalars().all()))
    session.add(Message(conversation_id=cid, role="user", content=req.question))
    if conversation.title == "新会话":
        conversation.title = req.question[:40]
    conversation.updated_at = utcnow()
    await session.commit()

    http: httpx.AsyncClient = request.app.state.http
    index = request.app.state.search_index

    retrieval = await retrieve(session, index, http, question=req.question,
                               document=document)
    crops = await attach_crops(retrieval, storage, document, job) if retrieval.hits else []
    image_uris = [uri for uri, _ in crops]
    messages = build_messages(req.question, retrieval,
                              [{"role": m.role, "content": m.content} for m in history],
                              image_uris)

    return StreamingResponse(
        _stream_answer(http, messages, retrieval, conversation_id=cid,
                       document_id=document.id, user_id=user.id, has_image=bool(image_uris),
                       # 图与文本必须成对取，不能一个取 image_uris[0] 一个取 hits[0]
                       verify_pair=(crops[0][0], crops[0][1]["text"]) if crops else None),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_answer(http: httpx.AsyncClient, messages: list[dict], retrieval: Retrieval, *,
                         conversation_id: str, document_id: str, user_id: str, has_image: bool,
                         verify_pair: tuple[str, str] | None = None):
    message_id = None
    chunks: list[str] = []
    degraded = retrieval.degraded
    verified = has_image
    error: dict | None = None

    # 出处一致性核对（A4）与回答**并发**跑：核对要多打一次视觉模型，
    # 串行做会把首字延迟顶上去，而它的结论只在最后的 done 帧里才用得上
    verify_task: asyncio.Task | None = None
    if settings.qa_verify_parse and verify_pair:
        verify_task = asyncio.create_task(verify_parse_consistency(http, *verify_pair))

    yield _sse("meta", {"retrieval": {"chunk_ids": [h["chunk_id"] for h in retrieval.hits]}})

    try:
        try:
            async for piece in _relay_chat(http, messages):
                chunks.append(piece)
                yield _sse("delta", {"text": piece})
        except _UpstreamDown:
            # 请求就没建立起来，大概率是视觉运行时没起（dev 常态）——退回纯文本再试一次，
            # 并把降级如实标出来，绝不静默
            if has_image:
                degraded, verified = "vision_unavailable", False
                text_only = _strip_images(messages)
                try:
                    async for piece in _relay_chat(http, text_only):
                        chunks.append(piece)
                        yield _sse("delta", {"text": piece})
                except (_UpstreamDown, httpx.HTTPError) as exc:
                    error = {"message": str(exc), "code": "upstream_unavailable"}
            else:
                error = {"message": "问答服务不可用", "code": "upstream_unavailable"}
        except httpx.HTTPError as exc:
            # 中途断流（上游超时/连接断开）：把已经产出的文本留住并如实报错，
            # 不能让异常冒到 StreamingResponse —— 那会把响应体截断在半路，
            # 客户端只看到"连接莫名其妙断了"，库里还被记成"客户端主动中断"
            error = {"message": f"上游响应中断：{exc!s} ({type(exc).__name__})",
                     "code": "upstream_interrupted"}

        if error:
            degraded, verified = "upstream_error", False
            yield _sse("error", error)
        elif verify_task is not None and await _verdict(verify_task) is False:
            # 图上的字和 chunk 文本对不上 -> 解析很可能错了。此时既不能说"已验证"，
            # 也不能装作没事：这正是七种降级留下的唯一的洞（A4）。
            # 这里会覆盖已有的 degraded，但唯一可能被覆盖的 vision_unavailable
            # 实际不会发生 —— 视觉运行时挂了的话核对也打不通，_verdict 返回 None。
            # 而 embedding_unavailable + 解析出错是真实组合，此时"出处存疑"更该说出口
            degraded, verified = "parse_mismatch", False
    except BaseException:
        # 客户端断开：uvicorn 下 Starlette 会取消流任务，GeneratorExit/CancelledError 到这里。
        # （httpx 的进程内 ASGI 传输不会，流是"正常结束"——所以这个标记由
        #  test_generator_close_marks_client_aborted_and_persists 直接驱动生成器来测。）
        # 无论走哪条路，下面 finally 里 shield 住的落库都保证已产出文本不丢
        degraded = "client_aborted"
        raise
    finally:
        # 客户端断开时核对已经没有意义，别把它留在后台空跑一次视觉推理
        if verify_task is not None and not verify_task.done():
            verify_task.cancel()
        # shield：客户端断开时这个 finally 跑在已取消的作用域里，
        # 不屏蔽的话第一个 await 就被打断，client_aborted 的回答根本落不了库
        message_id = await asyncio.shield(_persist(
            conversation_id=conversation_id, user_id=user_id, content="".join(chunks),
            citations=retrieval.citations, verified=verified and not error, degraded=degraded,
            model_meta=answer_model_meta()))

    yield _sse("citations", {"citations": [_citation_out(document_id, c)
                                           for c in retrieval.citations]})
    yield _sse("done", {"message_id": message_id, "verified": verified and not error,
                        "degraded": degraded,
                        # 出处够不够可信要跟着回答一起给：只给出处不给可信度，
                        # 用户没法判断该不该采信（kotaemon 的做法，A2）
                        "confidence": retrieval_confidence(retrieval.citations)})


async def _verdict(task: asyncio.Task) -> bool | None:
    """等出处核对的结论，但**最多等 qa_verify_timeout**。

    核对与回答并发跑，正常情况下回答先结束、这里几乎不等。但视觉模型在 CPU 上
    抄一段文字可能要几分钟，没有上限的话 done 帧会被硬生生拖后 —— 用户看着答案
    已经出完却迟迟不落定。超时就当"没测出来"：宁可不打标，也不能让核对拖垮体验。
    """
    try:
        return await asyncio.wait_for(asyncio.shield(task), settings.qa_verify_timeout)
    except TimeoutError:
        task.cancel()
        return None
    except asyncio.CancelledError:
        # **不能吞**：这是外层流任务被取消（客户端断开），要让它继续往上传，
        # 否则 `except BaseException` 里的 degraded="client_aborted" 就不会触发，
        # 用户回来只看到一条没有任何说明的半截回答
        task.cancel()
        raise


async def _persist(*, conversation_id: str, user_id: str, content: str, citations: list,
                   verified: bool, degraded: str | None, model_meta: dict | None = None) -> str:
    """把回答落库。

    **必须新开 session**：请求作用域的那个在响应体开始流之前就关了，
    复用它必炸（M5 在 proxy 上踩过，见 proxy.py 模块 docstring）。
    """
    async with get_sessionmaker()() as db:
        message = Message(conversation_id=conversation_id, role="assistant", content=content,
                          citations=citations, verified=verified, degraded=degraded,
                          model_meta=model_meta or {})
        db.add(message)
        await record_usage(db, user_id=user_id, kind="qa", requests=1)
        await db.commit()
        return message.id


class _UpstreamDown(RuntimeError):
    pass


async def _relay_chat(http: httpx.AsyncClient, messages: list[dict]):
    """消费上游的 OpenAI 流式响应，逐段吐出文本增量。"""
    request = chat_request(http, messages, stream=True)
    try:
        response = await http.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _UpstreamDown(f"chat runtime unreachable: {exc}")

    try:
        if response.status_code != 200:
            body = (await response.aread())[:200].decode(errors="replace")
            raise _UpstreamDown(f"chat runtime returned {response.status_code}: {body}")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError):
                continue
            text = delta.get("content")
            if text:
                yield text
    finally:
        await response.aclose()


def _strip_images(messages: list[dict]) -> list[dict]:
    stripped = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [part for part in content if part.get("type") != "image_url"]
        stripped.append({**message, "content": content})
    return stripped
