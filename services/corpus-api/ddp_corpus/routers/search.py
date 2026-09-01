"""跨文档检索：在自己的全部文档里找内容，命中带页码可直达。

与问答共用同一套混合检索（`ddp_core/search.py`），区别只是不限定 document_id。
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import current_user
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, User
from ddp_corpus.upstream import embed_one

router = APIRouter()


@router.get("/search")
async def search(request: Request, q: str = "", doc: str = "", limit: int = 20,
                 user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)):
    if not q.strip():
        return {"query": q, "groups": []}

    if doc:
        # 指定文档时前置校验它存在且未删。**不再判归属**（1b）——
        # 语料是整个部署共享的，"只能搜自己的文档"这条限制随之消失
        target = await session.get(Document, doc)
        if target is None or target.deleted_at is not None:
            raise APIError(404, "document not found", "invalid_request_error",
                           "document_not_found")

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
                              min_similarity=settings.qa_min_similarity)
    if not hits:
        return {"query": q, "degraded": degraded, "groups": []}

    documents = {
        d.id: d for d in (await session.execute(
            select(Document).where(Document.id.in_({h["document_id"] for h in hits}))
        )).scalars().all()
    }

    groups: dict[str, dict] = {}
    for hit in hits:
        document = documents.get(hit["document_id"])
        if document is None or document.deleted_at is not None:
            continue        # 检索层已过滤，这里是纵深防御（软删除是唯一剩下的可见性条件）
        group = groups.setdefault(document.id, {
            "document_id": document.id, "filename": document.filename, "hits": [],
        })
        group["hits"].append({
            "chunk_id": hit["chunk_id"], "page_idx": hit["page_idx"], "bbox": hit.get("bbox"),
            # score 是 RRF 名次分（只排序用），similarity 才是"有多相关"
            "score": hit.get("score"), "similarity": hit.get("similarity"),
            "snippet": " ".join(hit["text"].split())[:200],
        })
    return {"query": q, "degraded": degraded, "groups": list(groups.values())}
