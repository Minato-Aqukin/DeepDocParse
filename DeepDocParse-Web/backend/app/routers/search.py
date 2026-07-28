"""跨文档检索：在自己的全部文档里找内容，命中带页码可直达。

与问答共用同一套混合检索（app/search.py），区别只是不限定 document_id。
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import current_user
from app.errors import APIError
from app.models import Document, User
from app.upstream import embed_one

router = APIRouter()


@router.get("/search")
async def search(request: Request, q: str = "", doc: str = "", limit: int = 20,
                 user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)):
    if not q.strip():
        return {"query": q, "groups": []}

    if doc:
        # 只允许在自己的文档里搜。检索层虽然也按 user_id 兜了一道，
        # 但传了 document_id 时那道过滤就被换掉了，这里前置校验才稳
        target = await session.get(Document, doc)
        if target is None or target.user_id != user.id or target.deleted_at is not None:
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
                              user_id=user.id, limit=min(limit, 50),
                              candidates=max(limit, settings.qa_candidates))
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
        if document is None or document.user_id != user.id or document.deleted_at is not None:
            continue        # 检索层已过滤，这里是纵深防御
        group = groups.setdefault(document.id, {
            "document_id": document.id, "filename": document.filename, "hits": [],
        })
        group["hits"].append({
            "chunk_id": hit["chunk_id"], "page_idx": hit["page_idx"], "bbox": hit.get("bbox"),
            "score": hit.get("score"), "snippet": " ".join(hit["text"].split())[:200],
        })
    return {"query": q, "degraded": degraded, "groups": list(groups.values())}
