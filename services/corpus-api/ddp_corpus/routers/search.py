"""跨文档检索：在自己的全部文档里找内容，命中带页码可直达。

与问答共用同一套混合检索（`ddp_core/search.py`），区别只是不限定 document_id。
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.document_context import search_contexts
from ddp_corpus.policy import authorized_document_ids, require_document, visible_document_condition
from ddp_corpus.models import Document
from ddp_corpus.upstream import embed_one

router = APIRouter()


@router.get("/search")
async def search(request: Request, q: str = "", doc: str = "", limit: int = 20,
                 actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session)):
    if not q.strip():
        return {"query": q, "groups": []}

    if doc:
        await require_document(session, actor, doc)
    permitted_ids = await authorized_document_ids(session, actor)
    if not permitted_ids:
        return {"query": q, "groups": []}

    contexts = await search_contexts(session, actor, doc or None)
    if not contexts:
        return {"query": q, "degraded": "resource_index_unavailable", "groups": []}
    http = request.app.state.http
    index = request.app.state.search_index
    degraded: str | None = None
    try:
        vector = await embed_one(http, q)
    except Exception:
        # 只走关键词路，并如实告诉调用方——不许拿零向量假装语义检索还在工作
        vector, degraded = None, "embedding_unavailable"

    hits = await index.search(session, vector=vector, query=q, document_id=doc or None,
                              limit=min(limit, 50),
                              candidates=max(limit, settings.qa_candidates),
                              min_similarity=settings.qa_min_similarity,
                              authorized_document_ids=permitted_ids, authorized_parse_job_ids=list(contexts))
    if not hits:
        return {"query": q, "degraded": degraded, "groups": []}

    documents = {
        d.id: d for d in (await session.execute(
            select(Document).where(Document.id.in_({h["document_id"] for h in hits}),
                                   visible_document_condition(actor))
        )).scalars().all()
    }

    # Recheck after the model/search await; a withdrawn source must not survive
    # through a cached hit. Each group has explicit asset and fixed version identity.
    contexts = await search_contexts(session, actor, doc or None)
    groups: dict[str, dict] = {}
    for hit in hits:
        document = documents.get(hit["document_id"])
        if document is None or document.deleted_at is not None:
            continue
        for context in contexts.get(hit["parse_job_id"], []):
            key = context.version_id or document.id
            group = groups.setdefault(key, {
                "document_id": document.id, "resource_id": context.resource_id,
                "source_version_id": context.version_id, "parse_revision": hit["parse_job_id"],
                "filename": context.filename, "hits": [],
            })
            group["hits"].append({
                "chunk_id": hit["chunk_id"], "page_idx": hit["page_idx"], "bbox": hit.get("bbox"),
                "score": hit.get("score"), "similarity": hit.get("similarity"),
                "snippet": " ".join(hit["text"].split())[:200],
            })
    return {"query": q, "degraded": degraded, "groups": list(groups.values())}
