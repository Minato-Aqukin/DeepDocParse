"""service 侧检索：向量优先，关键词兜底 —— 抽取平面的定位链路。

与问答平面共用同一条规矩：**检索必须有量纲下限**。
无关 query 照样能返回 top-k，没有下限就等于给每个抽不到的字段
硬塞一个最相似的噪声块当"出处"，而那正是这个项目最不能接受的错误
（带着出处的假答案比没有答案更伤）。实测分布见 Web 层 config.qa_min_similarity 的注释。

**降级一律返回给调用方，不在这里吞掉。** 向量化挂了要能被看见，
静默退回关键词路是 M4a 吃过的大亏。
"""
import math
from dataclasses import dataclass, field as dc_field
from itertools import zip_longest

import httpx

from ddp_gateway.config import request_model_id, settings, upstream_auth_headers
from ddp_gateway.services.task_store import TaskStore
from ddp_core.tokenize import whole_text_bigrams


@dataclass
class Retrieved:
    hits: list[dict] = dc_field(default_factory=list)
    degraded: str | None = None
    mode: str = "none"          # vector | keyword | none —— 排查用，别当分支条件


# 分词搬到了 ddp_core（两侧共用同一份）。这里保留 `tokenize` 这个名字是因为
# 本模块的 keyword_rank 与测试都在用它 —— 换的是实现位置，不是行为：
# `whole_text_bigrams` 与原来这段逐字等价（含"二元组跨段"那个已知粗糙处）。
tokenize = whole_text_bigrams


def keyword_rank(chunks: list[dict], query: str, k: int) -> list[dict]:
    """BM25 的轻量替代：idf 加权词频。

    不引 rank_bm25：它对全空语料会除零崩溃（M3 验收抓到过），
    而这里的语料随时可能全是符号块。自己算反倒容易保证边界安全。
    """
    q_tokens = set(tokenize(query))
    if not q_tokens or not chunks:
        return []
    doc_tokens = [set(tokenize(c["text"])) for c in chunks]
    n = len(chunks)
    idf = {t: math.log(1 + n / (1 + sum(1 for d in doc_tokens if t in d))) for t in q_tokens}

    scored = []
    for chunk, tokens in zip(chunks, doc_tokens):
        score = sum(idf[t] for t in q_tokens if t in tokens)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [c for _, c in scored[:k]]


def _merge_variant_hits(ranked_lists: list[list[dict]], limit: int) -> list[dict]:
    """多路召回按轮次交错合并：原问题每轮先占一位，各路内部名次不动。

    与 corpus 侧 `search_query` 同一条交错规则（见 ddp_core/search.py），
    只是这里没有跨路 RRF 融合分 —— gateway 侧本来就没有融合分，
    每路各自的 similarity 下限在进合并前已经生效，合并只做去重与轮转。
    """
    merged: list[dict] = []
    seen: set = set()
    for row in zip_longest(*ranked_lists):
        for hit in row:
            if hit is None:
                continue
            key = hit.get("seq")
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= limit:
                return merged
    return merged


async def embed_queries(http: httpx.AsyncClient, registry,
                        queries: list[str]) -> list[list[float] | None]:
    """Embed one query batch, preserving input identity through response indices."""
    if not queries or not registry.embedding_models:
        return [None] * len(queries)
    try:
        name, entry = registry.default_of(registry.embedding_models)
        resp = await http.post(f"{entry.endpoint}/v1/embeddings",
                               json={"model": request_model_id(name, entry),
                                     "input": queries},
                               headers=upstream_auth_headers(entry))
        resp.raise_for_status()
        rows = resp.json()["data"]
        if not isinstance(rows, list) or len(rows) != len(queries):
            raise ValueError("embedding batch size mismatch")
        vectors: list[list[float] | None] = [None] * len(queries)
        for row in rows:
            index, vector = row["index"], row["embedding"]
            if (type(index) is not int or not 0 <= index < len(queries)
                    or vectors[index] is not None
                    or not isinstance(vector, list) or not vector):
                raise ValueError("embedding batch identity mismatch")
            vectors[index] = vector
        return vectors
    except Exception:
        # A partial/ambiguous batch must not bind another query's vector.
        return [None] * len(queries)


async def retrieve(store: TaskStore, http: httpx.AsyncClient, registry, *,
                   doc_hash: str, query: str, k: int,
                   corpus: list[dict] | None = None,
                   prefer_types: tuple[str, ...] = ()) -> Retrieved:
    """一次检索。prefer_types 非空时把这些块类型排到前面（抽表格记录用）。

    `corpus` 是关键词路的语料。给了就用它，不给才去 Redis 取 ——
    **这条通路是无 GPU 部署下抽取平面能工作的唯一原因**：没注册 embedding 模型时
    Redis 里压根没有分块索引（worker 的 chunk_and_index 是注册表驱动的），
    调用方可以直接把版面派生的块喂进来，关键词路照常工作。
    没有它的话，"无 GPU 快速开始"在抽取平面上会重演 M7 那次
    「路径做了但缺省请求必然失败」的坑。
    """
    vector = (await embed_queries(http, registry, [query]))[0]
    return await _retrieve_vector(
        store, doc_hash=doc_hash, query=query, vector=vector, k=k,
        corpus=corpus, prefer_types=prefer_types)


async def _retrieve_vector(store: TaskStore, *, doc_hash: str, query: str,
                           vector: list[float] | None, k: int,
                           corpus: list[dict] | None = None,
                           prefer_types: tuple[str, ...] = ()) -> Retrieved:
    degraded = None if vector else "embedding_unavailable"

    hits = None
    if vector:
        # 多要一些候选再按下限过滤：卡在 k 上过滤会让"过线的没进 top-k"白白丢掉
        hits = await store.search_chunks(doc_hash, vector, k * 3)
        if hits is not None:
            hits = [h for h in hits
                    if (h.get("similarity") or 0.0) >= settings.extract_min_similarity]

    if hits:
        mode = "vector"
    else:
        # 向量路没跑或零命中：退关键词路。**向量路跑了但被下限全滤掉时不退**——
        # 那说明"确实不相关"，退回关键词路等于绕过刚刚生效的下限，
        # 把已经判定为不相关的块又捞回来当出处（Web 层 PgVectorIndex 踩过同一个坑）
        if vector and hits is not None:
            return Retrieved(hits=[], degraded=degraded, mode="vector")
        chunks = corpus if corpus is not None else await store.load_chunks(doc_hash)
        hits = keyword_rank(chunks, query, k * 3)
        mode = "keyword" if hits else "none"

    if prefer_types:
        hits.sort(key=lambda h: 0 if h.get("block_type") in prefer_types else 1)
    return Retrieved(hits=hits[:k], degraded=degraded, mode=mode)


async def retrieve_with_variants(store: TaskStore, http: httpx.AsyncClient, registry, *,
                                 doc_hash: str, query: str,
                                 query_variants: list[str] | None = None, k: int = 4,
                                 corpus: list[dict] | None = None,
                                 prefer_types: tuple[str, ...] = ()) -> Retrieved:
    """多路有界召回：原 query 逐条加独立变体，各交错合并后截断到 k。

    与 corpus 侧 `search_query(query_variants=...)` 同一条语义：
    所有检索词一次批量向量化；每路独立走向量/关键词与相似度下限，
    路内名次不动，轮转去重后取前 k。最终候选数不变，不引入第二套打分。
    """
    legs = [query, *(query_variants or [])]
    legs = list(dict.fromkeys(leg for leg in legs if leg and leg.strip()))
    if len(legs) <= 1:
        return await retrieve(store, http, registry, doc_hash=doc_hash,
                              query=query, k=k, corpus=corpus,
                              prefer_types=prefer_types)
    per_leg = max(k * 3, k)
    vectors = await embed_queries(http, registry, legs)
    found = [await _retrieve_vector(
        store, doc_hash=doc_hash, query=leg, vector=vector, k=per_leg, corpus=corpus)
        for leg, vector in zip(legs, vectors, strict=True)]
    merged = _merge_variant_hits([r.hits for r in found], k)
    if prefer_types:
        merged = sorted(merged,
                        key=lambda h: 0 if h.get("block_type") in prefer_types else 1)
    degraded = None if any(r.degraded is None for r in found) else found[0].degraded
    mode = next((r.mode for r in found if r.hits), found[0].mode)
    return Retrieved(hits=merged, degraded=degraded, mode=mode)
