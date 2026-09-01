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
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.config import settings
from ddp_corpus.db import get_session, get_sessionmaker
from ddp_corpus.deps import current_user, get_storage
from ddp_corpus.errors import APIError
from ddp_corpus.evidence import citation_out, load_citations, record_evidence
from ddp_corpus.metering import record_usage
from ddp_corpus.models import (
    AgentTurn, Assertion, Chunk, Citation, Conversation, Document, Evidence,
    EvidenceVerification, Message, ParseJob, RetrievalCandidate, User, utcnow,
)
from ddp_corpus.qa import (
    Retrieval, answer_model_meta, attach_crops, build_messages, decide_retrieval,
    inherited_retrieval, retrieval_confidence, retrieve, trim_hits_to_context,
    verify_parse_consistency,
)
from ddp_corpus.storage import Storage, crop_key as build_crop_key
from ddp_corpus.upstream import chat_request
from ddp_core.agent import CandidateDecision, QueryDecision, assertions_from_text

router = APIRouter()


class ConversationInfo(BaseModel):
    id: str
    document_id: str
    title: str
    created_at: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


REFUSAL_NO_EVIDENCE = "本轮没有可继承的文档证据，无法在不检索的情况下回答。"
REFUSAL_INCOMPLETE_EVIDENCE = "上一轮证据已部分失效，无法在不重新检索的情况下可靠回答。"


class HumanVerificationRequest(BaseModel):
    verdict: Literal["pass", "reject", "question"]
    reason_code: str | None = Field(default=None, max_length=64)
    reason_text: str | None = Field(default=None, max_length=1000)


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


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


async def _assertion_payloads(session: AsyncSession, *, document_id: str,
                              messages: list[Message]) -> dict[str, list[dict]]:
    """批量加载 Assertion + 各自 Citation；旧 message 在读时也显式 unsupported。"""
    message_ids = [message.id for message in messages if message.role == "assistant"]
    if not message_ids:
        return {}
    assertions = (await session.execute(
        select(Assertion).where(Assertion.message_id.in_(message_ids))
        .order_by(Assertion.message_id, Assertion.position)
    )).scalars().all()
    assertion_ids = [row.id for row in assertions]
    resolved = await load_citations(
        session, source_kind="assertion", source_ids=assertion_ids)
    grouped: dict[str, list[dict]] = {}
    for row in assertions:
        citations = [citation_out(document_id, value)
                     for value in resolved.get(row.id, [])]
        live_citations = [item for item in citations if item.get("resolved") is True]
        fully_supported = (
            not row.unsupported and bool(citations)
            and len(live_citations) == len(citations)
        )
        grouped.setdefault(row.message_id, []).append({
            "id": row.id, "position": row.position, "text": row.text,
            # 失效 Citation 仍保留在 citations 供审计，但不能继续冒充当前支持证据。
            "evidence_ids": [item["evidence_id"] for item in live_citations],
            "verification": {
                "state": row.verification_state if fully_supported else "unverified",
                "mode": row.verification_mode if fully_supported else None},
            # 数据库列是最后一道类型闸门；API 再按 evidence 是否为空收紧，
            # 防止手工改库或索引漂移造出 unsupported=false 的无效出处断言。
            "unsupported": not fully_supported,
            "citations": citations,
        })
    for message in messages:
        if message.role == "assistant" and message.id not in grouped:
            grouped[message.id] = [{
                "id": None, "position": 0, "text": message.content,
                "evidence_ids": [],
                "verification": {"state": "unverified", "mode": None},
                "unsupported": True, "citations": [],
            }]
    return grouped


async def _turn_payloads(session: AsyncSession,
                         message_ids: list[str]) -> dict[str, dict]:
    if not message_ids:
        return {}
    turns = (await session.execute(
        select(AgentTurn).where(AgentTurn.message_id.in_(message_ids))
    )).scalars().all()
    candidates = (await session.execute(
        select(RetrievalCandidate).where(
            RetrievalCandidate.turn_id.in_([turn.id for turn in turns]))
        .order_by(RetrievalCandidate.rank)
    )).scalars().all() if turns else []
    by_turn: dict[str, list[dict]] = {}
    for row in candidates:
        by_turn.setdefault(row.turn_id, []).append({
            "evidence_id": row.evidence_id, "document_id": row.document_id,
            "rank": row.rank, "score": row.score, "similarity": row.similarity,
            "accepted": row.accepted, "reason": row.reason,
        })
    return {turn.message_id: {
        "query_decision": {
            "need_retrieval": turn.need_retrieval, "reason": turn.decision_reason,
            "inherited_evidence_ids": turn.inherited_evidence_ids,
            "degraded": turn.degraded,
        },
        "retrieval": {"candidates": by_turn.get(turn.id, [])},
    } for turn in turns}


async def _latest_evidence_ids(session: AsyncSession,
                               history: list[Message]) -> list[str]:
    """最近一条 assistant Assertion 的有序证据集；只继承上一轮，不跨轮拼接。"""
    previous = next((message for message in reversed(history)
                     if message.role == "assistant"), None)
    if previous is None:
        return []
    assertions = (await session.execute(
        select(Assertion.id).where(Assertion.message_id == previous.id)
        .order_by(Assertion.position)
    )).scalars().all()
    if assertions:
        rows = (await session.execute(
            select(Citation.evidence_id).join(
                Assertion,
                and_(Citation.source_kind == "assertion",
                     Citation.source_id == Assertion.id),
            ).where(
                Assertion.message_id == previous.id,
                Citation.role == "primary",
            ).order_by(Assertion.position, Citation.rank, Citation.id)
        )).scalars().all()
    else:  # 0011 迁移前或手工导入的兼容数据
        rows = (await session.execute(
            select(Citation.evidence_id).where(
                Citation.source_kind == "message", Citation.source_id == previous.id,
                Citation.role == "primary").order_by(Citation.rank, Citation.id)
        )).scalars().all()
    return list(dict.fromkeys(rows))


def _ordered_citation_union(assertions: list[dict]) -> list[dict]:
    """兼容旧客户端的 message.citations：按断言/排名排序并按 evidence 去重。"""
    result: list[dict] = []
    seen: set[str] = set()
    for assertion in assertions:
        for citation in assertion["citations"]:
            evidence_id = citation["evidence_id"]
            if evidence_id not in seen:
                seen.add(evidence_id)
                result.append(citation)
    return result


@router.get("/conversations/{cid}/messages")
async def list_messages(cid: str, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    conversation = await _owned_conversation(cid, user, session)
    rows = (await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )).scalars().all()
    assertion_map = await _assertion_payloads(
        session, document_id=conversation.document_id, messages=rows)
    turn_map = await _turn_payloads(session, [m.id for m in rows])
    result = []
    for message in rows:
        assertions = assertion_map.get(message.id, [])
        # 兼容旧客户端：message.citations 是断言 citations 的有序并集；
        # 新客户端以 assertions 为语义真相。
        citations = _ordered_citation_union(assertions)
        effectively_verified = bool(assertions) and all(
            not assertion["unsupported"]
            and assertion["verification"]["state"] == "passed"
            for assertion in assertions)
        result.append({
            "id": message.id, "role": message.role, "content": message.content,
            "citations": citations, "assertions": assertions,
            # 兼容字段也必须服从当前可解析的断言真相，不能让失效出处旁边继续
            # 显示绿色“已验证”。断言级 verification 才是语义真相。
            "verified": effectively_verified, "degraded": message.degraded,
            "model_meta": message.model_meta or {},
            "confidence": retrieval_confidence(citations),
            **turn_map.get(message.id, {}), "created_at": message.created_at,
        })
    return result


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: str, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    conversation = await _owned_conversation(cid, user, session)
    message_ids = select(Message.id).where(Message.conversation_id == conversation.id)
    assertion_ids = select(Assertion.id).where(Assertion.message_id.in_(message_ids))
    turn_ids = select(AgentTurn.id).where(AgentTurn.message_id.in_(message_ids))
    # EvidenceVerification 是语料级审计记录，不属于会话生命周期。Assertion 的 FK
    # 是 ON DELETE SET NULL：删会话只断开可选关联，自动/人工核对历史仍须保留。
    await session.execute(delete(Citation).where(
        Citation.source_kind == "assertion", Citation.source_id.in_(assertion_ids)))
    await session.execute(delete(RetrievalCandidate).where(
        RetrievalCandidate.turn_id.in_(turn_ids)))
    await session.execute(delete(Assertion).where(Assertion.message_id.in_(message_ids)))
    await session.execute(delete(AgentTurn).where(AgentTurn.message_id.in_(message_ids)))
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


def _crop_url(document_id: str, crop_key: str | None) -> str | None:
    if not crop_key:
        return None
    job_id, name = crop_key.split("/")[1], crop_key.rsplit("/", 1)[-1]
    return f"/api/documents/{document_id}/crops/{job_id}/{name}"


@router.get("/evidence/{evidence_id}")
async def get_evidence_detail(evidence_id: str, user: User = Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise APIError(404, "evidence not found", "invalid_request_error", "evidence_not_found")
    document = await _owned_document(evidence.document_id, user, session)
    verifications = (await session.execute(
        select(EvidenceVerification).where(
            EvidenceVerification.evidence_id == evidence.id)
        .order_by(EvidenceVerification.created_at, EvidenceVerification.id)
    )).scalars().all()
    chunk = (await session.execute(
        select(Chunk).where(
            Chunk.parse_job_id == evidence.parse_job_id,
            Chunk.seq == evidence.seq,
            # (job, seq) 只定位槽位，不证明它仍是这条历史 Evidence。重建后同槽
            # 内容可能变化；只有显式 FK 指回当前 Evidence 才能暴露 chunk_id。
            or_(Chunk.evidence_id == evidence.id,
                Chunk.derived_evidence_id == evidence.id),
        )
    )).scalars().first()
    return {
        "id": evidence.id,
        "document": {"id": document.id, "filename": document.filename},
        "page_idx": evidence.page_idx, "seq": evidence.seq,
        "parse_job_id": evidence.parse_job_id, "doc_version": evidence.doc_version,
        "bbox": evidence.bbox, "page_size": evidence.page_size, "kind": evidence.kind,
        "content": evidence.content, "source_type": (
            "generated" if evidence.derived_from else "source"),
        "derived_from": evidence.derived_from,
        "crop_url": _crop_url(document.id, evidence.crop_key),
        "review_state": evidence.review_state,
        "chunk_id": chunk.id if chunk else None,
        "verifications": [{
            "id": row.id, "mode": row.mode, "verdict": row.verdict,
            "reason_code": row.reason_code, "reason_text": row.reason_text,
            "reviewer_id": row.reviewer_id, "created_at": row.created_at,
        } for row in verifications],
    }


async def _refresh_assertion_review_state(session: AsyncSession,
                                          assertion_ids: list[str]) -> None:
    for assertion_id in assertion_ids:
        states = (await session.execute(
            select(Evidence.review_state).join(
                Citation, Citation.evidence_id == Evidence.id).where(
                    Citation.source_kind == "assertion",
                    Citation.source_id == assertion_id,
                    Citation.role == "primary")
        )).scalars().all()
        if not states:
            state = "unverified"
        elif "rejected" in states:
            state = "rejected"
        elif "questioned" in states:
            state = "questioned"
        elif all(value == "passed" for value in states):
            state = "passed"
        else:
            state = "unverified"
        await session.execute(
            update(Assertion).where(Assertion.id == assertion_id).values(
                verification_state=state, verification_mode="human"))


@router.post("/evidence/{evidence_id}/verification", status_code=201)
async def verify_evidence_human(evidence_id: str, req: HumanVerificationRequest,
                                user: User = Depends(current_user),
                                session: AsyncSession = Depends(get_session)):
    """人工只提交核对标注；没有任何修改 Evidence 内容/bbox 的入口。"""
    evidence = (await session.execute(
        select(Evidence).where(Evidence.id == evidence_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if evidence is None:
        raise APIError(404, "evidence not found", "invalid_request_error", "evidence_not_found")
    await _owned_document(evidence.document_id, user, session)
    state = {"pass": "passed", "reject": "rejected", "question": "questioned"}[
        req.verdict]
    verification = EvidenceVerification(
        evidence_id=evidence.id, mode="human", verdict=req.verdict,
        reason_code=req.reason_code, reason_text=req.reason_text, reviewer_id=user.id)
    session.add(verification)
    evidence.review_state = state
    assertion_ids = list(dict.fromkeys((await session.execute(
        select(Citation.source_id).where(
            Citation.source_kind == "assertion", Citation.evidence_id == evidence.id,
            Citation.role == "primary")
    )).scalars().all()))
    await _refresh_assertion_review_state(session, assertion_ids)
    await session.commit()
    return {"id": verification.id, "evidence_id": evidence.id, "mode": "human",
            "verdict": req.verdict, "review_state": state,
            "reason_code": req.reason_code, "reason_text": req.reason_text,
            "created_at": verification.created_at}


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
    inherited_ids = await _latest_evidence_ids(session, history)
    session.add(Message(conversation_id=cid, role="user", content=req.question))
    if conversation.title == "新会话":
        conversation.title = req.question[:40]
    conversation.updated_at = utcnow()
    await session.commit()

    http: httpx.AsyncClient = request.app.state.http
    index = request.app.state.search_index

    history_payload = [{"role": m.role, "content": m.content} for m in history]
    decision = await decide_retrieval(
        http, question=req.question, history=history_payload,
        inherited_evidence_ids=inherited_ids)
    if decision.need_retrieval:
        retrieval = await retrieve(session, index, http, question=req.question,
                                   document=document)
        if retrieval.degraded is None and decision.degraded:
            retrieval.degraded = decision.degraded
    else:
        retrieval = await inherited_retrieval(session, decision.inherited_evidence_ids)
        if retrieval.degraded in {"no_evidence_in_turn", "inherited_evidence_incomplete"}:
            # 判定时有 ID，不代表证据仍能接回当前索引。重建/删除后继承集可能全失效；
            # 此时必须把决定收紧成显式拒答，不能让空资料 prompt 落回模型常识。
            decision = QueryDecision(
                need_retrieval=False, reason=decision.reason,
                inherited_evidence_ids=decision.inherited_evidence_ids,
                degraded=retrieval.degraded)
    refusing = decision.degraded in {"no_evidence_in_turn", "inherited_evidence_incomplete"}
    if not refusing:
        # 候选审计保持完整，但出处编号只为回答模型实际看到的上下文分配。
        trim_hits_to_context(retrieval)
    crops = (await attach_crops(retrieval, storage, document, job)
             if retrieval.hits and not refusing else [])
    image_uris = [uri for uri, _ in crops]
    messages = build_messages(req.question, retrieval, history_payload, image_uris)

    return StreamingResponse(
        _stream_answer(http, messages, retrieval, decision=decision, conversation_id=cid,
                       document_id=document.id, user_id=user.id, has_image=bool(image_uris),
                       expected_job_id=job.id,
                       expected_generation=document.index_generation,
                       # 图与文本必须成对取，不能一个取 image_uris[0] 一个取 hits[0]
                       verify_pair=(crops[0][0], crops[0][1]["text"],
                                    crops[0][1].get("evidence_id")) if crops else None),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_answer(http: httpx.AsyncClient, messages: list[dict], retrieval: Retrieval, *,
                         decision: QueryDecision | None = None,
                         conversation_id: str, document_id: str, user_id: str, has_image: bool,
                         verify_pair: tuple[str, str, str | None] | None = None,
                         expected_job_id: str | None = None,
                         expected_generation: int | None = None):
    message_id = None
    decision = decision or QueryDecision(need_retrieval=True, reason="legacy_caller")
    chunks: list[str] = []
    degraded = retrieval.degraded
    verified = False
    verify_verdict: bool | None = None
    error: dict | None = None
    index_changed = False

    # 出处一致性核对（A4）与回答**并发**跑：核对要多打一次视觉模型，
    # 串行做会把首字延迟顶上去，而它的结论只在最后的 done 帧里才用得上
    verify_task: asyncio.Task | None = None
    refusing = decision.degraded in {"no_evidence_in_turn", "inherited_evidence_incomplete"}
    if settings.qa_verify_parse and verify_pair and not refusing:
        verify_task = asyncio.create_task(
            verify_parse_consistency(http, verify_pair[0], verify_pair[1]))

    yield _sse("meta", {
        "query_decision": {
            "need_retrieval": decision.need_retrieval, "reason": decision.reason,
            "inherited_evidence_ids": decision.inherited_evidence_ids,
            "degraded": decision.degraded,
        },
        "retrieval": {
            "chunk_ids": [h["chunk_id"] for h in retrieval.hits],
            "candidates": [candidate.as_dict() for candidate in retrieval.candidates],
        },
    })

    try:
        try:
            if decision.degraded in {"no_evidence_in_turn", "inherited_evidence_incomplete"}:
                refusal = (REFUSAL_INCOMPLETE_EVIDENCE
                           if decision.degraded == "inherited_evidence_incomplete"
                           else REFUSAL_NO_EVIDENCE)
                chunks.append(refusal)
                yield _sse("delta", {"text": refusal})
            else:
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
        elif verify_task is not None:
            verify_verdict = await _verdict(verify_task)
            if verify_verdict is True:
                verified = True
            elif verify_verdict is False:
                # 图上的字和 chunk 文本对不上 -> 解析很可能错了。此时既不能说"已验证"，
                # 也不能装作没事：这正是七种降级留下的唯一的洞（A4）。
                # 这里会覆盖已有的 degraded，但唯一可能被覆盖的 vision_unavailable
                # 实际不会发生 —— 视觉运行时挂了的话核对也打不通，_verdict 返回 None。
                # 而 embedding_unavailable + 解析出错是真实组合，此时"出处存疑"更该说出口
                degraded, verified = "parse_mismatch", False
            elif degraded is None:
                degraded = "verification_unavailable"
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
        if refusing:
            # 防御直接调用 `_stream_answer` 的路径：拒答没有受支持断言，绝不能
            # 继承核对结果或生成一条与断言无关联的自动核对记录。
            verified, verify_verdict = False, None
        (message_id, verified, degraded, index_changed,
         assertion_payload) = await asyncio.shield(_persist(
            conversation_id=conversation_id, user_id=user_id, content="".join(chunks),
            citations=retrieval.citations, verified=verified and not error, degraded=degraded,
            model_meta=answer_model_meta(), document_id=document_id,
            expected_job_id=expected_job_id, expected_generation=expected_generation,
            decision=decision, candidates=retrieval.candidates,
            verify_verdict=verify_verdict,
            verify_evidence_id=verify_pair[2] if verify_pair else None))

    cited_raw: list[dict] = []
    seen_evidence: set[str] = set()
    for assertion in assertion_payload:
        for value in assertion["_citations"]:
            evidence_id = value.get("evidence_id")
            if evidence_id and evidence_id not in seen_evidence:
                seen_evidence.add(evidence_id)
                cited_raw.append(value)
    citation_payload = [citation_out(document_id, c, fresh=not index_changed)
                        for c in cited_raw]
    if index_changed:
        for citation in citation_payload:
            citation["resolved"] = False
            citation["chunk_id"] = None
    yield _sse("citations", {"citations": citation_payload})
    for assertion in assertion_payload:
        assertion["citations"] = [
            citation_out(document_id, value, fresh=not index_changed)
            for value in assertion.pop("_citations")]
        if index_changed:
            for citation in assertion["citations"]:
                citation["resolved"] = False
                citation["chunk_id"] = None
    yield _sse("assertions", {"assertions": assertion_payload})
    yield _sse("done", {"message_id": message_id, "verified": verified,
                        "degraded": degraded,
                        # 出处够不够可信要跟着回答一起给：只给出处不给可信度，
                        # 用户没法判断该不该采信（kotaemon 的做法，A2）
                        "confidence": retrieval_confidence(cited_raw)})


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
                   verified: bool, degraded: str | None, model_meta: dict | None = None,
                   document_id: str | None = None, expected_job_id: str | None = None,
                   expected_generation: int | None = None,
                   decision: QueryDecision | None = None,
                   candidates: list[CandidateDecision] | None = None,
                   verify_verdict: bool | None = None,
                   verify_evidence_id: str | None = None,
                   ) -> tuple[str, bool, str | None, bool, list[dict]]:
    """把回答落库。

    **必须新开 session**：请求作用域的那个在响应体开始流之前就关了，
    复用它必炸（M5 在 proxy 上踩过，见 proxy.py 模块 docstring）。
    """
    async with get_sessionmaker()() as db:
        decision = decision or QueryDecision(need_retrieval=True, reason="legacy_caller")
        candidates = candidates or []
        index_changed = False
        if (document_id is not None and expected_job_id is not None
                and expected_generation is not None):
            # generation 核对与 Message/Citation 提交必须在同一把 Document 行锁内；
            # 否则 reindex 仍可插进“检查通过 → record_evidence”之间。
            document = (await db.execute(
                select(Document).where(Document.id == document_id).with_for_update()
            )).scalar_one_or_none()
            index_changed = (document is None or document.current_job_id != expected_job_id
                             or document.index_generation != expected_generation
                             or document.index_status != "ready")
            if index_changed:
                # 核对针对的是检索时的旧索引。即使图文一致，也不能在当前索引已
                # 切换后继续把断言标 passed，更不能落一条无效的自动核对记录。
                verified, verify_verdict, verify_evidence_id = False, None, None
                degraded = "index_changed_during_answer"
        ordered_evidence = [citation.get("evidence_id") for citation in citations
                            if citation.get("evidence_id")]
        parsed = assertions_from_text(content, ordered_evidence)
        projected_content = "\n".join(item["text"] for item in parsed)
        message = Message(conversation_id=conversation_id, role="assistant",
                          content=projected_content,
                          verified=verified, degraded=degraded, model_meta=model_meta or {})
        db.add(message)
        await db.flush()
        turn = AgentTurn(
            message_id=message.id, need_retrieval=decision.need_retrieval,
            decision_reason=decision.reason,
            inherited_evidence_ids=decision.inherited_evidence_ids,
            degraded=decision.degraded)
        db.add(turn)
        await db.flush()
        db.add_all([RetrievalCandidate(
            turn_id=turn.id,
            evidence_id=(candidate.hit.get("derived_evidence_id")
                         or candidate.hit.get("evidence_id")),
            document_id=candidate.hit["document_id"], rank=candidate.rank,
            score=candidate.hit.get("score"), similarity=candidate.hit.get("similarity"),
            accepted=candidate.accepted, reason=candidate.reason,
        ) for candidate in candidates])

        citation_by_evidence = {
            citation.get("evidence_id"): citation for citation in citations
            if citation.get("evidence_id")}
        verified_evidence = {verify_evidence_id} if verify_evidence_id else set()
        if verify_evidence_id:
            verified_evidence.update((await db.execute(
                select(Evidence.id).where(Evidence.derived_from == verify_evidence_id)
            )).scalars().all())

        payloads: list[dict] = []
        assertion_rows: list[Assertion] = []
        for item in parsed:
            requested_refs = item["evidence_ids"]
            if not requested_refs:
                state, mode = "unverified", None
            elif verify_verdict is True and set(requested_refs).issubset(verified_evidence):
                state, mode = "passed", "auto"
            elif verify_verdict is False and set(requested_refs) & verified_evidence:
                state, mode = "questioned", "auto"
            else:
                state, mode = "unverified", None
            assertion = Assertion(
                message_id=message.id, position=item["position"], text=item["text"],
                unsupported=not bool(requested_refs), verification_state=state,
                verification_mode=mode)
            db.add(assertion)
            await db.flush()
            assertion_rows.append(assertion)
            requested = [citation_by_evidence[evidence_id] for evidence_id in requested_refs
                         if evidence_id in citation_by_evidence]
            await record_evidence(
                db, requested, source_kind="assertion", source_id=assertion.id)
            persisted = set((await db.execute(
                select(Citation.evidence_id).where(
                    Citation.source_kind == "assertion",
                    Citation.source_id == assertion.id,
                    Citation.role == "primary")
            )).scalars().all())
            persisted_refs = [evidence_id for evidence_id in requested_refs
                              if evidence_id in persisted]
            # Citation 仍保留为审计事实并在 SSE 标 resolved=false；但失效证据
            # 不能继续出现在 Assertion.evidence_ids 这个“当前支持集”里。
            refs = [] if index_changed else persisted_refs
            selected = [citation_by_evidence[evidence_id] for evidence_id in persisted_refs]
            if len(persisted_refs) != len(requested_refs):
                # record_evidence 为保护回答事务会吞写入异常并计 metrics。阶段 6 起
                # Citation 是 Assertion 是否受支持的真相，不能继续忽略落库结果：
                # 当前 SSE 与刷新后的历史必须从第一帧起就是同一个事实。
                assertion.unsupported = True
                assertion.verification_state = "unverified"
                assertion.verification_mode = None
                state, mode = "unverified", None
                degraded, verified = "citation_persist_failed", False
                message.degraded, message.verified = degraded, False
            elif index_changed:
                assertion.unsupported = True
                assertion.verification_state = "unverified"
                assertion.verification_mode = None
                state, mode = "unverified", None
            payloads.append({
                "id": assertion.id, "position": assertion.position,
                "text": assertion.text, "evidence_ids": refs,
                "verification": {"state": state, "mode": mode},
                "unsupported": assertion.unsupported, "_citations": selected,
            })

        # message.verified 只是旧客户端投影：必须由所有断言的当前状态收敛得出，
        # 不能因第一条证据核对通过就把含未核对/无支持断言的整条回答标绿。
        verified = bool(payloads) and all(
            not payload["unsupported"]
            and payload["verification"]["state"] == "passed"
            for payload in payloads)
        message.verified = verified

        if verify_verdict is not None and verify_evidence_id:
            linked_assertion = next((row for row, payload in zip(assertion_rows, payloads)
                                     if set(payload["evidence_ids"]) & verified_evidence), None)
            db.add(EvidenceVerification(
                evidence_id=verify_evidence_id,
                assertion_id=linked_assertion.id if linked_assertion else None,
                mode="auto", verdict="pass" if verify_verdict else "question",
                reason_code=None if verify_verdict else "parse_mismatch"))
        await record_usage(db, user_id=user_id, kind="qa", requests=1)
        await db.commit()
        return message.id, verified, degraded, index_changed, payloads


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
