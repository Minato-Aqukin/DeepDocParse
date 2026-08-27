"""evidence / citation 双写（plan.md 阶段 2b）。

## 这一步在做什么，以及**没有**在做什么

出处今天只是两处 JSON 列里的字典：`messages.citations` 与
`extraction_items.fields[].citations`。查不了、连不了、没法反查"这条证据被谁引过"，
也没有地方安放复核状态 —— 三条系统级属性（可追溯 / 可复核 / 可更新）各缺一个支点。

这一步**只把同样的东西再写一份到两张真表里**：

    老路径照常写（一个字节都没动）  →  新表同时写一份  →  读仍然走老路

读切换与历史回填是阶段 3。所以这一步随时可以停：drop 两张表即可，
老路径从头到尾没被碰过。

## 为什么用 savepoint 而不是 try/except

双写失败**不许影响老路径**。但如果只是把异常吞掉，session 已经被
IntegrityError 之类污染了，接下来老路径那次 `commit()` 照样会炸 ——
"不影响老路径"就成了一句空话。`begin_nested()` 开一个 SAVEPOINT，
出错只回滚到这里，老路径挂起的那些改动完好无损。

## 失败怎么让人看见（不变式 2）

本层没有日志设施（全仓库零 `logging` 调用），可见性一向靠
degraded 字段与 metrics。degraded 字段属于老路径，这一步不许动它，
所以用一个 Prometheus 计数器。**别改成静默 `pass`** ——
"新表比老 JSON 少了一批"是一个查不出原因的悬案，而阶段 3 会直接读新表。
"""
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.anchor import digest_of, same_content
from ddp_core.models import Chunk, Citation, Evidence

# 老 JSON 写成功、新表没写成的次数。阶段 3 读切换之前，这个计数必须是 0 —— 
# 非 0 意味着新表缺了一批出处，而切过去之后那些回答会变成"没有出处"
DUAL_WRITE_FAILURES = Counter(
    "evidence_dual_write_failures_total",
    "evidence/citation 双写失败次数（老路径已成功，新表没写上）",
    ["source_kind"],
)
DUAL_WRITE_CITATIONS = Counter(
    "evidence_dual_write_citations_total",
    "双写成功落库的 citation 行数",
    ["source_kind"],
)


def _locator(citation: dict) -> tuple[str, int] | None:
    """出处的稳定定位键 `(parse_job_id, seq)`。

    缺任一段就定位不到块 —— 那种 citation 老路径里也是"接不回去"的
    （`attach_resolution` 会标 `resolved=False`），新表同样不该给它造一行证据。
    """
    job, seq = citation.get("parse_job_id"), citation.get("seq")
    if not job or seq is None:
        return None
    return (job, int(seq))


async def record_evidence(session: AsyncSession, citations: list[dict], *,
                          source_kind: str, source_id: str) -> int:
    """把一批出处双写进 evidence / citations 两张表。返回落库的 citation 行数。

    **在老路径写完之后调用，同一个 session、同一次 commit。**
    这样双写与老 JSON 要么一起在，要么一起不在 —— 对拍（阶段 2b 的验收标准）
    才有意义；分成两次事务的话，中间崩掉就会留下一批对不上的数据。

    `citations` 是老 JSON 里那些字典（问答与抽取两个平面形状一致）。

    **每次调用两条 SELECT**，抽取平面是按字段调的，60 字段的 schema 就是
    120 条本地查询。如实记一笔：相对同一次抽取里 60 次检索 + 60 次模型调用，
    这点开销可以忽略；但阶段 3 读切换后如果这里成了热点，第一件事是把
    整个 item 的字段合成一次批量查询，别先去动索引。
    """
    try:
        async with session.begin_nested():
            return await _record(session, citations, source_kind=source_kind,
                                 source_id=source_id)
    except Exception:      # noqa: BLE001 —— 双写绝不能拖垮老路径，但必须留痕
        DUAL_WRITE_FAILURES.labels(source_kind=source_kind).inc()
        return 0


def locators_of(citations: list[dict]) -> dict[tuple[str, int], dict]:
    """老 JSON 的出处列表 -> `{定位键: 出处}`，**保序**（顺序即检索名次）。

    抽出来是因为**双写与历史回填必须用同一条规则**：
    规则一旦有差异，对拍（阶段 2b 的验收标准）就会报出一堆不存在的差异，
    而真正的差异反倒被淹掉。
    """
    locators: dict[tuple[str, int], dict] = {}
    for citation in citations or []:
        key = _locator(citation)
        if key is not None:
            # 同一条 message 的 citations 里出现两个相同的定位键是数据错误，
            # 不是"引用了两次" —— 保留先出现的那条（它的名次更靠前）
            locators.setdefault(key, citation)
    return locators


async def _record(session: AsyncSession, citations: list[dict], *,
                  source_kind: str, source_id: str) -> int:
    locators = locators_of(citations)
    if not locators:
        return 0

    # **证据的字段一律取自 chunks 行，不取自 citation dict。**
    # 问答侧的 citation dict 里根本没有 page_size（抽取侧才有），
    # 照着 dict 建证据会让一半的行静默存成 page_size=NULL，
    # 而缺它遇到 CropBox 偏移/旋转页就会裁错区域。
    # content_digest 更是只能从这里来 —— dict 里只有截断过的 snippet。
    jobs = {job for job, _ in locators}
    seqs = {seq for _, seq in locators}
    chunks = {
        (c.parse_job_id, c.seq): c
        for c in (await session.execute(
            select(Chunk).where(Chunk.parse_job_id.in_(jobs), Chunk.seq.in_(seqs))
        )).scalars().all()
        if (c.parse_job_id, c.seq) in locators
    }

    existing = {
        (e.parse_job_id, e.seq): e
        for e in (await session.execute(
            select(Evidence).where(Evidence.parse_job_id.in_(jobs), Evidence.seq.in_(seqs))
        )).scalars().all()
    }

    written = 0
    # **enumerate 的顺序就是名次**：`citations` 传进来时是有序列表（检索名次），
    # locators 用 dict 保序，所以这里的下标与老 JSON 的下标一一对应
    for rank, (key, citation) in enumerate(locators.items()):
        chunk = chunks.get(key)
        if chunk is None:
            # 块已经不在了（重建索引换了分块规则，或文档被删）。
            # 老路径对这种 citation 同样是 resolved=False，双写跟着跳过就是一致的
            continue
        evidence = existing.get(key)
        if evidence is None:
            evidence = Evidence(
                document_id=chunk.document_id, parse_job_id=chunk.parse_job_id,
                seq=chunk.seq, page_idx=chunk.page_idx, bbox=chunk.bbox,
                page_size=chunk.page_size, kind=chunk.block_type or "text",
                crop_key=citation.get("crop_key"),
                content_digest=digest_of(chunk.text),
            )
            session.add(evidence)
            await session.flush()
            existing[key] = evidence
        elif evidence.crop_key is None and citation.get("crop_key"):
            # 第一次引用时没裁图（不是 PDF、或裁剪失败），这次裁出来了 —— 补上。
            # **反过来不覆盖**：已有的裁图不因为这次没裁而被抹掉
            evidence.crop_key = citation["crop_key"]

        session.add(Citation(
            evidence_id=evidence.id, source_kind=source_kind, source_id=source_id,
            role="primary",         # 阶段 2b 只写 primary，理由见 models.Citation
            score=citation.get("score"), similarity=citation.get("similarity"),
            snippet=citation.get("snippet") or "", rank=rank,
            # **每条引用各记各的**：证据行上那份是首次锚定时的内容，
            # 这份是这一次引用当时的内容。同一个块跨重建被引两次，两者会不同
            content_digest=digest_of(chunk.text),
        ))
        written += 1

    await session.flush()
    DUAL_WRITE_CITATIONS.labels(source_kind=source_kind).inc(written)
    return written


# ---------------------------------------------------------------------------
# 读（阶段 3）
# ---------------------------------------------------------------------------


async def load_citations(session: AsyncSession, *, source_kind: str,
                         source_ids: list[str]) -> dict[str, list[dict]]:
    """一次查回多个来源的出处，**并接回当前索引**。返回 source_id -> 有序出处列表。

    形状与老 JSON 逐字段一致（前端 CitationChip 两个平面共用，不能只对一边改）。
    唯一不同的是 `chunk_id`：它每次 reindex 都重铸，所以不落库、每次读时现算。

    ## 接回来的判据分两条路（`ddp_core.anchor.same_content`）

        content_digest 非空 -> 阶段 2b 之后写的，比指纹，**严格**
        content_digest 为空 -> 回填过来的老记录，比 snippet 包含，**宽松**

    老记录当年只存了截断过的 snippet，指纹无从补算 —— 硬给它算一个"当前块的
    指纹"就等于宣布"它一直指着这里"，那是凭空造证。

    ## 接不回来时：标失效，**不刷新指向**

    `resolved=False` 且 `chunk_id=None`。刷了 chunk_id 就等于把高亮指到错块 ——
    用户看到一条"可点开"的出处，snippet 是旧文本、框在别处。
    宁可说"接不回去"，也绝不指错地方（plan.md §9 不变式 1）。

    `page_idx` / `bbox` **一律回放当年的值，不跟着现在的块走**：
    它们记录的是"这个回答当时拿哪块区域作证"，是审计事实。
    """
    if not source_ids:
        return {}

    rows = (await session.execute(
        select(Citation, Evidence)
        .join(Evidence, Citation.evidence_id == Evidence.id)
        .where(Citation.source_kind == source_kind,
               Citation.source_id.in_(source_ids),
               Citation.role == "primary")
        # rank 是检索名次；id 只是并列时的稳定兜底，保证同一份数据每次顺序一样
        .order_by(Citation.rank, Citation.id)
    )).all()
    if not rows:
        return {}

    # 全部来源的定位键合起来查**一次**当前 chunk（每条来源查一次的话，
    # 一个长会话就是 N+1 —— 老路径 load_citation_targets 也是为这个抽出来的）
    live = {
        (c.parse_job_id, c.seq): c
        for c in (await session.execute(
            select(Chunk).where(
                Chunk.parse_job_id.in_({e.parse_job_id for _, e in rows}),
                Chunk.seq.in_({e.seq for _, e in rows}))
        )).scalars().all()
    }

    out: dict[str, list[dict]] = {}
    for citation, evidence in rows:
        chunk = live.get((evidence.parse_job_id, evidence.seq))
        # **用 citation 的指纹，不是 evidence 的**：判定问的是"这一次引用
        # 还指着它当时看到的原文吗"，那是引用这个事件的属性。
        # 用证据那份的话，同一个块跨重建被引两次，后一次会被前一次的指纹判成失效
        resolved = chunk is not None and same_content(
            snippet=citation.snippet, chunk_text=chunk.text,
            digest=citation.content_digest)
        out.setdefault(citation.source_id, []).append({
            "chunk_id": chunk.id if resolved else None,
            "parse_job_id": evidence.parse_job_id,
            "seq": evidence.seq,
            # 契约把 doc_hash 列成了字段。产品层的稳定定位键是 (parse_job_id, seq)，
            # 这一路用不上它 —— 如实给 None 而不是省略，省略会让消费方在两个平面
            # 之间写两套取字段的代码
            "doc_hash": None,
            "page_idx": evidence.page_idx,
            "bbox": evidence.bbox,
            "page_size": evidence.page_size,
            "crop_key": evidence.crop_key,
            "snippet": citation.snippet,
            "score": citation.score,
            "similarity": citation.similarity,
            "resolved": resolved,
        })
    return out
