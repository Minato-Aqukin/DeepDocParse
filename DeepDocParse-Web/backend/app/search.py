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
    """检索命中：{chunk_id, document_id, page_idx, bbox, page_size, text, score}"""


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
    """

    async def search(self, session: AsyncSession, *, vector: list[float] | None, query: str,
                     document_id: str | None, user_id: str, limit: int,
                     candidates: int) -> list[Hit]:
        scope = "c.document_id = :document_id" if document_id else "d.user_id = :user_id"
        params = {"user_id": user_id, "document_id": document_id,
                  "qvec": str(list(vector)) if vector else None, "q": query, "n": candidates,
                  # <=> 是余弦距离 = 1 - 相似度
                  "max_dist": 1.0 - settings.qa_min_similarity}

        # 带距离下限：无阈值的 top-k 会让"完全无关的问题"也拿到出处（见 config 注释）
        vec_sql = text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {scope} AND d.deleted_at IS NULL AND c.embedding IS NOT NULL
              AND c.embedding <=> CAST(:qvec AS vector) < :max_dist
            ORDER BY c.embedding <=> CAST(:qvec AS vector) LIMIT :n
        """)
        # websearch_to_tsquery 容忍自然语言输入（不会因为标点直接抛错）
        kw_sql = text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id,
                 websearch_to_tsquery('simple', :q) tsq
            WHERE {scope} AND d.deleted_at IS NULL
              AND to_tsvector('simple', c.text) @@ tsq
            ORDER BY ts_rank_cd(to_tsvector('simple', c.text), tsq) DESC LIMIT :n
        """)

        vec_ids = [r[0] for r in (await session.execute(vec_sql, params)).all()] if vector else []
        try:
            kw_ids = [r[0] for r in (await session.execute(kw_sql, params)).all()]
        except Exception:       # 关键词路失败（畸形查询等）不该拖垮整个检索
            await session.rollback()
            kw_ids = []

        scores = _rrf([vec_ids, kw_ids])
        if not scores:
            return []
        top_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:limit]
        return await _load_hits(session, top_ids, scores)


async def _load_hits(session: AsyncSession, chunk_ids: list[str],
                     scores: dict[str, float]) -> list[Hit]:
    rows = (await session.execute(
        text("""SELECT id, document_id, page_idx, bbox, page_size, text
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
                        page_idx=row["page_idx"], bbox=_as_list(row["bbox"]),
                        page_size=_as_list(row["page_size"]), text=row["text"],
                        score=round(scores[cid], 6)))
    return hits


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
        if vector:      # None = 向量化失败，只走关键词路（与 PgVectorIndex 语义一致）
            scored_vec = [(c.id, _cosine(vector, c.embedding)) for c, _ in rows if c.embedding]
            # 同样的相似度下限，语义与 PgVectorIndex 对齐
            # 用 > 而不是 >=，与 PgVectorIndex 的 `dist < max_dist` 边界严格对齐
            scored_vec = [p for p in scored_vec if p[1] > settings.qa_min_similarity]
            vec_ids = [cid for cid, _ in
                       sorted(scored_vec, key=lambda p: p[1], reverse=True)[:candidates]]

        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        scored_kw = [(c.id, sum(c.text.lower().count(t) for t in terms)) for c, _ in rows]
        kw_ids = [cid for cid, n in sorted(scored_kw, key=lambda p: p[1], reverse=True)[:candidates]
                  if n > 0]

        scores = _rrf([vec_ids, kw_ids])
        if not scores:
            return []
        top_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:limit]
        by_id = {c.id: c for c, _ in rows}
        return [Hit(chunk_id=cid, document_id=by_id[cid].document_id,
                    page_idx=by_id[cid].page_idx, bbox=by_id[cid].bbox,
                    page_size=by_id[cid].page_size, text=by_id[cid].text,
                    score=round(scores[cid], 6))
                for cid in top_ids if cid in by_id]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
