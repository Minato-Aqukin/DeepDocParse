"""检索层：向量 + 关键词的混合检索。

抽成协议是为了保住 M5 那套"单测在 SQLite in-memory 里跑完"的反馈速度：
生产用 PgVectorIndex（pgvector 的 <=> 与 tsvector），单测注入 MemoryIndex。
模型层不写 SQL，检索 SQL 全部收在这里。

融合用 Reciprocal Rank Fusion 而不是加权分数相加：向量距离与 ts_rank 量纲完全不同，
加权要调两个超参且换 embedding 模型就失效；RRF 只看名次，无量纲、无需调参。
"""
import math
import re
from typing import Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

RRF_K = 60          # 业界惯用值：名次靠后的贡献平滑衰减


class Hit(dict):
    """命中：{chunk_id, document_id, parse_job_id, seq, page_idx, bbox, page_size, text,
              score, similarity}

    **score 与 similarity 是两回事，别混用**：
    - `score` 是 RRF 融合分，只由名次决定，上限 2/(60+1)≈0.0328。它能排序，
      但表达不了"有多相关"——两路都排第一的块永远是 0.0328，不管它其实多勉强。
    - `similarity` 是问题与块的余弦相似度，有校准过的量纲（下限 0.45，实测真实命中
      0.725~0.786、无关问题 0.246~0.381）。**要给用户看的是它**。
      向量路没跑（向量化挂了）或该块没有向量时为 None。

    `chunk_id` 是随机 UUID，**每次 reindex 都会重铸**（indexing.py 先 DELETE 再 add_all）。
    因此出处的稳定定位键是 `(document_id, parse_job_id, seq)`，chunk_id 只作即时引用。
    检索层必须把这三个字段一并带出来，否则出处一旦落库就再也接不回原文（P0）。
    """


class SearchIndex(Protocol):
    async def search(self, session: AsyncSession, *, vector: list[float] | None, query: str,
                     document_id: str | None, user_id: str, limit: int,
                     candidates: int) -> list[Hit]: ...


def _rrf(ranked_lists: list[list[str]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, cid in enumerate(ranked):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


class PgVectorIndex:
    """PostgreSQL + pgvector 实现。

    `vector=None` 表示问题向量化失败：**只走关键词路**。
    绝不能拿零向量顶上——`<=>` 对零向量给不出有意义的名次，
    等于把任意 N 条 chunk 以满权重灌进 RRF，比丢掉语义检索更糟。

    **相似度下限对两路都生效**（曾经只加在向量路上）：RRF 是并集融合，
    关键词路没有下限的话，向量路已经判定"全都不相关"的问题，仍会靠共现词
    捞出几条 chunk 当出处——而 qa.py 的 verified 只看有没有裁剪图，
    于是假出处还会被打上"已做视觉验证"。正是 config.qa_min_similarity 要防的事。
    唯一豁免是向量化本身挂了（vector=None）：那时无从测量，只能放行，
    但调用方已经标了 degraded=embedding_unavailable，降级是可见的。
    """

    async def search(self, session: AsyncSession, *, vector: list[float] | None, query: str,
                     document_id: str | None, user_id: str, limit: int,
                     candidates: int) -> list[Hit]:
        scope = "c.document_id = :document_id" if document_id else "d.user_id = :user_id"
        params = {"user_id": user_id, "document_id": document_id,
                  "qvec": str(list(vector)) if vector else None, "q": query, "n": candidates,
                  # <=> 是余弦距离 = 1 - 相似度
                  "max_dist": 1.0 - settings.qa_min_similarity}

        # 带距离下限：无阈值的 top-k 会让"完全无关的问题"也拿到出处（见 config 注释）。
        # 顺带把距离取回来 —— 出处要给用户看"有多相关"，RRF 分做不到（见 Hit 的注释）
        vec_sql = text(f"""
            SELECT c.id, c.embedding <=> CAST(:qvec AS vector) AS dist
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {scope} AND d.deleted_at IS NULL AND c.embedding IS NOT NULL
              AND c.embedding <=> CAST(:qvec AS vector) < :max_dist
            ORDER BY c.embedding <=> CAST(:qvec AS vector) LIMIT :n
        """)
        # 同一把尺子也量关键词路。拼进 SQL 而不是用 `:qvec IS NULL OR ...`：
        # vector=None 时 CAST(NULL AS vector) 的参数类型推断在不同驱动上行为不一，
        # 干脆不让这段出现在语句里
        kw_floor = ("""
              AND (c.embedding IS NULL
                   OR c.embedding <=> CAST(:qvec AS vector) < :max_dist)
        """ if vector else "")
        # websearch_to_tsquery 容忍自然语言输入（不会因为标点直接抛错）
        kw_sql = text(f"""
            SELECT c.id, {"c.embedding <=> CAST(:qvec AS vector)" if vector else "NULL"} AS dist
            FROM chunks c JOIN documents d ON d.id = c.document_id,
                 websearch_to_tsquery('simple', :q) tsq
            WHERE {scope} AND d.deleted_at IS NULL
              AND to_tsvector('simple', c.text) @@ tsq
              {kw_floor}
            ORDER BY ts_rank_cd(to_tsvector('simple', c.text), tsq) DESC LIMIT :n
        """)

        similarity: dict[str, float] = {}
        vec_ids: list[str] = []
        if vector:
            for cid, dist in (await session.execute(vec_sql, params)).all():
                vec_ids.append(cid)
                similarity[cid] = 1.0 - float(dist)      # <=> 是余弦距离
        try:
            kw_ids = []
            for cid, dist in (await session.execute(kw_sql, params)).all():
                kw_ids.append(cid)
                if dist is not None:
                    similarity.setdefault(cid, 1.0 - float(dist))
        except Exception:       # 关键词路失败（畸形查询等）不该拖垮整个检索
            await session.rollback()
            kw_ids = []

        scores = _rrf([vec_ids, kw_ids])
        if not scores:
            return []
        top_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:limit]
        return await _load_hits(session, top_ids, scores, similarity)


async def _load_hits(session: AsyncSession, chunk_ids: list[str], scores: dict[str, float],
                     similarity: dict[str, float] | None = None) -> list[Hit]:
    rows = (await session.execute(
        text("""SELECT id, document_id, parse_job_id, seq, page_idx, bbox, page_size, text
                FROM chunks WHERE id IN :ids""").bindparams(
            bindparam("ids", expanding=True)),
        {"ids": chunk_ids},
    )).mappings().all()
    by_id = {r["id"]: r for r in rows}
    hits: list[Hit] = []
    for cid in chunk_ids:                       # 保持 RRF 的名次
        row = by_id.get(cid)
        if row is None:
            continue
        hits.append(Hit(chunk_id=row["id"], document_id=row["document_id"],
                        parse_job_id=row["parse_job_id"], seq=row["seq"],
                        page_idx=row["page_idx"], bbox=_as_list(row["bbox"]),
                        page_size=_as_list(row["page_size"]), text=row["text"],
                        score=round(scores[cid], 6),
                        similarity=_round_or_none((similarity or {}).get(cid))))
    return hits


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _as_list(value):
    if isinstance(value, str):
        import json
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


class MemoryIndex:
    """单测用：纯 Python 余弦 + 词面匹配，语义与 PgVectorIndex 对齐（同样走 RRF）。"""

    async def search(self, session: AsyncSession, *, vector: list[float] | None, query: str,
                     document_id: str | None, user_id: str, limit: int,
                     candidates: int) -> list[Hit]:
        from sqlalchemy import select

        from app.models import Chunk, Document

        stmt = select(Chunk, Document).join(Document, Document.id == Chunk.document_id).where(
            Document.deleted_at.is_(None))
        stmt = stmt.where(Chunk.document_id == document_id) if document_id \
            else stmt.where(Document.user_id == user_id)
        rows = (await session.execute(stmt)).all()

        vec_ids: list[str] = []
        # 相似度下限对两路都生效，语义与 PgVectorIndex 对齐。
        # 用 > 而不是 >=，与 PgVectorIndex 的 `dist < max_dist` 边界严格对齐
        similar_enough: dict[str, bool] = {}
        similarity: dict[str, float] = {}
        if vector:      # None = 向量化失败，只走关键词路（与 PgVectorIndex 语义一致）
            scored_vec = [(c.id, _cosine(vector, c.embedding)) for c, _ in rows if c.embedding]
            similarity = dict(scored_vec)
            similar_enough = {cid: s > settings.qa_min_similarity for cid, s in scored_vec}
            vec_ids = [cid for cid, s in
                       sorted(scored_vec, key=lambda p: p[1], reverse=True)[:candidates]
                       if s > settings.qa_min_similarity]

        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        scored_kw = [(c.id, sum(c.text.lower().count(t) for t in terms)) for c, _ in rows
                     # 测得出相似度就必须过线；测不出（无向量/向量化挂了）才放行
                     if similar_enough.get(c.id, True)]
        kw_ids = [cid for cid, n in sorted(scored_kw, key=lambda p: p[1], reverse=True)[:candidates]
                  if n > 0]

        scores = _rrf([vec_ids, kw_ids])
        if not scores:
            return []
        top_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:limit]
        by_id = {c.id: c for c, _ in rows}
        return [Hit(chunk_id=cid, document_id=by_id[cid].document_id,
                    parse_job_id=by_id[cid].parse_job_id, seq=by_id[cid].seq,
                    page_idx=by_id[cid].page_idx, bbox=by_id[cid].bbox,
                    page_size=by_id[cid].page_size, text=by_id[cid].text,
                    score=round(scores[cid], 6),
                    similarity=_round_or_none(similarity.get(cid)))
                for cid in top_ids if cid in by_id]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
