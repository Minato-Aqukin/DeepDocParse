"""文档问答的编排：检索 -> 裁剪 -> 组 prompt -> 流式调用上游。

产品上这是本层最核心的能力（ARCHITECTURE.md §6 的"视觉子代理"搬到 Web 端）。
工程上要盯死两件事：

1. **降级必须可见**。检索零命中、VQA 不可达、非 PDF 不能裁剪——每一种都要在返回里
   打标（verified/degraded），前端照实显示。这个项目吃过静默降级的大亏
   （M4a 的向量检索悄悄退回 BM25，没人发现）。
2. **不把整篇文档塞进 prompt**。M5 的 ask_document 对短文档走"全文即证据"，
   那是 MCP 场景的取舍；Web 端文档可以很长，必须靠检索裁出 top-k。
"""
import base64
from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crops import get_or_create_crop
from app.models import Document, ParseJob
from app.search import Hit, SearchIndex
from app.storage import Storage
from app.upstream import embed_one

SYSTEM_PROMPT = (
    "你是文档问答助手。只依据【资料】回答问题；资料中没有的信息，"
    "必须明确回答“文档中未找到”，不要凭常识补充。"
    "引用资料时用 [1] [2] 这样的编号标注来源。"
)


@dataclass
class Retrieval:
    hits: list[Hit] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    degraded: str | None = None
    verified: bool = False


def _snippet(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


async def retrieve(session: AsyncSession, index: SearchIndex, http: httpx.AsyncClient, *,
                   question: str, document: Document, user_id: str) -> Retrieval:
    """混合检索 + 出处裁剪。任何一步不可用都降级——但降级要**说出来**。"""
    degraded: str | None = None
    try:
        vector = await embed_one(http, question)
    except Exception:
        # 向量化挂了就只走关键词路，并如实打标。
        # 绝不能拿零向量顶上：那样检索照跑、结果照返，用户以为是语义命中，
        # 实际是一堆噪声——正是铁律 3 要杜绝的静默降级
        vector, degraded = None, "embedding_unavailable"

    hits = await index.search(session, vector=vector, query=question,
                              document_id=document.id, user_id=user_id,
                              limit=settings.qa_top_k, candidates=settings.qa_candidates)
    if not hits:
        return Retrieval(degraded=degraded or "no_hits")
    return Retrieval(hits=hits, degraded=degraded)


async def attach_crops(retrieval: Retrieval, storage: Storage, document: Document,
                       job: ParseJob) -> list[str]:
    """给前 N 个命中生成区域截图，返回可用于 VQA 的 data URI 列表。"""
    data_uris: list[str] = []
    crop_supported = "pdf" in (document.mime or "").lower() and bool(document.object_key)

    for idx, hit in enumerate(retrieval.hits):
        key = None
        if idx < settings.qa_crop_count:
            key = await get_or_create_crop(
                storage, job_id=job.id, source_key=document.object_key, mime=document.mime,
                page_idx=hit["page_idx"], bbox=hit.get("bbox"), page_size=hit.get("page_size"))
            if key:
                raw = await storage.get(key)
                data_uris.append("data:image/png;base64," + base64.b64encode(raw).decode())
        retrieval.citations.append({
            "chunk_id": hit["chunk_id"], "page_idx": hit["page_idx"], "bbox": hit.get("bbox"),
            "crop_key": key, "snippet": _snippet(hit["text"]), "score": hit.get("score"),
        })

    if not data_uris and retrieval.degraded is None:
        retrieval.degraded = "crop_unsupported" if not crop_supported else "crop_failed"
    return data_uris


def build_messages(question: str, retrieval: Retrieval, history: list[dict],
                   image_uris: list[str]) -> list[dict]:
    """组多模态消息。资料段有字符预算，超了就截断——长文档不能整篇塞进去。"""
    sources: list[str] = []
    budget = settings.qa_context_chars
    for i, hit in enumerate(retrieval.hits, start=1):
        text = hit["text"]
        if budget <= 0:
            break
        if len(text) > budget:
            text = text[:budget] + "…"
        sources.append(f"[{i}] (第 {hit['page_idx'] + 1} 页) {text}")
        budget -= len(text)

    parts: list[dict] = [{"type": "image_url", "image_url": {"url": uri}} for uri in image_uris]
    body = ["【资料】", "\n\n".join(sources) if sources else "（未检索到相关内容）"]
    if history:
        turns = "\n".join(f"{m['role']}: {_snippet(m['content'], 300)}" for m in history)
        body += ["", "【最近对话】", turns]
    body += ["", "【问题】", question]
    parts.append({"type": "text", "text": "\n".join(body)})

    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": parts}]
