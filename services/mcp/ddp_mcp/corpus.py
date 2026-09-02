"""语料级 MCP 工具实现；直接连接 core 的 PostgreSQL/MinIO。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager

import httpx
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent
try:
    from minio import Minio
except ImportError:  # 老开发 venv 尚未补装时，文本工具仍可加载；部署依赖已显式声明
    Minio = None
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ddp_core.agent import assertions_from_text
from ddp_core.knowledge import neighbor_ids, normalize_entity_name
from ddp_core.models import (
    Chunk, Citation, Document, Evidence, GraphEdge, KnowledgeEntity, WikiEntry, WikiSection,
    WikiSentence,
)
from ddp_core.search import PgVectorIndex

DATABASE_URL = os.environ.get("CORPUS_DATABASE_URL", "")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:9000")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "change-me")
MIN_SIMILARITY = float(os.environ.get("MCP_MIN_SIMILARITY", "0.55"))

_engine = None
_sessions = None
_index = PgVectorIndex()
_http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=300), trust_env=False)
_minio = None


def _headers() -> dict:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _sessionmaker():
    global _engine, _sessions
    if not DATABASE_URL:
        raise RuntimeError("CORPUS_DATABASE_URL is not configured")
    if _sessions is None:
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessions


@asynccontextmanager
async def corpus_session():
    async with _sessionmaker()() as session:
        yield session


def _minio_client():
    global _minio
    endpoint = os.environ.get("MINIO_ENDPOINT", "")
    if not endpoint or Minio is None:
        return None
    if _minio is None:
        _minio = Minio(endpoint.removeprefix("http://").removeprefix("https://"),
                       access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
                       secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
                       secure=endpoint.startswith("https://"))
    return _minio


async def _crop_url(document_id: str, key: str | None) -> str | None:
    """返回经 Web 鉴权的稳定裁图路径，不泄露 MinIO 内网预签名地址。"""
    if not key:
        return None
    parts = key.split("/")
    if len(parts) < 3:
        return None
    base = os.environ.get("MCP_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/api/documents/{document_id}/crops/{parts[-2]}/{parts[-1]}"


async def _crop_bytes(key: str | None) -> tuple[bytes | None, str | None]:
    """返回 (裁图字节, 降级原因)。**"没有图"与"取不到图"必须分开** ——

    外部 agent 拿不到像素就核对不了这条证据；把两者都渲染成"没有图"，
    等于把一次配置缺失（minio 没装/没配）伪装成"这条证据本来就没裁图"。
    不变式 2：任何降级都必须可见。
    """
    if not key:
        return None, None
    client = _minio_client()
    if client is None:
        return None, "crop_store_unavailable"
    bucket = os.environ.get("MINIO_BUCKET", "deepdocparse")

    def read():
        response = client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    try:
        return await asyncio.to_thread(read), None
    except Exception:
        return None, "crop_read_failed"


async def _embedding(query: str) -> tuple[list[float] | None, str | None]:
    try:
        response = await _http.post(f"{GATEWAY}/v1/embeddings", headers=_headers(),
                                    json={"input": query})
        if response.status_code != 200:
            return None, "embedding_unavailable"
        return response.json()["data"][0]["embedding"], None
    except Exception:
        return None, "embedding_unavailable"


async def _evidence_payload(session: AsyncSession, evidence: Evidence,
                            *, score=None, similarity=None) -> dict:
    chunk = (await session.execute(select(Chunk).where(
        Chunk.parse_job_id == evidence.parse_job_id, Chunk.seq == evidence.seq,
        or_(Chunk.evidence_id == evidence.id, Chunk.derived_evidence_id == evidence.id)
    ))).scalar_one_or_none()
    document = await session.get(Document, evidence.document_id)
    return {
        "evidence_id": evidence.id, "document_id": evidence.document_id,
        "document": document.filename if document else None,
        "page_idx": evidence.page_idx, "seq": evidence.seq, "bbox": evidence.bbox,
        "page_size": evidence.page_size, "kind": evidence.kind,
        "content": evidence.content, "snippet": evidence.content[:500],
        "source_type": "generated" if evidence.derived_from else "source",
        "derived_from": evidence.derived_from, "review_state": evidence.review_state,
        "resolved": chunk is not None, "chunk_id": chunk.id if chunk else None,
        "crop_url": await _crop_url(evidence.document_id, evidence.crop_key),
        "score": score, "similarity": similarity,
    }


async def search_impl(query: str, limit: int = 10) -> dict:
    if not query.strip():
        return {"results": [], "degraded": "empty_query"}
    limit = max(1, min(limit, 50))
    vector, degraded = await _embedding(query)
    async with corpus_session() as session:
        hits = await _index.search(session, vector=vector, query=query, document_id=None,
                                   limit=limit, candidates=max(20, limit * 3),
                                   min_similarity=MIN_SIMILARITY)
        ids = [hit.get("derived_evidence_id") or hit.get("evidence_id") for hit in hits]
        evidence = {row.id: row for row in (await session.execute(
            select(Evidence).where(Evidence.id.in_([value for value in ids if value]))
        )).scalars().all()}
        results = [await _evidence_payload(
            session, evidence[eid], score=hit.get("score"), similarity=hit.get("similarity"))
            for hit, eid in zip(hits, ids) if eid in evidence]
    return {"results": results, "degraded": degraded if vector is None else None}


async def ask_impl(question: str) -> dict:
    found = await search_impl(question, 8)
    evidence = found["results"]
    if not evidence:
        return {"assertions": [{"text": "语料中未找到可支持的证据。", "evidence_ids": [],
                                "verification": {"state": "unverified", "mode": None},
                                "unsupported": True, "citations": []}],
                # **契约里的 `no_hits`，不是自己造一个同义词。**
                # `no_relevant_chunks` 不在 enums.yaml 里，于是它没有用户可见
                # 文案：界面上要么显示这个裸标识符，要么按枚举分支渲染时
                # 干脆什么都不显示 —— 而那正是"降级不可见"（不变式 2）。
                # 语料侧同样的情形用的就是 no_hits。
                # 这一处是补上 `return`/`or` 两种形状之后被守卫抓出来的
                "degraded": found["degraded"] or "no_hits"}
    sources = "\n\n".join(f"[{i}] {item['content']}" for i, item in enumerate(evidence, 1))
    response = await _http.post(f"{GATEWAY}/v1/chat/completions", headers=_headers(), json={
        "messages": [{"role": "system", "content": "只依据资料回答并用 [n] 引用。"},
                     {"role": "user", "content": f"【资料】\n{sources}\n【问题】\n{question}"}],
        "stream": False})
    if response.status_code != 200:
        return {"assertions": [], "degraded": "answer_unavailable"}
    text = response.json()["choices"][0]["message"]["content"] or ""
    parsed = assertions_from_text(text, [item["evidence_id"] for item in evidence])
    by_id = {item["evidence_id"]: item for item in evidence}
    assertions = []
    for item in parsed:
        citations = [by_id[value] for value in item["evidence_ids"] if value in by_id]
        assertions.append({"text": item["text"], "evidence_ids": item["evidence_ids"],
                           "verification": {"state": "unverified", "mode": None},
                           "unsupported": not bool(item["evidence_ids"]),
                           "citations": citations})
    return {"assertions": assertions, "degraded": found["degraded"]}


async def get_evidence_impl(evidence_id: str) -> ToolResult:
    async with corpus_session() as session:
        evidence = await session.get(Evidence, evidence_id)
        if evidence is None:
            return ToolResult(structured_content={"status": "not_found"}, is_error=True)
        payload = await _evidence_payload(session, evidence)
        image, payload["crop_degraded"] = await _crop_bytes(evidence.crop_key)
    content = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    if image:
        content.append(ImageContent(type="image", data=base64.b64encode(image).decode(),
                                    mimeType="image/png"))
    return ToolResult(content=content, structured_content=payload)


async def read_wiki_impl(value: str) -> dict:
    async with corpus_session() as session:
        entry = await session.get(WikiEntry, value)
        if entry is None:
            entry = (await session.execute(select(WikiEntry).where(
                WikiEntry.title == value))).scalar_one_or_none()
        if entry is None:
            return {"status": "not_found"}
        sections = (await session.execute(select(WikiSection).where(
            WikiSection.entry_id == entry.id).order_by(WikiSection.position))).scalars().all()
        out = []
        for section in sections:
            sentences = (await session.execute(select(WikiSentence).where(
                WikiSentence.section_id == section.id).order_by(WikiSentence.position)
            )).scalars().all()
            values = []
            for sentence in sentences:
                citations = (await session.execute(select(Citation, Evidence).join(
                    Evidence, Evidence.id == Citation.evidence_id).where(
                    Citation.source_kind == "wiki_sentence", Citation.source_id == sentence.id,
                    Citation.role == "primary"))).all()
                evidence_payloads = [await _evidence_payload(session, evidence)
                                     for _, evidence in citations]
                live = [item for item in evidence_payloads if item["resolved"]]
                values.append({"id": sentence.id, "text": sentence.text,
                               "evidence_ids": [item["evidence_id"] for item in live],
                               "unsupported": sentence.unsupported or not bool(live),
                               "conflict_group": sentence.conflict_group,
                               "citations": evidence_payloads})
            out.append({"heading": section.heading, "sentences": values})
        return {"status": "ok", "entry": {"id": entry.id, "title": entry.title},
                "sections": out}


async def graph_neighbors_impl(value: str, depth: int = 1) -> dict:
    if not 1 <= depth <= 3:
        return {"status": "invalid_depth"}
    async with corpus_session() as session:
        center = await session.get(KnowledgeEntity, value)
        if center is None:
            center = (await session.execute(select(KnowledgeEntity).where(
                KnowledgeEntity.normalized_name == normalize_entity_name(value)
            ))).scalar_one_or_none()
        if center is None:
            return {"status": "not_found"}
        edges = (await session.execute(select(GraphEdge))).scalars().all()
        included = neighbor_ids(center.id, [(edge.subject_id, edge.object_id) for edge in edges],
                                depth)
        entities = (await session.execute(select(KnowledgeEntity).where(
            KnowledgeEntity.id.in_(included)))).scalars().all()
        edge_out = []
        for edge in edges:
            if edge.subject_id not in included or edge.object_id not in included:
                continue
            rows = (await session.execute(select(Citation, Evidence).join(
                Evidence, Evidence.id == Citation.evidence_id).where(
                Citation.source_kind == "graph_edge", Citation.source_id == edge.id,
                Citation.role == "primary"))).all()
            citations = [await _evidence_payload(session, evidence) for _, evidence in rows]
            live = [item for item in citations if item["resolved"]]
            edge_out.append({"id": edge.id, "subject_id": edge.subject_id,
                             "predicate": edge.predicate, "object_id": edge.object_id,
                             "confidence": edge.confidence,
                             "evidence_ids": [item["evidence_id"] for item in live],
                             "unsupported": edge.unsupported or not bool(live),
                             "citations": citations})
        return {"status": "ok", "center_id": center.id,
                "entities": [{"id": row.id, "name": row.canonical_name,
                              "entity_type": row.entity_type,
                              "entity_merge_uncertain": row.entity_merge_uncertain}
                             for row in entities], "edges": edge_out}
