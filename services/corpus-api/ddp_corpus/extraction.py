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
    CoerceError, FieldSpec, SchemaSpec, coerce_value, field_result, overall_status,
    parse_json_object,
)
from ddp_corpus.models import Document, ParseJob
from ddp_corpus.qa import verify_parse_consistency
from ddp_core.search import Hit, SearchIndex, search_query
from ddp_corpus.storage import Storage
from ddp_corpus.upstream import chat_request, embed_batched
from ddp_core.rerank import rerank_hits

_SYSTEM = (
    "你是文档信息抽取器。只依据【资料】抽取，资料里没有的信息必须如实报告没有，"
    "**绝不猜测、绝不用常识补充**。只输出 JSON，不要任何解释文字。"
)

_FIELD_PROMPT = """【字段】
名称：{name}
含义：{description}
类型：{type}{extra}

【资料】
{sources}

从【资料】里找出该字段的值。只输出一个 JSON 对象：
{{"found": true 或 false, "value": 值或 null, "source": 用到的资料编号（整数）}}

规则：
- 资料里没有这个字段的信息 -> {{"found": false, "value": null, "source": null}}
- value 只放字段本身的值，不要带单位说明、不要带前后文
- **不要为了填满而编造**。找不到就是找不到。"""

_RECORDS_PROMPT = """【记录字段】
{fields}

【资料】
{sources}

上面的资料可能包含**多条**同构记录（例如表格的多行）。把它们抽成 JSON：
{{"records": [{{"字段名": 值或 null, ...}}, ...]}}

规则：
- 资料里一条记录都没有 -> {{"records": []}}
- 某条记录缺某个字段 -> 该字段填 null，不要跳过整条记录
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
        # 而"甲方名称"和"甲方地址"命中同一段是常态
        self._crop_cache: dict[tuple[str, int],
                              tuple[str | None, bool | None, bool]] = {}

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

async def _chat(ctx: ExtractContext, prompt: str) -> str | None:
    """调 chat 端点。不可达/非 200 返回 None -> 字段判 error。"""
    await ctx.check_access()
    ctx.usage["chat_calls"] += 1
    try:
        request = chat_request(ctx.http, [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ], stream=False)
        response = await ctx.http.send(request)
        if response.status_code != 200:
            return None
        return response.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None


# ---------- 检索 ----------

async def _retrieve(ctx: ExtractContext, query: str, *, k: int,
                    prefer_types: tuple[str, ...] = ()) -> tuple[list[Hit], str | None]:
    await ctx.check_access()
    ctx.usage["retrievals"] += 1
    candidates = max(settings.rerank_candidates if settings.rerank_enabled else k * 3, k)
    # 与问答 /api/search 同一条共享检索：search_query 负责分面拆查 + 批量向量化，
    # 向量化挂了它自己退回关键词路并打标（铁律 3 那条绝不能拿零向量顶上，
    # 在它内部已经处理，见 ddp_core/search.py::_search_query）
    hits, degraded = await search_query(
        ctx.session, ctx.index, embed=lambda texts: embed_batched(ctx.http, texts),
        query=query, document_id=ctx.document.id,
        authorized_parse_job_ids=ctx.authorized_parse_job_ids,
        limit=candidates, candidates=candidates,
        min_similarity=settings.qa_min_similarity)
    if not hits:
        return [], degraded or "no_hits"

    await ctx.check_access()
    hits, rerank_degraded = await rerank_hits(ctx.http, query, hits, top_k=max(k, 1),
                                              cfg=rerank_config())
    degraded = degraded or rerank_degraded
    if prefer_types:
        # 稳定排序：表格块提前，同类保持原名次
        hits = sorted(hits, key=lambda h: 0 if h.get("block_type") in prefer_types else 1)
    return hits[:k], degraded


# ---------- 出处 ----------

def _snippet(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _citation(hit: Hit, crop_key: str | None) -> dict:
    """一条出处。形状与问答平面完全一致（前端组件因此可以直接复用）。

    稳定定位键是 `(parse_job_id, seq)` —— chunk_id 每次 reindex 都会重铸，
    只存它等于历史抽取结果一次重建就永久失去原文依据（P0 那条教训的抽取版）。

    视觉块的引用与 qa.attach_crops 同一条规则：命中带派生理解时引用派生
    evidence（source_type="generated"），沿 derived_from 回到源原子的 bbox
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


def _format_sources(hits: list[Hit]) -> str:
    parts = []
    for i, hit in enumerate(hits, start=1):
        label = "，表格" if hit.get("block_type") == "table" else ""
        generated = bool(hit.get("derived_text") and hit.get("derived_evidence_id"))
        if generated:
            # 派生理解与 qa.build_messages 同一形状：模型看到的是引用指向的那份文本
            body = (f"[生成理解，原子证据 {hit.get('evidence_id') or '未编号'}]\n"
                    f"{hit['derived_text']}\n[原文/OCR]\n{hit['text']}")
        else:
            # 表格块优先给 HTML：拼出来的单元格文字已经丢了行列关系，
            # 而"第 3 行第 2 列是多少"恰恰是抽取最常问的
            body = hit.get("table_html") or hit["text"]
        parts.append(f"[{i}] (第 {hit['page_idx'] + 1} 页{label}) {body}")
    return "\n\n".join(parts)


async def _crop_and_verify(ctx: ExtractContext, hit: Hit) -> tuple[str | None, bool | None]:
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
    cache_key = (hit.get("parse_job_id") or "", hit.get("seq") or 0)
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
        # 视觉块核对的是派生理解（引用指向的那份文本），不是 OCR 原文 ——
        # 拿原文去对图等于拿另一份文本验，误报 parse_mismatch
        cited = hit.get("derived_text") if (
            hit.get("derived_text") and hit.get("derived_evidence_id")) else hit["text"]
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


def _pick_source(hits: list[Hit], source: object) -> Hit | None:
    """模型说值来自第几条资料就用第几条。

    **这是字段级出处的精度所在**：检索给了 4 个块，值只来自其中一个，
    把 4 个都挂上去等于告诉用户"在这四块里自己找"，出处就退化回 chunk 级了。
    编号越界/没给/指到不存在的资料时返回 None —— 调用方按幻觉引用处理
    （not_found + schema_violation），**绝不退回 top-1**：模型凭空指了一条
    不存在的资料，那条值已经不可信，硬挂一条出处等于给幻觉贴"已验证"。
    """
    if not hits:
        return None
    try:
        index = int(source) - 1
    except (TypeError, ValueError):
        return None
    return hits[index] if 0 <= index < len(hits) else None


async def extract_field(ctx: ExtractContext, spec: FieldSpec) -> dict:
    ctx.usage["fields"] += 1
    hits, degraded = await _retrieve(ctx, spec.query, k=settings.extract_candidates)
    if not hits:
        # 检索零命中：文档里大概率确实没有。如实标 no_hits ——
        # 它是信息（"我们什么都没看到"），不是掩饰
        return field_result(status="not_found", degraded=degraded or "no_hits")

    prompt = _FIELD_PROMPT.format(
        name=spec.name, description=spec.description, type=spec.type,
        extra=_field_extra(spec), sources=_format_sources(hits))

    answer = None
    for _ in range(settings.extract_max_retries + 1):
        raw = await _chat(ctx, prompt)
        if raw is None:
            return field_result(status="error", degraded="upstream_error")
        answer = parse_json_object(raw)
        if answer is not None and "found" in answer:
            break
        answer = None
    if answer is None:
        # 重试用尽仍不合规。**绝不当成"文档里没有"** —— 那会让系统故障伪装成事实
        return field_result(status="error", degraded="schema_violation")

    if not answer.get("found") or answer.get("value") is None:
        # 模型看过资料、明确说没有 —— 最强的一种 not_found，不打降级标
        return field_result(status="not_found", degraded=degraded)

    try:
        value = coerce_value(answer.get("value"), spec)
    except CoerceError:
        return field_result(status="error", degraded="schema_violation")
    if value is None:
        return field_result(status="not_found", degraded=degraded)

    hit = _pick_source(hits, answer.get("source"))
    if hit is None:
        # 模型指了一条不存在的资料（越界编号/非数字/没给）：值声称来自一段
        # 我们根本没给它看过的文本 —— 这条值已经不可信，不能挂 top-1 出处
        # 假装它有依据。判 not_found 而不是 error：文档里有没有这个字段，
        # 此刻已经无从判断，"没抽到"是唯一诚实的回答；而 degraded 用
        # schema_violation（除非已有更严重的标）：错的是模型输出的格式契约
        # （source 必须是 1..N 的整数），不是文档内容
        return field_result(status="not_found", degraded=degraded or "schema_violation")
    crop_key, consistent, attempted = (None, None, False)
    if ctx.verify:
        crop_key, consistent, attempted = await _crop_and_verify(ctx, hit)
        if consistent is False:
            degraded = degraded or "parse_mismatch"
        elif crop_key is None and degraded is None:
            supported = "pdf" in (ctx.document.mime or "").lower() and ctx.document.object_key
            degraded = "crop_unsupported" if not supported else "crop_failed"
        elif attempted and consistent is None and degraded is None:
            # 打了视觉模型但它没给出可用结果（不可达 / 抄写太短）。
            # **必须打标** —— 不打的话与"预算用完所以没核对"长得一模一样，
            # 视觉模型整个挂掉时用户看到的只是一整表 verified:false，没有任何解释
            degraded = "vision_unavailable"

    citations = [_citation(hit, crop_key)]
    return field_result(status="found", value=value, citations=citations,
                        verified=bool(consistent), degraded=degraded,
                        confidence=_confidence(citations))


# ---------- 多记录 ----------

async def extract_records(ctx: ExtractContext,
                          spec: SchemaSpec) -> tuple[list[dict], str | None]:
    """顶层 schema 是 array 时走这条：找候选块 -> 每块抽一组记录。

    记录的出处是**它所在的那个块**，不是整份文档 —— 表格被拆到两页时
    也能看出哪一行来自哪一页。
    """
    query = " ".join(f"{f.name} {f.description}" for f in spec.fields)
    hits, degraded = await _retrieve(ctx, query, k=settings.extract_max_record_blocks,
                                     prefer_types=("table",))
    if not hits:
        return [], degraded or "no_hits"

    field_lines = "\n".join(
        f"- {f.name}（{f.type}）：{f.description}{_field_extra(f)}" for f in spec.fields)
    records: list[dict] = []

    for hit in hits:
        raw = await _chat(ctx, _RECORDS_PROMPT.format(
            fields=field_lines, sources=_format_sources([hit])))
        if raw is None:
            degraded = degraded or "upstream_error"
            continue
        parsed = parse_json_object(raw)
        if parsed is None or not isinstance(parsed.get("records"), list):
            degraded = degraded or "schema_violation"
            continue

        crop_key, consistent, attempted = (None, None, False)
        if ctx.verify:
            crop_key, consistent, attempted = await _crop_and_verify(ctx, hit)
            if consistent is False:
                degraded = degraded or "parse_mismatch"
            elif attempted and consistent is None:
                degraded = degraded or "vision_unavailable"
            elif crop_key is None and degraded is None:
                supported = ("pdf" in (ctx.document.mime or "").lower()
                             and ctx.document.object_key)
                degraded = "crop_unsupported" if not supported else "crop_failed"
        citation = _citation(hit, crop_key)

        for row in parsed["records"]:
            if not isinstance(row, dict):
                continue
            fields = {f.name: _record_field(row.get(f.name), f, citation,
                                            verified=bool(consistent))
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
                return field.name, await extract_field(ctx, field)
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
            "candidates": settings.extract_candidates,
            "max_retries": settings.extract_max_retries,
            "min_similarity": settings.qa_min_similarity,
            "verify": settings.extract_verify,
            "verify_fields": settings.extract_verify_fields,
            "rerank_enabled": settings.rerank_enabled,
            "rerank_model": settings.rerank_model,
        },
    }
