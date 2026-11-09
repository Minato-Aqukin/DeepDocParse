"""结构化抽取编排（产品层）—— 「可验证出处」从 chunk 级下沉到**字段级**。

问答是「问题进、自然语言答案出」；抽取是「schema 进、记录出」。
两者共用同一条定位链路（检索 -> 定位 -> 裁剪 -> 视觉核对），所以出处形状完全一致，
前端 `CitationChip.vue` 一个字不用改就能复用。

## 三条不可让步的规矩

1. **字段值必须走「先检索定位到块 -> 再从块里抽值」。**
   不许把全文塞进 prompt 让模型直接吐 JSON —— 那样出处只能靠事后字符串匹配倒推，
   会产出带着"已验证"标记的假出处，也违反 plan.md 那条贯穿性准则
   （信息进了潜空间就指不回 bbox）。这条写进了 openapi.yaml，不是工程口味。
2. **not_found 与 error 必须分开。** 合成一个的话，"这份合同没写违约金"和
   "我们的检索挂了"长得一模一样，而空值看起来像结论 —— 这是抽取里最危险的输出。
3. **绝不为了填满 schema 而编值。** 抽不到就是 not_found。
   这是 docs/EVAL.md「拒答正确率」在抽取上的对应物。

格式契约见 ../DeepDocParse/docs/extract-format.md（本层按铁律 1 各写一份实现）。
"""
import asyncio
import base64
import json
from dataclasses import dataclass, field as dc_field
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.config import rerank_config, settings
from ddp_corpus.errors import APIError
from ddp_corpus.crops import get_or_create_crop
from ddp_core.extract_format import (
    FIELD_SELECTION_PROTOCOL, RECORDS_SELECTION_PROTOCOL, CoerceError, FieldSpec,
    SchemaSpec, coerce_value, field_result, overall_status, field_messages,
    field_response_format, field_source_groups, parse_json_object, resolve_field_support,
    resolve_record_source, scope_phrase_query, value_supported_by_source,
)
from ddp_corpus.models import Document, ParseJob
from ddp_corpus.qa import verify_parse_consistency
from ddp_core.search import Hit, SearchIndex, adjacent_context_hits, search_query
from ddp_corpus.storage import Storage
from ddp_corpus.upstream import chat_request, embed_batched
from ddp_core.rerank import rerank_hits

_SYSTEM = (
    "你是文档信息抽取器。只依据【资料】抽取，资料里没有的信息必须如实报告没有，"
    "**绝不猜测、绝不用常识补充**。只输出 JSON，不要任何解释文字。"
)


_RECORDS_PROMPT = """【记录字段】
{fields}

【资料】
{sources}

上面的资料可能包含**多条**同构记录（例如表格的多行）。把它们抽成 JSON：
{{"records": [{{"source": 资料编号, "values": {{"字段名": 值或 null, ...}}}}, ...]}}

规则：
- 资料里一条记录都没有 -> {{"records": []}}
- 某条记录缺某个字段 -> values 里该字段填 null，不要跳过整条记录
- 每条记录的 source 写它读的那份资料的编号：只读了其中一份就只写那一份；
  不许写没读过的编号，也不许把两份编号都挂上。用户字段一律放在 values 里，
  source 只写编号不写业务值。
- **不要编造记录，也不要把表头当成一条记录。**"""


@dataclass
class ExtractOutcome:
    """一份文档的抽取结果。records 为空表示顶层是 object（结果在 fields 里）。"""

    fields: dict = dc_field(default_factory=dict)
    records: list[dict] = dc_field(default_factory=list)
    status: str = "ok"
    degraded: str | None = None
    usage: dict = dc_field(default_factory=dict)


class ExtractContext:
    def __init__(self, *, session: AsyncSession, index: SearchIndex, http: httpx.AsyncClient,
                 storage: Storage, document: Document, job: ParseJob | None,
                 actor_id: str, verify: bool | None = None,
                 authorize: Callable[[], Awaitable[None]] | None = None,
                 authorized_parse_job_ids: list[str] | None = None,
                 authorize_original: Callable[[], Awaitable[str | None]] | None = None):
        self.session = session
        self.index = index
        self.http = http
        self.storage = storage
        self.document = document
        self.job = job
        self.actor_id = actor_id
        self._authorize = authorize
        self._authorize_original = authorize_original
        self.authorized_parse_job_ids = authorized_parse_job_ids
        self.verify = settings.extract_verify if verify is None else verify
        self.usage = {"fields": 0, "retrievals": 0, "chat_calls": 0, "verifications": 0}
        # 核对是有预算的：每次核对 = 一次渲染 + 一次视觉模型调用。
        # 不封顶的话一次 30 字段的抽取会变成 60 次模型调用
        self._verify_budget = settings.extract_verify_fields
        # 同一个块被多个字段引用时只裁一次图。裁剪很贵（渲染整页再切），
        # 而"甲方名称"和"甲方地址"命中同一段是常态。键带上引用指向的证据：
        # 同块原文/生成两个视图共用区域图，但核对的是各自引用的正文。
        self._crop_cache: dict[tuple[str, int, str],
                              tuple[str | None, bool | None, bool]] = {}
        # **字段并发跑，但 `session` 只有一个。** AsyncSession 不允许并发操作：两个字段
        # 同时检索时，后到的那个在 SQLAlchemy 里炸（"Can't operate on closed transaction
        # inside context manager"），被 run() 的兜底吞成 upstream_error —— 2026-10-15 在
        # 真库 + 真 embedding 上实测，两字段的 schema 稳定丢一个字段。检索串行化，
        # 模型调用（大头）仍然并发
        self._session_lock = asyncio.Lock()

    async def check_access(self) -> None:
        if self._authorize is not None:
            await self._authorize()

    async def original_key(self) -> str | None:
        """Object key of the original for pixel reads (crops, visual verification).

        Stored excerpts stay usable after a licensed copy's term ends; pixels cut from
        the original do not, so the run's authorizer may raise 410 source_unavailable.
        """
        if self._authorize_original is None:
            return self.document.object_key
        return await self._authorize_original()


# ---------- 上游 ----------

async def _chat(ctx: ExtractContext, messages: list[dict], *,
                temperature: float | None = None,
                response_format: dict | None = None) -> str | None:
    """调 chat 端点。不可达/非 200 返回 None -> 字段判 error。"""
    await ctx.check_access()
    ctx.usage["chat_calls"] += 1
    try:
        request = chat_request(ctx.http, messages, stream=False, temperature=temperature,
                               response_format=response_format)
        response = await ctx.http.send(request)
        if response.status_code != 200:
            return None
        return response.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None


# ---------- 检索 ----------
def _fill_field_context(seeds: list[Hit], pool: list[Hit], hybrid_order: list[Hit], *,
                        max_chars: int, max_sources: int) -> list[Hit]:
    """Spend spare context on whole recalled regions without displacing the seeds.

    Visit order diversifies ranker bias instead of re-asking the reranker: the
    strongest-hybrid seed-region completion first, then fresh recalled regions in
    hybrid order, then deferred seed-region completions if capacity remains. The
    seeds already hedge rerank and hybrid heads (see `_retrieve`); continuing down
    the deterministic hybrid (RRF vector+keyword+code, all legs) order keeps a
    lower-rerank complementary component reachable when the cross-encoder buries
    it, while whole-group atomicity and the seed set stay untouched. Deferred
    completions are skipped only for this stage, never discarded: the neighbor
    stage below can still recover an adjacent deferred fragment, and every
    admission still respects the source count and character budget.
    Char budgets count every citable view's actual text (a derived chunk's
    original and generated views both); the source count counts chunks, so a
    generated view never displaces another chunk's seat.
    """
    def view_chars(hit: Hit) -> int:
        return sum(len(_source_text(view)) for view in _hit_views(hit))
    selected = list(seeds)
    remaining = max_chars - sum(view_chars(hit) for hit in selected)
    if remaining <= 0 or len(selected) >= max_sources:
        return selected
    seen = {hit["chunk_id"] for hit in selected}
    hybrid_rank = {hit["chunk_id"]: rank for rank, hit in enumerate(hybrid_order)}

    def group_rank(group: list[int]) -> int:
        return min(hybrid_rank.get(pool[index]["chunk_id"], len(hybrid_order))
                   for index in group)

    completions, fresh = [], []
    for group in field_source_groups(pool):
        members = [pool[index] for index in group]
        if (any(hit["chunk_id"] in seen for hit in members)
                and any(hit["chunk_id"] not in seen for hit in members)):
            completions.append(group)
        else:
            fresh.append(group)
    completions.sort(key=group_rank)
    fresh.sort(key=group_rank)
    for group in completions[:1] + fresh + completions[1:]:
        additions = [pool[index] for index in group if pool[index]["chunk_id"] not in seen]
        if not additions:
            continue
        chars = sum(view_chars(hit) for hit in additions)
        if len(selected) + len(additions) > max_sources or chars > remaining:
            continue
        selected.extend(additions)
        seen.update(hit["chunk_id"] for hit in additions)
        remaining -= chars
    return selected



async def _retrieve(ctx: ExtractContext, query: str, *, k: int,
                    prefer_types: tuple[str, ...] = (),
                    include_context: bool = True,
                    query_variants: list[str] | None = None) -> tuple[list[Hit], str | None]:
    await ctx.check_access()
    ctx.usage["retrievals"] += 1
    candidates = max(settings.rerank_candidates if settings.rerank_enabled else k * 3, k)
    # 与问答 /api/search 同一条共享检索：search_query 负责分面拆查 + 批量向量化，
    # 向量化挂了它自己退回关键词路并打标（铁律 3 那条绝不能拿零向量顶上，
    # 在它内部已经处理，见 ddp_core/search.py::_search_query）
    async with ctx._session_lock:
        hits, degraded = await search_query(
            ctx.session, ctx.index, embed=lambda texts: embed_batched(ctx.http, texts),
            query=query, document_id=ctx.document.id,
            query_variants=query_variants,
            authorized_parse_job_ids=ctx.authorized_parse_job_ids,
            limit=candidates, candidates=candidates,
            min_similarity=settings.qa_min_similarity)
    if not hits:
        return [], degraded or "no_hits"

    await ctx.check_access()
    ranked, rerank_degraded = await rerank_hits(
        ctx.http, query, hits,
        top_k=len(hits) if include_context else max(k, 1),
        cfg=rerank_config())
    degraded = degraded or rerank_degraded
    hybrid_order = list(hits)
    if include_context and settings.rerank_enabled and not rerank_degraded and len(hits) > 1:
        # Thin field queries can rank a property label above its defining passage.
        # Preserve complementary heads, rather than letting rank consensus erase both.
        ranked_by_id = {hit["chunk_id"]: hit for hit in ranked}
        selected, seen = [], set()
        for position in range(max(len(hits), len(ranked))):
            for order in (ranked, hits):
                if position >= len(order):
                    continue
                hit = order[position]
                if hit["chunk_id"] not in seen:
                    seen.add(hit["chunk_id"])
                    selected.append(ranked_by_id.get(hit["chunk_id"], hit))
        hits = selected
    else:
        hits = ranked
    if prefer_types:
        # 稳定排序：表格块提前，同类保持原名次
        hits = sorted(hits, key=lambda h: 0 if h.get("block_type") in prefer_types else 1)
    seeds = hits[:k]
    if include_context and seeds:
        # Use the existing 3*k source ceiling for both recalled regions and neighbors.
        selected = _fill_field_context(
            seeds, hits, hybrid_order, max_chars=settings.qa_context_chars,
            max_sources=2 * k)
        remaining = settings.qa_context_chars - sum(
            len(_source_text(view)) for hit in selected for view in _hit_views(hit))
        await ctx.check_access()
        async with ctx._session_lock:
            neighbors = await adjacent_context_hits(
                ctx.session, selected, max_chars=remaining)
        for hit in neighbors:
            if len(selected) >= 3 * k:
                break
            chars = sum(len(_source_text(view)) for view in _hit_views(hit))
            if chars <= remaining:
                selected.append(hit)
                remaining -= chars
        seeds = selected
    return seeds, degraded


# ---------- 出处 ----------

def _snippet(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _citation(hit: Hit, crop_key: str | None) -> dict:
    """一条出处。形状与问答平面完全一致（前端组件因此可以直接复用）。

    稳定定位键是 `(parse_job_id, seq)` —— chunk_id 每次 reindex 都会重铸，
    只存它等于历史抽取结果一次重建就永久失去原文依据（P0 那条教训的抽取版）。

    Each view cites its own content: a generated view cites the derived
    evidence（source_type="generated"），an original view cites the source atom.
    （不变式 3：生成物与原文必须可区分，但最终仍指向原始原子）。
    """
    generated = bool(hit.get("derived_text") and hit.get("derived_evidence_id"))
    cited_text = hit.get("derived_text") if generated else hit["text"]
    return {
        "chunk_id": hit["chunk_id"],
        "evidence_id": hit.get("derived_evidence_id") if generated else hit.get("evidence_id"),
        "source_type": "generated" if generated else "source",
        "derived_from": hit.get("evidence_id") if generated else None,
        "parse_job_id": hit.get("parse_job_id"),
        # 产品层的稳定定位键是 (parse_job_id, seq)，doc_hash 这一路用不上；
        # 但契约把它列成了字段，如实给 None 而不是省略 —— 省略会让消费方
        # 在两个平面之间写两套取字段的代码
        "doc_hash": None,
        "seq": hit.get("seq"),
        "page_idx": hit["page_idx"],
        "printed_page_label": hit.get("printed_page_label"),
        "bbox": hit.get("bbox"),
        # 裁剪时按它换算坐标，缺它遇到 CropBox 偏移/旋转页会裁错区域
        "page_size": hit.get("page_size"),
        "crop_key": crop_key,
        "snippet": _snippet(cited_text),
        "score": hit.get("score"),
        "similarity": hit.get("similarity"),
        "rerank_score": hit.get("rerank_score"),
        "block_type": hit.get("block_type"),
    }


def _confidence(citations: list[dict]) -> dict:
    """依据有多可信 —— 交给用户判断，而不是替他判断。

    只看 top-1 的余弦相似度：它有校准过的量纲。**不能用 score**（RRF 名次分，
    两路都排第一就恒为 0.0328，勉强及格和绝佳命中长得一模一样）。
    关键词路量不到相似度时是 unknown，不许装作 high。
    """
    sims = [c["similarity"] for c in citations if c.get("similarity") is not None]
    if not sims:
        return {"level": "unknown", "top_similarity": None,
                "warn_below": settings.qa_low_similarity}
    top = max(sims)
    return {"level": "high" if top >= settings.qa_low_similarity else "low",
            "top_similarity": top, "warn_below": settings.qa_low_similarity}


def _hit_views(hit: Hit) -> list[Hit]:
    """One citable view per own content: original always first, generated second.

    A generated view cites its derived evidence; an original view cites the
    source atom. The two views share the chunk's seat budget (callers expand
    only after seed selection) and the chunk's locator keys, and are paired
    through the generated view's `context_of_evidence_id` pointing at the
    original atom. Never merge the two texts under one citation ID: each
    supplied fragment supports only its own text.
    """
    if not (hit.get("derived_text") and hit.get("derived_evidence_id")):
        return [hit]
    original = Hit({**hit, "derived_text": None, "derived_evidence_id": None})
    generated = Hit({**hit, "text": hit["derived_text"] or "",
                     "table_html": None,
                     "context_of_evidence_id": hit.get("evidence_id")})
    return [original, generated]


def expand_source_views(hits: list[Hit]) -> list[Hit]:
    """Expand retrieved chunks into separately citable original/generated views.

    Ordering is deterministic: the original view immediately precedes its own
    generated view. Seed selection happens before expansion, so the pair shares
    one chunk seat; char budgets count both views' actual text.
    """
    views: list[Hit] = []
    for hit in hits:
        views.extend(_hit_views(hit))
    return views


def _source_text(hit: Hit) -> str:
    if hit.get("derived_text") and hit.get("derived_evidence_id"):
        # A generated view carries only its own derived text: the original text
        # is a sibling view with the source atom's own citation ID.
        return hit["derived_text"]
    # 表格块优先给 HTML：拼出来的单元格文字已经丢了行列关系，
    # 而“第 3 行第 2 列是多少”恰恰是抽取最常问的
    return hit.get("table_html") or hit["text"]


def _format_sources(hits: list[Hit]) -> str:
    # Multi-record extraction reads one original block at a time.
    parts = []
    for i, hit in enumerate(hits, start=1):
        label = "，表格" if hit.get("block_type") == "table" else ""
        parts.append(f"[{i}] (第 {hit['page_idx'] + 1} 页{label}) "
                     f"{_source_text(hit)}")
    return "\n\n".join(parts)


async def _crop_and_verify(ctx: ExtractContext, hit: Hit) -> tuple[str | None, bool | None, bool]:
    """裁区域图 + 视觉核对。返回 `(对象键, 一致性, 是否真的核对过)`。

    一致性 True=一致 / False=对不上 / None=没测出来。
    **None 不许当成 False**：核对不了就是核对不了，把"不知道"说成"有问题"
    是另一种撒谎，而且会毁掉用户对这个标记的信任。

    **第三个返回值不能省。** `consistent is None` 有两种截然不同的来源：
      a. 核对预算用完了，压根没打模型 —— 正常，不该打任何降级标
      b. 打了模型但它不可达/抄写太短 —— **必须打 vision_unavailable**
    只返回 (key, None) 的话调用方分不出这两种，于是视觉模型整个挂掉时
    用户看到的是一整表 `verified: false` 且没有任何解释 ——
    与"预算用完"长得一模一样。这正是「降级必须可见」要防的事。
    """
    await ctx.check_access()
    view_key = (hit.get("derived_evidence_id") if hit.get("derived_text")
                and hit.get("derived_evidence_id") else hit.get("evidence_id")) or ""
    cache_key = (hit.get("parse_job_id") or "", hit.get("seq") or 0, view_key)
    if cache_key in ctx._crop_cache:
        return ctx._crop_cache[cache_key]

    key = None
    if ctx.job is not None:
        key = await get_or_create_crop(
            ctx.storage, job_id=ctx.job.id, source_key=await ctx.original_key(),
            mime=ctx.document.mime, page_idx=hit["page_idx"], bbox=hit.get("bbox"),
            page_size=hit.get("page_size"))

    consistent: bool | None = None
    attempted = False
    if key and ctx._verify_budget > 0:
        ctx._verify_budget -= 1
        ctx.usage["verifications"] += 1
        attempted = True
        raw = await ctx.storage.get(key)
        uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        await ctx.check_access()
        # The licence can end during the crop read; pixels go out only while it holds.
        await ctx.original_key()
        # Compare visible text belonging to this view, not another view's body
        # or HTML markup. HTML preserves table structure for extraction, but
        # its tag names are not text the vision model should transcribe.
        cited = (hit["derived_text"] if hit.get("derived_text")
                 and hit.get("derived_evidence_id") else hit["text"])
        consistent = await verify_parse_consistency(ctx.http, uri, cited)

    ctx._crop_cache[cache_key] = (key, consistent, attempted)
    return key, consistent, attempted


# ---------- 单字段 ----------

def _field_extra(spec: FieldSpec) -> str:
    bits = []
    if spec.format:
        bits.append(f"格式：{spec.format}")
    if spec.enum:
        bits.append(f"只能是这些值之一：{json.dumps(spec.enum, ensure_ascii=False)}")
    return ("\n" + "\n".join(bits)) if bits else ""


async def extract_field(ctx: ExtractContext, spec: FieldSpec, *,
                        related_fields: list[FieldSpec] | tuple = ()) -> dict:
    ctx.usage["fields"] += 1
    variants = [spec.description] if spec.description else []
    anchor = scope_phrase_query(spec)
    if anchor and anchor not in variants and anchor != spec.query:
        # 第三条有界 leg：本字段 description 里点名的位置短语，一字不改。
        # 整句 description 进向量后，内容词常把摘要/表格正文挤出候选池
        # （R8 LoRA-10000：摘要块掉到第 23/24 名）；光秃秃的位置词
        # 首名命中标题块，再经邻块扩展带出正文。最终候选数、版本与授权
        # 边界不变；位置词只是多一路召回，不作语义证明，不改名次规则。
        variants.append(anchor)
    hits, degraded = await _retrieve(
        ctx, spec.query, k=settings.extract_candidates,
        query_variants=variants or None)
    if not hits:
        # No candidates in this bounded search does not prove absence from the document.
        return field_result(status="not_found", degraded=degraded or "no_hits")

    views = expand_source_views(hits)
    messages = field_messages(spec, [{**hit, "text": _source_text(hit)} for hit in views],
                              related_fields=related_fields)
    response_format = field_response_format(spec, len(views))

    answer = None
    for _ in range(settings.extract_max_retries + 1):
        raw = await _chat(ctx, messages, temperature=0, response_format=response_format)
        if raw is None:
            return field_result(status="error", degraded="upstream_error")
        answer = parse_json_object(raw)
        if answer is not None and "found" in answer:
            break
        answer = None
    if answer is None:
        # 重试用尽仍不合规。**绝不当成"文档里没有"** —— 那会让系统故障伪装成事实
        return field_result(status="error", degraded="schema_violation")

    indices = resolve_field_support(answer, len(views))
    if indices is None:
        return field_result(status="error", degraded="schema_violation")
    if not indices:
        # An explicit bounded absence, not a document-wide proof.
        return field_result(status="not_found", degraded=degraded)

    try:
        value = coerce_value(answer.get("value"), spec)
    except CoerceError:
        return field_result(status="error", degraded="schema_violation")
    if value is None:
        return field_result(status="not_found", degraded=degraded)

    texts = [_source_text(hit) for hit in views]
    primary_index = indices[0]
    if spec.type in ("number", "integer") and not value_supported_by_source(
            value, texts[primary_index]):
        # 所引块原文里找不到这个数：模型声称“来自该块”却对不上 ——
        # 错的是模型输出，不是文档内容。只做机械必要条件，不宣称语义验证。
        # 上下文引用不能抢救一个虚假的 primary 数值出处：它们钉的是限定语，
        # 不是值本身。
        return field_result(status="error", degraded="schema_violation")
    selected = [views[index] for index in indices]
    crop_keys: list[str | None] = [None] * len(selected)
    consistents: list[bool | None] = [None] * len(selected)
    if ctx.verify:
        for position, selected_hit in enumerate(selected):
            crop_key, consistent, attempted = await _crop_and_verify(ctx, selected_hit)
            crop_keys[position], consistents[position] = crop_key, consistent
            if consistent is False:
                degraded = degraded or "parse_mismatch"
            elif crop_key is None and degraded is None:
                supported = ("pdf" in (ctx.document.mime or "").lower()
                             and ctx.document.object_key)
                degraded = "crop_unsupported" if not supported else "crop_failed"
            elif attempted and consistent is None and degraded is None:
                # 打了视觉模型但它没给出可用结果（不可达 / 抄写太短）。
                # **必须打标** —— 不打的话与"预算用完所以没核对"长得一模一样，
                # 视觉模型整个挂掉时用户看到的只是一整表 verified:false，没有任何解释
                degraded = "vision_unavailable"

    citations = [_citation(selected_hit, crop_key)
                 for selected_hit, crop_key in zip(selected, crop_keys)]
    # verified=true 要求每一条选中的出处都真正核对过且一致：
    # 任一条没测出来/预算不够/对不上，都不许整体标 true。
    verified = ctx.verify and all(c is True for c in consistents)
    return field_result(status="found", value=value, citations=citations,
                        verified=verified, degraded=degraded,
                        confidence=_confidence(citations))


# ---------- 多记录 ----------

async def extract_records(ctx: ExtractContext,
                          spec: SchemaSpec) -> tuple[list[dict], str | None]:
    """顶层 schema 是 array 时走这条：找候选块 -> 每块抽一组记录。

    派生块展开成原文/生成两个可引用视图：模型看到的每份正文都有自己的
    引用 ID。同一块的两个视图顺序相邻、不挤占块席位；每条记录在生产时
    选择自己读的那一份（`source`），落库只挂它选的那一份 —— 不把全部
    输入无条件挂为支持，也不在事后按值匹配改引用。缺少/幻觉的 `source`
    只丢掉该行，不连累同块其余行。表格被拆到两页时仍能看出哪一行来自
    哪一页、哪一份正文。
    """
    query = " ".join(f"{f.name} {f.description}" for f in spec.fields)
    hits, degraded = await _retrieve(ctx, query, k=settings.extract_max_record_blocks,
                                     prefer_types=("table",), include_context=False)
    if not hits:
        return [], degraded or "no_hits"

    field_lines = "\n".join(
        f"- {f.name}（{f.type}）：{f.description}{_field_extra(f)}" for f in spec.fields)
    records: list[dict] = []

    for hit in hits:
        views = expand_source_views([hit])
        prompt = _RECORDS_PROMPT.format(fields=field_lines, sources=_format_sources(views))
        raw = await _chat(ctx, [{"role": "system", "content": _SYSTEM},
                                {"role": "user", "content": prompt}])
        if raw is None:
            degraded = degraded or "upstream_error"
            continue
        parsed = parse_json_object(raw)
        if parsed is None or not isinstance(parsed.get("records"), list):
            degraded = degraded or "schema_violation"
            continue

        crop_key = None
        if ctx.verify:
            await ctx.check_access()
            if ctx.job is not None:
                crop_key = await get_or_create_crop(
                    ctx.storage, job_id=ctx.job.id, source_key=await ctx.original_key(),
                    mime=ctx.document.mime, page_idx=hit["page_idx"], bbox=hit.get("bbox"),
                    page_size=hit.get("page_size"))
            if crop_key is None and degraded is None:
                supported = ("pdf" in (ctx.document.mime or "").lower()
                             and ctx.document.object_key)
                degraded = "crop_unsupported" if not supported else "crop_failed"
        citations = [_citation(view, crop_key) for view in views]

        for row in parsed["records"]:
            if not isinstance(row, dict):
                continue
            resolved = resolve_record_source(row, len(views))
            if resolved is None:
                degraded = degraded or "schema_violation"
                continue
            index, values = resolved
            citation = citations[index]
            row_verified = False
            if ctx.verify:
                _, row_consistent, row_attempted = await _crop_and_verify(
                    ctx, views[index])
                row_verified = bool(row_consistent)
                if row_consistent is False:
                    degraded = degraded or "parse_mismatch"
                elif row_attempted and row_consistent is None:
                    degraded = degraded or "vision_unavailable"
            fields = {f.name: _record_field(values.get(f.name), f, citation,
                                            verified=row_verified)
                      for f in spec.fields}
            # 整行都是 not_found 就丢掉：那多半是模型把表头或空行当成了记录
            if any(v["status"] == "found" for v in fields.values()):
                records.append({"fields": fields})
    return records, degraded


def _record_field(raw: object, spec: FieldSpec, citation: dict, *, verified: bool) -> dict:
    if raw is None:
        return field_result(status="not_found")
    try:
        value = coerce_value(raw, spec)
    except CoerceError:
        return field_result(status="error", degraded="schema_violation")
    if value is None:
        return field_result(status="not_found")
    return field_result(status="found", value=value, citations=[citation],
                        verified=verified, confidence=_confidence([citation]))


# ---------- 入口 ----------

# 越靠前越值得让用户先看见。no_hits 排最后：单个字段没检索到很常见，
# 把它冒泡成整体降级会淹掉真正的系统问题
# keyword_unavailable 紧跟 embedding_unavailable：关键词腿瘸了比整条语义路
# 没跑轻，但比 vision/rerank 这类后处理缺席重（字面量来自契约 enums.yaml，
# 由 contracts 生成物约束取值，见 ddp_core/search.py::SearchHits）
_DEGRADED_PRIORITY = ("upstream_error", "schema_violation", "parse_mismatch",
                      "embedding_unavailable", "keyword_unavailable",
                      "vision_unavailable", "rerank_unavailable",
                      "crop_failed", "crop_unsupported", "no_hits")


def rollup_degraded(items) -> str | None:
    present = {i.get("degraded") for i in items if i.get("degraded")}
    for value in _DEGRADED_PRIORITY:
        if value in present:
            return value
    return None


async def run(ctx: ExtractContext, spec: SchemaSpec) -> ExtractOutcome:
    """跑完一份文档的抽取。"""
    if spec.kind == "array":
        records, degraded = await extract_records(ctx, spec)
        return ExtractOutcome(records=records, status="ok" if records else "partial",
                              degraded=degraded, usage=ctx.usage)

    fields = spec.fields[:settings.extract_max_fields]
    semaphore = asyncio.Semaphore(settings.extract_concurrency)

    async def one(field: FieldSpec) -> tuple[str, dict]:
        async with semaphore:
            try:
                return field.name, await extract_field(ctx, field, related_fields=fields)
            except APIError:
                raise  # permission revocation aborts this task; it is not an empty field
            except Exception:   # noqa: BLE001
                # **一个字段的意外不能丢掉整批已经抽好的字段。**
                # gather 默认在第一个异常处抛出，其余协程的结果直接作废 ——
                # 于是"一个字段 error、其余照常"被升级成"整份文档白跑"，
                # 三态设计在这里就白设计了。真实触发点：换 embedding 模型后
                # pgvector 报 "different vector dimensions"，检索层没兜住
                return field.name, field_result(status="error",
                                                degraded="upstream_error")

    tasks = [asyncio.create_task(one(f)) for f in fields]
    try:
        result = dict(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return ExtractOutcome(fields=result, status=overall_status(result, spec),
                          degraded=rollup_degraded(result.values()), usage=ctx.usage)


def extraction_model_meta() -> dict:
    """这一轮抽取的"配置指纹"，随 ExtractionRun 落库。

    与 Message.model_meta 同一个作用：不记下用了哪个模型、哪套检索参数，
    换配置之后历史结果就无法分组对比 —— 而那是判断"新配置有没有变好"的唯一依据。
    """
    from ddp_core.tokenize import backend as tokenize_backend

    return {
        "chat_model": settings.chat_model,
        "chat_endpoint": settings.chat_endpoint,
        "embedding_model": settings.embedding_model,
        "embedding_endpoint": settings.embeddings_endpoint,
        "tokenizer": tokenize_backend(),
        "extraction": {
            "selection_protocol": FIELD_SELECTION_PROTOCOL,
            "records_selection_protocol": RECORDS_SELECTION_PROTOCOL,
            "seed_selection": "balanced-hybrid-rerank/1",
            "context_selection": "hybrid-diverse-regions-then-neighbors/2",
            "candidates": settings.extract_candidates,
            "max_retries": settings.extract_max_retries,
            "min_similarity": settings.qa_min_similarity,
            "verify": settings.extract_verify,
            "verify_fields": settings.extract_verify_fields,
            "rerank_enabled": settings.rerank_enabled,
            "rerank_model": settings.rerank_model,
        },
    }
