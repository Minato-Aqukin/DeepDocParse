"""Keyword ranking keeps rare facts visible without changing OR recall or ACLs."""
from hashlib import sha256

from ddp_core.models import Chunk, Document, ParseJob
from ddp_core.search import MemoryIndex


async def add_document(session, texts):
    digest = sha256("\n".join(texts.values()).encode()).hexdigest()
    document = Document(uploaded_by="ranking-reader", doc_id=digest,
                        filename="manual.pdf", mime="application/pdf", object_key="")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h" * 64, status="succeeded", result_prefix="results/ranking/")
    session.add(job)
    await session.flush()
    for seq, (identifier, text) in enumerate(texts.items()):
        session.add(Chunk(id=identifier, document_id=document.id, parse_job_id=job.id,
                          seq=seq, page_idx=0, text=text, search_text=text,
                          text_tokenized=text, block_type="text"))
    await session.commit()
    return document


async def search(session, document, query, *, authorized_document_ids=None):
    return await MemoryIndex().search(
        session, vector=None, query=query, document_id=document.id if document else None,
        limit=10, candidates=10, min_similarity=0.4,
        authorized_document_ids=authorized_document_ids)


async def test_common_headers_do_not_bury_rare_facts_or_change_recall(session):
    document = await add_document(session, {
        "decoy": "raspberry pico raspberry pico datasheet overview raspberry pico",
        "target": "dual core frequency 133 mhz pll variable core raspberry pico",
        "other": "raspberry pico gpio power pins usb raspberry pico",
    })
    hits = await search(session, document, "raspberry pico frequency core")
    assert hits[0]["chunk_id"] == "target"
    assert {hit["chunk_id"] for hit in hits} == {"target", "decoy", "other"}


async def test_absent_query_terms_do_not_destroy_common_term_ranking(session):
    document = await add_document(session, {"a": "pico", "b": "pico pico"})
    before = await search(session, document, "pico")
    hits = await search(session, document, "pico missingterm")
    assert {hit["chunk_id"] for hit in hits} == {"a", "b"}
    assert [hit["chunk_id"] for hit in hits] == [hit["chunk_id"] for hit in before]
    assert await search(session, document, "") == []


async def test_question_function_words_do_not_outrank_content_words(session):
    """问句里的 does/its 在手册里恰好稀有，按 1/df 加权会比 cpu/clock 还重 ——
    真栈 ESP32：问 CPU 时免责声明页凭它们排关键词路第一，真答案掉出候选。"""
    document = await add_document(session, {
        "disclaimer": "this document does not warrant its accuracy",
        "target": "cpu clock frequency up to 240 mhz",
        **{f"pins-{i}": "cpu pins and power" for i in range(6)},
    })
    hits = await search(session, document,
                        "What CPU clock frequency does the chip have according to its datasheet?")
    assert hits[0]["chunk_id"] == "target"
    assert "disclaimer" not in {hit["chunk_id"] for hit in hits}


async def test_unauthorized_blocks_cannot_change_keyword_ranking(session):
    document = await add_document(session, {
        "decoy": "pico pico pico", "target": "pico frequency", "other": "pico pins",
    })
    before = await search(session, None, "pico frequency", authorized_document_ids=[document.id])
    await add_document(session, {f"private-{i}": "frequency" for i in range(8)})
    after = await search(session, None, "pico frequency", authorized_document_ids=[document.id])
    assert before[0]["chunk_id"] == "target"
    assert [hit["chunk_id"] for hit in after] == [hit["chunk_id"] for hit in before]


async def test_cross_document_facts_survive_repeated_document_names(session):
    pico = await add_document(session, {
        **{f"pico-header-{i}": "pico overview " * 20 for i in range(10)},
        "pico-fact": "pico has 264 KB SRAM",
    })
    esp32 = await add_document(session, {
        **{f"esp-header-{i}": "esp32 overview " * 20 for i in range(10)},
        "esp-fact": "esp32 has 520 KB SRAM",
    })
    paper = await add_document(session, {
        f"paper-{i}": "matrix multiplication attention" for i in range(20)
    })
    hits = await search(session, None, "Compare SRAM capacities of Pico and ESP32",
                        authorized_document_ids=[pico.id, esp32.id, paper.id])
    assert {hit["chunk_id"] for hit in hits[:2]} == {"pico-fact", "esp-fact"}
