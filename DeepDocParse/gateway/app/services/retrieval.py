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

import httpx

from app.config import settings
from app.services.task_store import TaskStore
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


async def embed_query(http: httpx.AsyncClient, registry, query: str) -> list[float] | None:
    """把 query 向量化。任何一环不可用返回 None —— 调用方据此打 embedding_unavailable。"""
    if not registry.embedding_models:
        return None
    try:
        name, entry = registry.default_of(registry.embedding_models)
        resp = await http.post(f"{entry.endpoint}/v1/embeddings",
                               json={"model": name, "input": [query]})
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception:
        return None


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
    vector = await embed_query(http, registry, query)
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
