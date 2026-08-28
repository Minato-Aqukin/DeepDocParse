"""文档问答的编排：检索 -> 裁剪 -> 组 prompt -> 流式调用上游。

产品上这是本层最核心的能力（ARCHITECTURE.md §6 的"视觉子代理"搬到 Web 端）。
工程上要盯死两件事：

1. **降级必须可见**。检索零命中、VQA 不可达、非 PDF 不能裁剪——每一种都要在返回里
   打标（verified/degraded），前端照实显示。这个项目吃过静默降级的大亏
   （M4a 的向量检索悄悄退回 BM25，没人发现）。
2. **不把整篇文档塞进 prompt**。M5 的 ask_document 对短文档走"全文即证据"，
   那是 MCP 场景的取舍；Web 端文档可以很长，必须靠检索裁出 top-k。
3. **出处要经得起重建索引**。chunk_id 是随机 UUID，reindex 会全部重铸，
   只存它等于历史回答一次重建就永久失去原文依据（补不回来）。citations 因此
   同时存稳定定位键 `(parse_job_id, seq)`，读取时由 `app.evidence.load_citations` 接回。
"""
import base64
import difflib
import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import rerank_config, settings
from app.crops import get_or_create_crop
from app.models import Chunk, Document, ParseJob
from ddp_core.anchor import same_content
from ddp_core.rerank import rerank_hits
from ddp_core.search import Hit, SearchIndex
from app.storage import Storage
from ddp_core.tokenize import backend as tokenize_backend
from app.upstream import chat_request, embed_one

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


def answer_model_meta() -> dict:
    """这一轮回答的"配置指纹"，随 Message 落库。

    没有它，换了 chat / embedding 模型或调了检索参数之后，历史问答就无法分组对比——
    而那是判断"新配置有没有变好"的唯一依据（A1 评测集也要靠它切片）。
    空串表示"由上游注册表选默认"，如实记下来，不要在这里替上游做决定。
    """
    return {
        "chat_model": settings.chat_model,
        "chat_endpoint": settings.chat_endpoint,
        "embedding_model": settings.embedding_model,
        "embedding_endpoint": settings.embeddings_endpoint,
        "embedding_dim": settings.embedding_dim,
        "retrieval": {
            "top_k": settings.qa_top_k,
            "candidates": settings.qa_candidates,
            "min_similarity": settings.qa_min_similarity,
            "context_chars": settings.qa_context_chars,
            "crop_count": settings.qa_crop_count,
            # v1.1：这两项直接决定关键词路与精排的行为，不记下来就没法解释
            # "换了配置之后指标为什么变了"（model_meta 存在的全部理由）
            "rerank_enabled": settings.rerank_enabled,
            "rerank_model": settings.rerank_model,
            "tokenizer": tokenize_backend(),
        },
    }


def retrieval_confidence(citations: list[dict]) -> dict:
    """这组出处有多可信 —— 交给用户判断，而不是替他判断。

    只看 top-1 的余弦相似度：它有校准过的量纲（下限 0.45，实测真实命中 0.725~0.786、
    无关问题 0.246~0.381）。**不能用 citation 里的 score**：那是 RRF 名次分，
    两路都排第一就恒为 0.0328，勉强及格的出处和绝佳命中长得一模一样。

    level:
      high    —— top-1 相似度过了 qa_low_similarity
      low     —— 过了下限但没到提示线：能答，但依据不牢，界面要说出来
      unknown —— 压根没量到（向量化挂了，只有关键词路）。同样不许装作 high
    """
    sims = [c["similarity"] for c in citations if c.get("similarity") is not None]
    if not sims:      # 没出处，或出处全是关键词路命中（量不到相似度）
        return {"level": "unknown", "top_similarity": None,
                "warn_below": settings.qa_low_similarity}
    top = max(sims)
    return {"level": "high" if top >= settings.qa_low_similarity else "low",
            "top_similarity": top, "warn_below": settings.qa_low_similarity}


async def retrieve(session: AsyncSession, index: SearchIndex, http: httpx.AsyncClient, *,
                   question: str, document: Document) -> Retrieval:
    """混合检索 + 出处裁剪。任何一步不可用都降级——但降级要**说出来**。"""
    degraded: str | None = None
    try:
        vector = await embed_one(http, question)
    except Exception:
        # 向量化挂了就只走关键词路，并如实打标。
        # 绝不能拿零向量顶上：那样检索照跑、结果照返，用户以为是语义命中，
        # 实际是一堆噪声——正是铁律 3 要杜绝的静默降级
        vector, degraded = None, "embedding_unavailable"

    # 开了精排就多要候选：rerank 的价值全在"从更大的候选池里挑"，
    # 候选 == top_k 时它只是把已经选定的几条重新排了个序（config 有启动期校验）
    if settings.rerank_enabled:
        limit, candidates = settings.rerank_candidates, settings.rerank_candidates
    else:
        limit, candidates = settings.qa_top_k, settings.qa_candidates

    hits = await index.search(session, vector=vector, query=question,
                              document_id=document.id,
                              limit=limit, candidates=candidates,
                              min_similarity=settings.qa_min_similarity)
    if not hits:
        return Retrieval(degraded=degraded or "no_hits")

    hits, rerank_degraded = await rerank_hits(http, question, hits,
                                              top_k=settings.qa_top_k, cfg=rerank_config())
    # 向量化不可用比"没重排"严重得多，不能被后者盖掉 —— 前者意味着整条语义路都没跑
    return Retrieval(hits=hits, degraded=degraded or rerank_degraded)


async def attach_crops(retrieval: Retrieval, storage: Storage, document: Document,
                       job: ParseJob) -> list[tuple[str, Hit]]:
    """给前 N 个命中生成区域截图。

    返回的是 **(data URI, 对应的命中) 成对列表**，不是裸 URI 列表：出处一致性核对
    要拿"这张图"和"这张图对应那个块的文本"比对。只返回 URI 的话，一旦 hits[0]
    裁剪失败而 hits[1] 成功，调用方就会拿**另一个块的图**去核对 hits[0] 的文本，
    判出一个假的 parse_mismatch —— 而误报比不报更伤信任。
    （qa_crop_count=1 时这条恒不发生，正因如此它能一直藏着。）
    """
    crops: list[tuple[str, Hit]] = []
    crop_supported = "pdf" in (document.mime or "").lower() and bool(document.object_key)

    for idx, hit in enumerate(retrieval.hits):
        key = None
        if idx < settings.qa_crop_count:
            key = await get_or_create_crop(
                storage, job_id=job.id, source_key=document.object_key, mime=document.mime,
                page_idx=hit["page_idx"], bbox=hit.get("bbox"), page_size=hit.get("page_size"))
            if key:
                raw = await storage.get(key)
                crops.append(("data:image/png;base64," + base64.b64encode(raw).decode(), hit))
        generated = bool(hit.get("derived_text") and hit.get("derived_evidence_id"))
        evidence_id = hit.get("derived_evidence_id") if generated else hit.get("evidence_id")
        cited_text = hit.get("derived_text") if generated else hit["text"]
        retrieval.citations.append({
            # chunk_id 只作即时引用；(parse_job_id, seq) 才是熬得过 reindex 的定位键
            "chunk_id": hit["chunk_id"], "parse_job_id": hit.get("parse_job_id"),
            "seq": hit.get("seq"), "page_idx": hit["page_idx"], "bbox": hit.get("bbox"),
            "crop_key": key, "snippet": _snippet(cited_text or ""),
            "evidence_id": evidence_id,
            "source_type": "generated" if generated else "source",
            # score 是 RRF 名次分（表达不了"有多相关"），similarity 才是给用户看的那个
            "score": hit.get("score"), "similarity": hit.get("similarity"),
        })

    if not crops and retrieval.degraded is None:
        retrieval.degraded = "crop_unsupported" if not crop_supported else "crop_failed"
    return crops


TRANSCRIBE_PROMPT = (
    "把这张图里的文字**原样**抄写出来，保持原有顺序。"
    "不要翻译、不要总结、不要解释，只输出文字本身。"
)
# 抄写结果短于这个长度就认为"没抄出来"（模型拒答、图糊、纯图表区域），
# 判 unknown 而不是 mismatch —— 误报会把好出处打成存疑，比不报更伤信任
_MIN_TRANSCRIPT_CHARS = 10


def _comparable(text: str) -> str:
    """比对前的归一化：去掉空白与标点，只留下文字本身。

    视觉模型抄出来的标点、空格与解析器给的几乎不可能一致，
    留着它们会把噪声算成分歧。中日文没有词边界，所以按字符比而不是按词。
    """
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


async def verify_parse_consistency(http: httpx.AsyncClient, image_uri: str,
                                   chunk_text: str) -> bool | None:
    """图上的字和 chunk 文本是不是同一段？

    返回 True=一致 / False=对不上 / None=没测出来（模型不可用、抄写太短）。
    **None 不许当成 False**：核对不了就是核对不了，把"不知道"说成"有问题"
    是另一种撒谎，而且会毁掉用户对这个标记的信任。

    这是七种降级里唯一没被覆盖的洞：解析错了的时候，chunk 文本是错的，
    但语义相似度照样过阈值、照样裁图、照样标 verified ——
    产出"带着已做视觉验证标记的假出处"。这也正是视觉检索路线（ColPali 系）
    对文本管线的核心攻击点。
    """
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image_uri}},
        {"type": "text", "text": TRANSCRIBE_PROMPT},
    ]}]
    try:
        response = await http.send(chat_request(http, messages, stream=False))
        if response.status_code != 200:
            return None
        transcript = response.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None                 # 视觉模型不可用 —— 那由 vision_unavailable 去标

    left, right = _comparable(transcript), _comparable(chunk_text)
    if len(left) < _MIN_TRANSCRIPT_CHARS or not right:
        return None
    # autojunk=False：默认启发式会把长串里出现频繁的字符当"垃圾"忽略，
    # 中文正文里"的""是"这类字首当其冲 —— 一致度被压低，判定偏向误报 mismatch，
    # 与"宁可漏报不要误报"的取向正好相反
    ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    return ratio >= settings.qa_parse_mismatch_threshold


def build_messages(question: str, retrieval: Retrieval, history: list[dict],
                   image_uris: list[str]) -> list[dict]:
    """组多模态消息。资料段有字符预算，超了就截断——长文档不能整篇塞进去。"""
    sources: list[str] = []
    budget = settings.qa_context_chars
    for i, hit in enumerate(retrieval.hits, start=1):
        generated = hit.get("derived_text")
        if generated:
            text = (f"[生成理解，原子证据 {hit.get('evidence_id') or '未编号'}]\n"
                    f"{generated}\n[原文/OCR]\n{hit['text']}")
        else:
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
