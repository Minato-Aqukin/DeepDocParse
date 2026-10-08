"""Keyword-leg degradation is visible and vector ranking is deterministic."""
from sqlalchemy import select

from ddp_core.hits import Hit
from ddp_core.models import Chunk, Document, ParseJob
from ddp_core.search import MemoryIndex, PgVectorIndex, SearchHits, search_query


def test_search_hits_defaults_to_no_degraded():
    assert SearchHits().degraded is None
    assert SearchHits([{"chunk_id": "c"}]).degraded is None
    assert SearchHits([], degraded="keyword_unavailable").degraded == "keyword_unavailable"


async def test_pg_keyword_leg_failure_marks_degraded_and_keeps_outer_tx(
        sqlite_sessionmaker):
    """SQLite has no to_tsvector, so the keyword leg must fail here.

    The failure must surface as degraded='keyword_unavailable' (not silent
    vector-only), and the savepoint must leave the outer transaction intact:
    a row flushed before the search is still visible afterwards. The old
    `session.rollback()` path would have discarded it.
    """
    maker = await sqlite_sessionmaker()
    async with maker() as session:
        session.add(Document(uploaded_by="search-degraded", doc_id="kw-tx-doc",
                             filename="m.pdf", mime="application/pdf", object_key=""))
        await session.flush()
        doc_id = (await session.execute(
            select(Document.id).where(Document.doc_id == "kw-tx-doc"))).scalar_one()

        hits = await PgVectorIndex().search(
            session, vector=None, query="supply voltage", document_id=None,
            limit=5, candidates=5, min_similarity=0.4)

        assert isinstance(hits, SearchHits)
        assert hits.degraded == "keyword_unavailable"
        assert hits == []
        assert (await session.scalar(
            select(Document.id).where(Document.id == doc_id))) == doc_id
        await session.commit()


class _KeywordDownIndex:
    """Stub whose keyword leg already failed, like PgVectorIndex on bad tsquery."""

    async def search(self, session, **kwargs):
        return SearchHits(
            [Hit(chunk_id="c1", document_id="d", parse_job_id="j", seq=0,
                 page_idx=0, text="t", score=0.03, similarity=0.8)],
            degraded="keyword_unavailable")


async def test_search_query_propagates_keyword_unavailable():
    async def embed(texts):
        return [[1.0] for _ in texts]

    hits, degraded = await search_query(
        None, _KeywordDownIndex(), embed=embed, query="What is the supply voltage?",
        document_id=None, limit=2, candidates=4, min_similarity=0.4)
    assert [hit["chunk_id"] for hit in hits] == ["c1"]
    assert degraded == "keyword_unavailable"


async def test_search_query_embedding_failure_takes_precedence():
    """Vector leg down beats keyword leg down: the whole semantic leg never ran."""
    async def boom(texts):
        raise RuntimeError("no embedding")

    _, degraded = await search_query(
        None, _KeywordDownIndex(), embed=boom, query="What is the supply voltage?",
        document_id=None, limit=2, candidates=4, min_similarity=0.4)
    assert degraded == "embedding_unavailable"


async def _seed(session, chunks, *, doc_id, embedding=None):
    document = Document(uploaded_by="search-degraded", doc_id=doc_id,
                        filename="m.pdf", mime="application/pdf", object_key="")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="test", options={},
                   options_hash="h" * 64, status="succeeded")
    session.add(job)
    await session.flush()
    for seq, (cid, text) in enumerate(chunks):
        session.add(Chunk(id=cid, document_id=document.id, parse_job_id=job.id,
                          seq=seq, page_idx=0, text=text, search_text=text,
                          text_tokenized=text, block_type="text",
                          embedding=embedding))
    await session.commit()
    return document


async def test_memory_index_breaks_vector_ties_by_id(sqlite_sessionmaker):
    """Equal cosine similarity must rank by id ascending, like PG's
    `ORDER BY distance, c.id ASC` — not scan order (here ids are inserted
    largest-first). The query term matches nothing, so the keyword leg is
    empty and the vector leg alone decides the order.
    """
    maker = await sqlite_sessionmaker()
    async with maker() as session:
        document = await _seed(
            session, [("c-chunk", "alpha beta gamma"),
                      ("b-chunk", "alpha beta gamma"),
                      ("a-chunk", "alpha beta gamma")],
            doc_id="tie-doc", embedding=[1.0, 0.0])

        only = await MemoryIndex().search(
            session, vector=[1.0, 0.0], query="zzzqqq",
            document_id=document.id, limit=1, candidates=1, min_similarity=0.4)
        assert isinstance(only, SearchHits)
        assert [hit["chunk_id"] for hit in only] == ["a-chunk"]

        ordered = await MemoryIndex().search(
            session, vector=[1.0, 0.0], query="zzzqqq",
            document_id=document.id, limit=3, candidates=3, min_similarity=0.4)
        assert [hit["chunk_id"] for hit in ordered] == ["a-chunk", "b-chunk", "c-chunk"]


async def test_memory_index_search_returns_search_hits(sqlite_sessionmaker):
    maker = await sqlite_sessionmaker()
    async with maker() as session:
        document = await _seed(session, [("only", "supply voltage regulator")],
                               doc_id="hits-doc")

        hits = await MemoryIndex().search(
            session, vector=None, query="supply voltage",
            document_id=document.id, limit=5, candidates=5, min_similarity=0.4)
        assert isinstance(hits, SearchHits)
        assert hits.degraded is None
        assert [hit["chunk_id"] for hit in hits] == ["only"]

        empty = await MemoryIndex().search(
            session, vector=None, query="supply voltage",
            document_id=document.id, limit=5, candidates=5, min_similarity=0.4,
            authorized_document_ids=[])
        assert isinstance(empty, SearchHits)
        assert empty == []
