"""抽取平面的编排：schema -> 逐字段定位 -> 抽值 -> 出处 -> 核对。

**这一层最重要的不是抽得准，是抽不到时如实说抽不到。**
三态（found / not_found / error）必须分得干净：
  - not_found = 本次检索与抽取未找到值，不证明原文不存在
  - error     = 我们没能可靠地抽出来（类型转不动、模型输出不合规、上游挂了）
合成一个的话，"本次未找到"和"我们的检索挂了"长得一模一样，
而空值看起来像结论 —— 这是抽取里最危险的输出。

**实现约束（写进 openapi.yaml，不是可选的工程口味）**：
字段值必须走「先检索定位到块 -> 再从块里抽值」。不许把全文塞进 prompt 让模型直接吐 JSON：
那样出处只能靠事后字符串匹配倒推，会产出带着"已验证"标记的假出处，
也违反 plan.md 那条贯穿性准则（信息进了潜空间就指不回 bbox）。

格式契约见 docs/extract-format.md。
"""
import asyncio
import base64
import json

import httpx

from ddp_gateway.config import (
    adapt_structured_output, chat_request_defaults, request_model_id, settings, upstream_auth_headers,
)
from ddp_core import extract_format as fmt
from ddp_core import crops
from ddp_core.extract_format import CoerceError, FieldSpec, SchemaSpec, coerce_value
from ddp_core.verification import TRANSCRIBE_PROMPT, transcript_agrees
from ddp_gateway.services.retrieval import retrieve, retrieve_with_variants

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



class ExtractContext:
    def __init__(self, *, store, http: httpx.AsyncClient, registry, doc_hash: str,
                 file_url: str = "", verify: bool | None = None,
                 corpus: list[dict] | None = None, on_progress=None):
        self.store = store
        self.http = http
        self.registry = registry
        self.doc_hash = doc_hash
        self.file_url = file_url
        self.verify = settings.extract_verify if verify is None else verify
        # 关键词路的语料。无 embedding 部署下由调用方从版面直接派生喂进来 ——
        # 没有它抽取平面在无 GPU 环境里是空的（见 retrieval.retrieve 的说明）
        self.corpus = corpus
        # 进度回调 (已完成字段数, 总字段数)。抽取可能跑几分钟，
        # 没有它调用方只能看着一个 0 干等 —— 与"降级必须可见"同一个道理：
        # 不知道发生了什么，和真的什么都没发生，用户是分不出来的
        self.on_progress = on_progress
        self.usage = {"fields": 0, "retrievals": 0, "chat_calls": 0}
        self._pdf: bytes | None = None
        self._pdf_tried = False


# ---------- 上游调用 ----------

# 能力词：这个条目**不会遵循指令**，只会干它专精的那件事。
#
# 为什么需要它：抽取平面「复用 VQA 平面的模型」这个设计，在 VQA 位上是
# **OCR 专用模型**时会塌掉。DeepSeek-OCR 系就是典型 —— 给它一张图，它把字抄出来；
# 给它一段"请按 schema 抽取并输出 JSON"的指令，它还是继续抄字。
# 抄出来的东西解析不出 JSON，重试用尽后打 schema_violation；
# 更糟的情况是它恰好吐出一个能解析、但 found=false 的东西 ——
# **那就变成了"文档里没有"，一个看起来像结论的空值**，正是本模块开头说的最危险输出。
#
# 所以：OCR 专用模型在注册表里写 `capabilities: [vision, no_instruct]`，
# 抽值路径挑模型时跳过它们；一个都挑不到就如实报 no_instruct_model，
# 而不是拿 OCR 模型去硬抽。视觉核对（原样抄写）**照常用它们** —— 那正是它们的本行。
NO_INSTRUCT = "no_instruct"

# 能力词：这个条目**看得见图**。
#
# 它是上面那条的镜像，而且是同一个坑的另一半。加了 no_instruct 之后，
# `vqa_models` 段里第一次出现了**纯文本模型**（抽取平面需要一个会遵循指令的模型，
# 而它不必会看图）。于是反向的错配随之诞生：视觉核对若挑中纯文本条目，
# 模型根本收不到图，只会对着那句指令自说自话 —— 抄写比对必然对不上，
# 于是**每一条好出处都被打成 parse_mismatch**。
# 这与 transcribe_prompt 修的那个 bug 后果完全一样，只是方向相反。
#
# 段名会把没写 capabilities 的 vqa 条目缺省补成 [vision]（config.SECTION_CAPABILITIES），
# 所以老注册表一字不改照跑；被这条挡住的只可能是**显式声明了自己不会看图**的条目。
VISION = "vision"


def _pick_chat(ctx: ExtractContext, *, instruct: bool):
    """挑一个能干这活的 vqa 条目，挑不到返回 None。

    instruct=True  抽值：必须会遵循指令、能按要求吐 JSON，**不需要看图**
    instruct=False 视觉核对：必须看得见图，OCR 专用模型正合适

    两条路各自按能力词过滤，**不能只筛一边**：`vqa_models` 段如今同时住着
    OCR 专用模型（会看图、不听指令）与纯文本指令模型（听指令、看不见图），
    少筛哪一边，哪一边就会挑中干不了这活的那个 —— 而两种错配都**不报错**，
    只是让核对变成噪声（挑错视觉模型）或让抽值变成假的 not_found（挑错指令模型）。
    """
    section = ctx.registry.vqa_models
    if not section:
        return None
    want, unwanted = (None, NO_INSTRUCT) if instruct else (VISION, None)
    usable = {
        name: entry for name, entry in section.items()
        if (want is None or want in (entry.capabilities or []))
        and (unwanted is None or unwanted not in (entry.capabilities or []))
    }
    if not usable:
        return None
    # default_of 会优先取标了 default 的那个；缺省项不可用时自动落到第一个可用项
    return ctx.registry.default_of(usable)


def instruct_available(ctx: ExtractContext) -> bool:
    return _pick_chat(ctx, instruct=True) is not None


def _transcribe_prompt(ctx: ExtractContext) -> str:
    """让模型"把这块图上的字抄出来"，**用它听得懂的话问**。

    2026-08-25 在真机上标定阈值时抓到的：拿通用中文抄写指令去问 DeepSeek-OCR-2，
    它不抄写，而是**回应那句指令**——

        原文 "PURCHASE AGREEMENT" -> 抄写 "例如，如果问题涉及"购买协议"，则写"购买协议"。"

    这和 no_instruct 是同一类问题：OCR 专用模型只认它自己那两个官方 prompt。
    后果比抽值那边更阴险 —— 抄写对不上会被判成 `parse_mismatch`
    （"这块的解析结果可疑"），于是**每一条出处都被打上可疑标记**，
    而解析本身其实是好的。核对功能不是失灵，是变成了纯噪声。

    所以：注册表条目可以用 `options.transcribe_prompt` 声明"该怎么问我"。
    OCR 专用模型填它自己的原生 OCR prompt（DeepSeek-OCR 系是 `Free OCR.`）；
    通用视觉模型不用填，走缺省那句中文指令。
    """
    picked = _pick_chat(ctx, instruct=False)
    if picked is None:
        return TRANSCRIBE_PROMPT
    _, entry = picked
    return str((entry.options or {}).get("transcribe_prompt") or TRANSCRIBE_PROMPT)


async def _chat(ctx: ExtractContext, messages: list[dict], *,
                instruct: bool = True, temperature: float | None = None,
                response_format: dict | None = None) -> str | None:
    """调 VQA 平面（OpenAI 协议）。不可达/非 200/无密钥返回 None -> 字段判 error。"""
    picked = _pick_chat(ctx, instruct=instruct)
    if picked is None:
        return None
    try:
        name, entry = picked
        headers = upstream_auth_headers(entry)
        ctx.usage["chat_calls"] += 1
        resp = await ctx.http.post(
            f"{entry.endpoint}/v1/chat/completions",
            json=adapt_structured_output({
                **chat_request_defaults(entry.options),
                "model": request_model_id(name, entry),
                "messages": messages, "stream": False,
                **({"temperature": temperature} if temperature is not None else {}),
                **({"response_format": response_format} if response_format is not None else {}),
            }, entry.options),
            headers=headers,
        )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None


# ---------- 出处 ----------

def _citation(ctx: ExtractContext, hit: dict, crop_uri: str | None = None) -> dict:
    """一条出处。形状与问答平面完全一致，前端 CitationChip 不用改就能复用。

    稳定定位键是 (doc_hash, seq)：chunk_id 在 service 侧压根不存在，
    而 Redis 里的分块随时会因 24h TTL 消失 —— 只有这两个值能一直指回原文。
    service 侧命中没有派生 evidence 路：`text` 与 `table_html` 即本块自有正文，
    引用不跨块拼凑 —— 与产品层“每份正文只由其自身引用”同一条规则。
    """
    return {
        "chunk_id": None,
        "parse_job_id": None,
        "doc_hash": ctx.doc_hash,
        "seq": hit.get("seq"),
        "page_idx": hit.get("page_idx"),
        "bbox": hit.get("bbox"),
        "page_size": hit.get("page_size"),
        "snippet": _snippet(hit.get("text", "")),
        "similarity": hit.get("similarity"),
        "block_type": hit.get("block_type"),
        "crop_url": crop_uri,
    }


def _snippet(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _confidence(citations: list[dict]) -> dict:
    """这个字段的依据有多可信 —— 交给用户判断，而不是替他判断。

    只看 top-1 的余弦相似度：它有校准过的量纲。关键词路量不到相似度时是
    unknown，**不许装作 high**（沿用问答平面 retrieval_confidence 的语义）。
    """
    sims = [c["similarity"] for c in citations if c.get("similarity") is not None]
    if not sims:
        return {"level": "unknown", "top_similarity": None,
                "warn_below": settings.extract_low_similarity}
    top = max(sims)
    return {"level": "high" if top >= settings.extract_low_similarity else "low",
            "top_similarity": top, "warn_below": settings.extract_low_similarity}


def _format_sources(hits: list[dict]) -> str:
    parts = []
    for i, hit in enumerate(hits, start=1):
        label = "，表格" if hit.get("block_type") == "table" else ""
        # **表格块优先给 HTML**：拼出来的单元格文字已经丢了行列关系，
        # 而“第 3 行第 2 列是多少”恰恰是抽取最常问的。
        # 不给 HTML 的话，块类型进契约这件事在 service 侧就没兑现
        parts.append(f"[{i}] (第 {hit.get('page_idx', 0) + 1} 页{label}) {_source_text(hit)}")
    return "\n\n".join(parts)


# ---------- 出处核对（沿用 A4 的做法） ----------

async def _load_pdf(ctx: ExtractContext) -> bytes | None:
    # 函数内导入：本文件只允许动这个函数，顶层 import 不碰。
    # 上限常量只有 engines.BORNDIGITAL_MAX_BYTES 一份，不另起。
    from ddp_core.fetch_policy import (
        FetchNotAllowedError,
        FileTooLargeError,
        check_destination,
        fetch_bytes_capped,
    )
    from ddp_gateway.services.engines import BORNDIGITAL_MAX_BYTES
    if ctx._pdf_tried:
        return ctx._pdf
    ctx._pdf_tried = True
    if not ctx.file_url:
        return None
    try:
        # SSRF 封锁 + 200MB 上限：与 engines._download 同一条抓取循环。
        from ddp_gateway.config import settings as _settings
        cfg = _settings.fetch_policy_config()
        check_destination(ctx.file_url, cfg)
        data = await fetch_bytes_capped(ctx.http, ctx.file_url, cfg, BORNDIGITAL_MAX_BYTES)
        ctx._pdf = data if data.lstrip()[:5].startswith(b"%PDF") else None
    except (FetchNotAllowedError, FileTooLargeError):
        # 目的地拒绝 / 超上限：核对降级为"没测出来"（下游视 None 为
        # vision_unavailable）。URL/token 绝不进错误或日志。
        ctx._pdf = None
    except Exception:
        ctx._pdf = None
    return ctx._pdf


async def _verify_citation(ctx: ExtractContext, hit: dict) -> tuple[str | None, bool | None]:
    """裁区域图 + 让视觉模型原样抄一遍，与块文本比对。

    返回 (crop data URI, 一致性)。一致性 True=一致 / False=对不上 / None=没测出来。
    **None 不许当成 False**：核对不了就是核对不了，把"不知道"说成"有问题"
    会毁掉用户对这个标记的信任。
    """
    pdf = await _load_pdf(ctx)
    if pdf is None or not hit.get("bbox"):
        return None, None
    png = await asyncio.to_thread(crops.render_crop, pdf, hit.get("page_idx", 0),
                                  hit["bbox"], hit.get("page_size"))
    if png is None:
        return None, None
    uri = "data:image/png;base64," + base64.b64encode(png).decode()

    # instruct=False：原样抄写是 OCR 模型的本行，别把它们排除在外。
    # 但**得用它听得懂的话去问** —— 见 _transcribe_prompt。
    transcript = await _chat(ctx, [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "text", "text": _transcribe_prompt(ctx)},
    ]}], instruct=False)
    if transcript is None:
        return uri, None
    return uri, transcript_agrees(
        transcript, hit.get("text", ""), threshold=settings.extract_mismatch_threshold)


# ---------- 单字段抽取 ----------

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
    ctx.usage["retrievals"] += 1
    variants = [spec.description] if spec.description else []
    anchor = fmt.scope_phrase_query(spec)
    if anchor and anchor not in variants and anchor != spec.query:
        # 与产品层同一条第三 leg：本字段 description 里点名的位置短语，一字不改。
        # 只是多一路有界召回，不作语义证明，不改名次规则与最终候选数。
        variants.append(anchor)
    found = await retrieve_with_variants(
        ctx.store, ctx.http, ctx.registry, doc_hash=ctx.doc_hash,
        query=spec.query, query_variants=variants or None,
        k=settings.extract_candidates, corpus=ctx.corpus)
    if not found.hits:
        # No candidates in this bounded search does not prove absence from the document.
        return fmt.field_result(status="not_found",
                                degraded=found.degraded or "no_hits")

    if not instruct_available(ctx):
        # 注册表里只有 OCR 专用模型（或压根没有 vqa 条目）。**如实报错，不硬抽** ——
        # 拿 OCR 模型抽值最好的结果是 schema_violation，最坏的结果是一个假的 not_found
        return fmt.field_result(status="error", degraded="no_instruct_model")

    messages = fmt.field_messages(
        spec, [{**hit, "text": _source_text(hit)} for hit in found.hits],
        related_fields=related_fields)
    response_format = fmt.field_response_format(spec, len(found.hits))

    answer = None
    for _ in range(settings.extract_max_retries + 1):
        raw = await _chat(ctx, messages, temperature=0, response_format=response_format)
        if raw is None:
            return fmt.field_result(status="error", degraded="upstream_error")
        answer = fmt.parse_json_object(raw)
        if answer is not None and "found" in answer:
            break
        answer = None
    if answer is None:
        # 重试用尽仍不合规。**绝不当成"文档里没有"** —— 那会让系统故障伪装成事实
        return fmt.field_result(status="error", degraded="schema_violation")

    indices = fmt.resolve_field_support(answer, len(found.hits))
    if indices is None:
        return fmt.field_result(status="error", degraded="schema_violation")
    if not indices:
        # An explicit bounded absence, not a document-wide proof.
        return fmt.field_result(status="not_found", degraded=found.degraded)

    try:
        value = coerce_value(answer.get("value"), spec)
    except CoerceError:
        return fmt.field_result(status="error", degraded="schema_violation")
    if value is None:
        return fmt.field_result(status="not_found", degraded=found.degraded)

    texts = [_source_text(hit) for hit in found.hits]
    primary_index = indices[0]
    if spec.type in ("number", "integer") and not fmt.value_supported_by_source(
            value, texts[primary_index]):
        # 所引块原文里找不到这个数：模型声称“来自该块”却对不上 ——
        # 错的是模型输出，不是文档内容。只做机械必要条件，不宣称语义验证。
        # 上下文引用不能抢救一个虚假的 primary 数值出处。
        return fmt.field_result(status="error", degraded="schema_violation")
    selected = [found.hits[index] for index in indices]
    crop_uris: list[str | None] = [None] * len(selected)
    consistents: list[bool | None] = [None] * len(selected)
    degraded = found.degraded
    if ctx.verify:
        for position, selected_hit in enumerate(selected):
            crop_uri, consistent = await _verify_citation(ctx, selected_hit)
            crop_uris[position], consistents[position] = crop_uri, consistent
            if consistent is False:
                degraded = degraded or "parse_mismatch"
            elif consistent is None and degraded is None:
                degraded = ("crop_unsupported" if crop_uri is None
                            else "vision_unavailable")

    citations = [_citation(ctx, selected_hit, crop_uri)
                 for selected_hit, crop_uri in zip(selected, crop_uris)]
    # verified=true 要求每一条选中的出处都真正核对过且一致：
    # 任一条没测出来/对不上，都不许整体标 true。
    verified = ctx.verify and all(c is True for c in consistents)
    return fmt.field_result(status="found", value=value, citations=citations,
                            verified=verified, degraded=degraded,
                            confidence=_confidence(citations))




def _source_text(hit: dict) -> str:
    # 数值核验只认配置给模型的同一份资料文本（含表格 HTML 路径），
    # 不另取别的字段拼凑 —— 否则“所引块里有没有这个数”判的就不是模型看到的那份。
    text = hit.get("table_html") or hit.get("text", "")
    return text if isinstance(text, str) else ""




# ---------- 多记录（表格）抽取 ----------

async def extract_records(ctx: ExtractContext, spec: SchemaSpec) -> tuple[list[dict], str | None]:
    """顶层 schema 是 array 时走这条：找候选块 -> 每块抽一组记录。

    每条记录在生产时选择自己读的那份资料（`source`），落库只挂它选的那一份 ——
    不把全部输入无条件挂为支持，也不在事后按值匹配改引用。缺少/幻觉的 `source`
    只丢掉该行，不连累同块其余行。记录的出处是**它所在的那个块**，不是整份文档。
    这样每一行都指得回原文，表格被拆成两页时也能看出哪一行来自哪一页。
    """
    query = " ".join(f"{f.name} {f.description}" for f in spec.fields)
    ctx.usage["retrievals"] += 1
    found = await retrieve(ctx.store, ctx.http, ctx.registry, doc_hash=ctx.doc_hash,
                           query=query, k=settings.extract_max_record_blocks,
                           corpus=ctx.corpus,
                           # 表格块优先：多记录抽取的典型载体就是表格
                           prefer_types=("table",))
    if not found.hits:
        return [], found.degraded or "no_hits"
    if not instruct_available(ctx):
        # 与单字段路径同一条理由：宁可空手报 no_instruct_model，
        # 也不拿 OCR 专用模型去抽记录（那会抽出一堆看似合理的空记录）
        return [], "no_instruct_model"

    field_lines = "\n".join(
        f"- {f.name}（{f.type}）：{f.description}{_field_extra(f)}" for f in spec.fields)
    records: list[dict] = []
    degraded = found.degraded

    for hit in found.hits:
        prompt = _RECORDS_PROMPT.format(fields=field_lines, sources=_format_sources([hit]))
        raw = await _chat(ctx, [{"role": "system", "content": _SYSTEM},
                                {"role": "user", "content": prompt}])
        if raw is None:
            degraded = degraded or "upstream_error"
            continue
        parsed = fmt.parse_json_object(raw)
        if parsed is None or not isinstance(parsed.get("records"), list):
            degraded = degraded or "schema_violation"
            continue

        crop_uri, consistent = (None, None)
        if ctx.verify:
            crop_uri, consistent = await _verify_citation(ctx, hit)
            if consistent is False:
                degraded = degraded or "parse_mismatch"
        citation = _citation(ctx, hit, crop_uri)

        for row in parsed["records"]:
            if not isinstance(row, dict):
                continue
            resolved = fmt.resolve_record_source(row, 1)
            if resolved is None:
                degraded = degraded or "schema_violation"
                continue
            _, values = resolved
            fields = {}
            for f in spec.fields:
                fields[f.name] = _record_field(values.get(f.name), f, citation,
                                               verified=bool(consistent))
            # 整行都是 not_found 就丢掉：那多半是模型把表头或空行当成了记录
            if any(v["status"] == "found" for v in fields.values()):
                records.append({"fields": fields})
    return records, degraded


def _record_field(raw: object, spec: FieldSpec, citation: dict, *, verified: bool) -> dict:
    if raw is None:
        return fmt.field_result(status="not_found")
    try:
        value = coerce_value(raw, spec)
    except CoerceError:
        return fmt.field_result(status="error", degraded="schema_violation")
    if value is None:
        return fmt.field_result(status="not_found")
    return fmt.field_result(status="found", value=value, citations=[citation],
                            verified=verified, confidence=_confidence([citation]))


# ---------- 入口 ----------

async def run(ctx: ExtractContext, spec: SchemaSpec) -> dict:
    """跑完一次抽取，返回 DDP-Extract v1。"""
    result = {
        "extract_version": fmt.EXTRACT_VERSION,
        "doc_hash": ctx.doc_hash,
        "status": "failed",
        "degraded": None,
        "fields": {},
        "records": [],
        "usage": ctx.usage,
    }

    if spec.kind == "array":
        records, degraded = await extract_records(ctx, spec)
        result["records"] = records
        result["degraded"] = degraded
        result["status"] = "ok" if records else ("partial" if degraded else "ok")
        return result

    # 字段之间互不依赖，并发跑；但要有闸 —— 上游是同一个模型运行时
    fields = spec.fields[:settings.extract_max_fields]
    semaphore = asyncio.Semaphore(settings.extract_concurrency)

    done = 0
    lock = asyncio.Lock()

    async def one(field: FieldSpec) -> tuple[str, dict]:
        nonlocal done
        async with semaphore:
            try:
                item = await extract_field(ctx, field, related_fields=fields)
            except Exception:   # noqa: BLE001
                # 一个字段的意外不能丢掉整批已抽好的字段（gather 默认在第一个
                # 异常处抛出，其余结果作废）。如实记成 error，其余照常
                item = fmt.field_result(status="error", degraded="upstream_error")
        if ctx.on_progress:
            async with lock:
                done += 1
                await ctx.on_progress(done, len(fields))
        return field.name, item

    for name, item in await asyncio.gather(*(one(f) for f in fields)):
        result["fields"][name] = item

    result["status"] = fmt.overall_status(result["fields"], spec)
    # 整体降级取字段里最"值得警惕"的那个：系统性问题（上游挂了、schema 违规）
    # 优先于个别字段的检索空手而归
    result["degraded"] = _rollup_degraded(result["fields"].values())
    return result


# 越靠前越值得让用户先看见。no_hits 排最后：单个字段没检索到很常见，
# 把它冒泡成整体降级会淹掉真正的系统问题。
#
# **no_instruct_model 排第一**：它不是"某个字段没抽出来"，是**整个抽取平面不可用**
# （注册表里一个会遵循指令的模型都没有）。漏收录它的后果很隐蔽 ——
# 每个字段各自打了这个标，而顶层 degraded 是 null、status 只是 partial，
# 于是"我们没有这个能力"在结果摘要里长得像"这份文档字段比较少"。
_DEGRADED_PRIORITY = ("no_instruct_model", "upstream_error", "schema_violation",
                      "parse_mismatch", "embedding_unavailable", "vision_unavailable",
                      "crop_failed", "crop_unsupported", "no_hits")


def _rollup_degraded(items) -> str | None:
    present = {i.get("degraded") for i in items if i.get("degraded")}
    for value in _DEGRADED_PRIORITY:
        if value in present:
            return value
    return None
