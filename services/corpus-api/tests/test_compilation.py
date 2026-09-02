"""阶段 5 编译层：源/派生 Evidence、可见降级与版本校验。"""
import asyncio
import copy
import json
from contextlib import suppress
from datetime import timedelta

import httpx
import respx
from sqlalchemy import select

from ddp_corpus.config import settings
from ddp_corpus.crops import get_or_create_crops
from ddp_corpus.evidence import load_citations
from ddp_corpus.compilation import CompileOutput
from ddp_corpus.indexing import index_document
from ddp_corpus.models import (
    Chunk, Citation, Document, Evidence, ParseJob, as_aware, utcnow,
)
from ddp_corpus.qa import Retrieval, build_messages
from ddp_corpus.storage import MemoryStorage
from ddp_core.compilation import provider_of
from ddp_core.search import MemoryIndex, exact_identifiers
from ddp_core.tokenize import code_tokenized, tokenized
from tests.conftest import CHAT, usage_events, drain_tasks
from tests.test_documents import _callback, _mock_service, _upload
from tests.test_qa import _real_pdf


VISUAL_RESULT = {
    "markdown": "![图](chart.png)",
    "layout_json": {
        "layout_version": "ddp-layout/1", "engine": "vlm-ocr",
        "code_detection": "native",
        "pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [{
            "type": "figure", "bbox": [72, 72, 540, 300],
            "lines": [{"spans": [{"content": "图 1 延迟曲线"}]}],
        }]}],
    },
    "images": [],
}


def _vision(description: str = "延迟在 80ms 后趋于平稳") -> httpx.Response:
    return httpx.Response(200, json={"model": settings.chat_model,
                                    "choices": [{"message": {"content": json.dumps({
        "description": description, "elements": ["横轴：时间", "纵轴：延迟"],
    }, ensure_ascii=False)}}]})


async def test_compile_crops_read_pdf_once_and_reuse_object_cache(monkeypatch):
    storage = MemoryStorage()
    await storage.put("source.pdf", b"fake-pdf", "application/pdf")
    calls = []

    def fake_render(pdf, requests):
        calls.append((pdf, requests))
        return [f"png-{index}".encode() for index, _ in enumerate(requests)]

    monkeypatch.setattr("ddp_corpus.crops.render_crops", fake_render)
    atoms = [
        {"seq": 0, "page_idx": 0, "bbox": [0, 0, 10, 10], "page_size": [100, 100]},
        {"seq": 1, "page_idx": 0, "bbox": [20, 20, 30, 30], "page_size": [100, 100]},
    ]
    first = await get_or_create_crops(
        storage, job_id="job", source_key="source.pdf", mime="application/pdf", atoms=atoms)
    second = await get_or_create_crops(
        storage, job_id="job", source_key="source.pdf", mime="application/pdf", atoms=atoms)
    assert set(first) == set(second) == {0, 1}
    assert len(calls) == 1 and calls[0][0] == b"fake-pdf"
    assert len(calls[0][1]) == 2


@respx.mock
async def test_compile_materializes_source_and_generated_evidence(actor_client, session):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    callback = await _callback(actor_client)
    assert callback.status_code == 200

    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "ready"
    assert detail["compile_status"] == "ready"
    assert detail["code_detection"] == "native"

    chunk = (await session.execute(select(Chunk))).scalars().one()
    rows = (await session.execute(select(Evidence).order_by(Evidence.derived_from))).scalars().all()
    assert len(rows) == 2
    source = next(e for e in rows if e.derived_from is None)
    derived = next(e for e in rows if e.derived_from is not None)
    assert derived.derived_from == source.id
    assert source.content == "图 1 延迟曲线"
    assert "80ms" in derived.content and "80ms" in chunk.search_text
    assert chunk.text == source.content, "生成理解不得写回原文"
    assert chunk.evidence_id == source.id and chunk.derived_evidence_id == derived.id
    assert source.crop_key and source.crop_key == derived.crop_key
    assert source.provider_fingerprint == chunk.provider_fingerprint
    usage = await usage_events(session, "compile_vision")
    assert len(usage) == 1
    assert usage[0]["requests"] == 1 and usage[0]["parse_job_id"] == chunk.parse_job_id


@respx.mock
async def test_vision_failure_is_partial_not_silent_or_total_failure(actor_client, session):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=httpx.Response(503, text="down"))
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "ready", "图注原文仍可索引，不应整份作废"
    assert detail["compile_status"] == "partial"
    assert "vision_unavailable" in detail["compile_degraded"]
    assert [e["requests"] for e in await usage_events(session, "compile_vision")] == [1]


@respx.mock
async def test_vision_usage_survives_later_embedding_failure(actor_client, session):
    _mock_service(result=VISUAL_RESULT, embed=httpx.Response(503, text="embed down"))
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "failed" and "向量化失败" in detail["index_error"]
    usage = await usage_events(session, "compile_vision")
    assert len(usage) == 1 and usage[0]["requests"] == 1


@respx.mock
async def test_visual_atom_without_crop_does_not_invent_vision_usage(actor_client, session):
    _mock_service(result=VISUAL_RESULT)
    document = await _upload(actor_client, b"not-a-pdf", "figure.png", "image/png")
    await _callback(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "ready"
    assert "crop_unsupported" in detail["compile_degraded"]
    assert (await usage_events(session, "compile_vision") or [None])[0] is None


@respx.mock
async def test_unresolved_default_models_are_visible_and_never_current(
        actor_client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "")
    monkeypatch.setattr(settings, "chat_model", "")
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["compile_status"] == "partial"
    assert "provider_unresolved" in detail["compile_degraded"]
    validation = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert validation["status"] == "unresolved"
    assert "provider_unresolved" in validation["reasons"]


@respx.mock
async def test_invalid_code_detection_fails_compilation_visibly(actor_client):
    result = copy.deepcopy(VISUAL_RESULT)
    result["layout_json"]["code_detection"] = "best-effort"
    _mock_service(result=result)
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "failed"
    assert detail["compile_status"] == "failed"
    assert detail["compile_degraded"] == ["compile_failed"]
    assert "invalid code_detection" in detail["index_error"]


@respx.mock
async def test_version_validation_is_read_only_and_detects_provider_drift(
        actor_client, session, monkeypatch):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)

    current = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert current["status"] == "current" and current["safe_to_reindex"] is True

    monkeypatch.setattr(settings, "embedding_model", "replacement-model")
    stale = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert stale["status"] == "stale"
    assert "embedding_model_changed" in stale["reasons"]
    # 校验只读：库里仍是原指纹，索引状态也没被排队。
    chunk = (await session.execute(select(Chunk))).scalars().one()
    assert chunk.provider_fingerprint == current["observed_fingerprints"][0]


@respx.mock
async def test_generated_citation_requires_explicit_reindex_acknowledgement(
        actor_client, session, app_state):
    _mock_service(result=VISUAL_RESULT)
    vision = respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    chunk = (await session.execute(select(Chunk))).scalars().one()
    session.add(Citation(
        evidence_id=chunk.derived_evidence_id, source_kind="message", source_id="m1",
        role="primary", snippet=chunk.derived_text or "", content_digest=(await session.get(
            Evidence, chunk.derived_evidence_id)).content_digest,
    ))
    await session.commit()

    validation = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert validation["citation_invalidations"] == 1
    assert validation["safe_to_reindex"] is False
    refused = await actor_client.post(f"/api/documents/{document['id']}/reindex")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "index_version_unsafe"

    # 用户明确确认后才能重建；VLM 生成物变了就保留老 Evidence，
    # 并把老引用显式标成 resolved=false，绝不悄悄接到新描述。
    vision.return_value = _vision("延迟在 120ms 后才趋于平稳")
    accepted = await actor_client.post(
        f"/api/documents/{document['id']}/reindex?acknowledge_invalidations=true")
    assert accepted.status_code == 202
    await drain_tasks(app_state)     # 重建索引排在持久队列里，跑一轮
    session.expire_all()
    loaded = await load_citations(session, source_kind="message", source_ids=["m1"])
    assert loaded["m1"][0]["source_type"] == "generated"
    assert loaded["m1"][0]["resolved"] is False
    assert loaded["m1"][0]["chunk_id"] is None
    assert loaded["m1"][0]["bbox"] == VISUAL_RESULT["layout_json"]["pdf_info"][0][
        "para_blocks"][0]["bbox"]


@respx.mock
async def test_unchanged_source_citation_reconnects_after_reindex(
        actor_client, session, app_state):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    chunk = (await session.execute(select(Chunk))).scalars().one()
    source = await session.get(Evidence, chunk.evidence_id)
    session.add(Citation(
        evidence_id=source.id, source_kind="message", source_id="m-source",
        role="primary", snippet=source.content, content_digest=source.content_digest,
    ))
    await session.commit()

    validation = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert validation["citation_reconnectable"] == 1
    assert validation["citation_invalidations"] == 0
    assert (await actor_client.post(
        f"/api/documents/{document['id']}/reindex")).status_code == 202
    session.expire_all()
    loaded = await load_citations(session, source_kind="message", source_ids=["m-source"])
    assert loaded["m-source"][0]["resolved"] is True
    assert loaded["m-source"][0]["evidence_id"] == source.id
    assert loaded["m-source"][0]["chunk_id"] is not None


@respx.mock
async def test_same_text_with_changed_bbox_is_not_reported_reconnectable(
        actor_client, session, app_state):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    chunk = (await session.execute(select(Chunk))).scalars().one()
    source = await session.get(Evidence, chunk.evidence_id)
    session.add(Citation(
        evidence_id=source.id, source_kind="message", source_id="bbox-drift",
        role="primary", snippet=source.content, content_digest=source.content_digest))
    await session.commit()

    layout = copy.deepcopy(VISUAL_RESULT["layout_json"])
    layout["pdf_info"][0]["para_blocks"][0]["bbox"] = [80, 72, 548, 300]
    job = await session.get(ParseJob, chunk.parse_job_id)
    await app_state.storage.put(f"{job.result_prefix}layout.json",
                                json.dumps(layout).encode(), "application/json")
    validation = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index")).json()
    assert validation["citation_reconnectable"] == 0
    assert validation["citation_invalidations"] == 1
    assert validation["safe_to_reindex"] is False


@respx.mock
async def test_validation_does_not_compare_other_job_same_seq_citation(
        actor_client, session):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    doc = await session.get(Document, document["id"])
    other = ParseJob(document_id=doc.id, engine="borndigital", options={},
                     options_hash="other-job", status="succeeded", result_prefix="other/",
                     document_version=2)
    session.add(other)
    await session.flush()
    row = (await session.execute(select(Evidence).where(Evidence.derived_from.is_(None)))).scalars().one()
    other_evidence = Evidence(
        document_id=doc.id, doc_version=2, parse_job_id=other.id, seq=row.seq,
        atom_key="source:0:other", page_idx=row.page_idx, bbox=row.bbox,
        page_size=row.page_size, kind=row.kind, content_digest=row.content_digest,
        content=row.content, provider=row.provider, provider_fingerprint=row.provider_fingerprint)
    session.add(other_evidence)
    await session.flush()
    session.add(Citation(
        evidence_id=other_evidence.id, source_kind="message", source_id="other-job",
        role="primary", snippet=row.content, content_digest=row.content_digest))
    await session.commit()

    validation = (await actor_client.post(
        f"/api/documents/{doc.id}/validate-index")).json()
    assert validation["citation_reconnectable"] == 0
    assert validation["citation_invalidations"] == 0


@respx.mock
async def test_switch_version_requires_ack_for_current_resolved_citations(
        actor_client, session, app_state):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    chunk = (await session.execute(select(Chunk))).scalars().one()
    source = await session.get(Evidence, chunk.evidence_id)
    session.add(Citation(
        evidence_id=source.id, source_kind="message", source_id="before-switch",
        role="primary", snippet=source.content, content_digest=source.content_digest))
    target = ParseJob(
        document_id=document["id"], engine="borndigital", options={"variant": 2},
        options_hash="target-job", status="succeeded", result_prefix="target/",
        page_count=1, document_version=2)
    session.add(target)
    await session.commit()
    await app_state.storage.put("target/layout.json",
                                json.dumps(VISUAL_RESULT["layout_json"]).encode(),
                                "application/json")

    validation = (await actor_client.post(
        f"/api/documents/{document['id']}/validate-index?job_id={target.id}")).json()
    assert validation["citation_invalidations"] == 1
    refused = await actor_client.put(
        f"/api/documents/{document['id']}/current-job", json={"job_id": target.id})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "index_version_unsafe"
    accepted = await actor_client.put(
        f"/api/documents/{document['id']}/current-job",
        json={"job_id": target.id, "acknowledge_invalidations": True})
    assert accepted.status_code == 200


@respx.mock
async def test_revived_document_with_history_waits_for_validation(
        actor_client, session, app_state):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    pdf = _real_pdf()
    document = await _upload(actor_client, pdf)
    await _callback(actor_client)
    chunk = (await session.execute(select(Chunk))).scalars().one()
    source = await session.get(Evidence, chunk.evidence_id)
    session.add(Citation(
        evidence_id=source.id, source_kind="message", source_id="before-delete",
        role="primary", snippet=source.content, content_digest=source.content_digest))
    await session.commit()
    assert (await actor_client.delete(f"/api/documents/{document['id']}")).status_code == 204

    revived = await _upload(actor_client, pdf)
    assert revived["index_status"] == "failed"
    assert "版本校验" in revived["index_error"]
    assert revived["compile_status"] == "failed"
    assert revived["compile_degraded"] == ["reindex_validation_required"]

    # 删除前已经排队、复活后才到达的 worker 不得绕过版本校验闸门，也不得产生新费用。
    usage_before = {e["_event_id"] for e in (await usage_events(session, "compile_vision")) + (await usage_events(session, "embed"))}
    vision_calls_before = len(respx.calls)
    assert await index_document(
        session, app_state.storage, app_state.http, document["id"]
    ) == 0
    row = await session.get(Document, document["id"])
    assert row.index_status == "failed"
    assert row.compile_status == "failed"
    assert row.compile_degraded == ["reindex_validation_required"]
    usage_after = {e["_event_id"] for e in (await usage_events(session, "compile_vision")) + (await usage_events(session, "embed"))}
    assert usage_after == usage_before
    assert len(respx.calls) == vision_calls_before
    assert (await session.scalar(select(Chunk.id).where(
        Chunk.document_id == document["id"]))) is None


@respx.mock
async def test_reindex_refuses_second_worker_while_build_is_active(actor_client, session):
    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    row = await session.get(Document, document["id"])
    row.index_status = "indexing"
    row.index_lease_until = utcnow() + timedelta(minutes=5)
    await session.commit()
    response = await actor_client.post(f"/api/documents/{document['id']}/reindex")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "index_in_progress"


@respx.mock
async def test_reconcile_recovers_expired_index_lease(
        actor_client, session, app_state):
    from ddp_corpus import db
    from ddp_corpus.reconcile import reconcile_once

    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    row = await session.get(Document, document["id"])
    old_generation = row.index_generation
    row.index_status = "indexing"
    row.compile_status = "compiling"
    row.index_lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["indexed"] == 1
    session.expire_all()
    recovered = await session.get(Document, document["id"])
    assert recovered.index_status == "ready"
    assert recovered.index_generation > old_generation
    assert recovered.index_lease_until is None


@respx.mock
async def test_old_generation_cannot_commit_while_successor_is_indexing(
        actor_client, session, app_state, monkeypatch):
    from ddp_corpus.indexing import _index_claimed

    _mock_service(result=VISUAL_RESULT)
    respx.post(CHAT).mock(return_value=_vision())
    document = await _upload(actor_client, _real_pdf())
    await _callback(actor_client)
    row = await session.get(Document, document["id"])
    existing_ids = set((await session.execute(select(Chunk.id))).scalars().all())
    stale_generation = row.index_generation
    row.index_generation += 1
    successor_generation = row.index_generation
    row.index_status = "indexing"
    row.compile_status = "compiling"
    row.index_lease_until = utcnow() + timedelta(minutes=5)
    await session.commit()

    async def stale_compile(**_kwargs):
        provider = provider_of(
            layout=VISUAL_RESULT["layout_json"], parse_options_hash="old",
            embedding_model=settings.embedding_model, vision_model=settings.chat_model)
        return CompileOutput(chunks=[{
            "seq": 0, "page_idx": 0, "bbox": [1, 1, 2, 2], "page_size": [10, 10],
            "text": "stale", "search_text": "stale", "derived_text": None,
            "char_len": 5, "block_type": "text", "table_html": None,
            "text_tokenized": "stale", "provider": provider,
            "provider_fingerprint": "stale",
        }], crop_keys={}, degraded=[], provider=provider, vision_requests=0)

    monkeypatch.setattr("ddp_corpus.indexing.compile_document", stale_compile)
    assert await _index_claimed(
        session, app_state.storage, app_state.http,
        document_id=document["id"], generation=stale_generation) == 0
    assert set((await session.execute(select(Chunk.id))).scalars().all()) == existing_ids
    session.expire_all()
    current = await session.get(Document, document["id"])
    assert current.index_generation == successor_generation
    assert current.index_status == "indexing"


async def test_stale_sessions_cannot_reuse_fencing_generation(session):
    """陈旧 ORM identity map 不能把 generation 写回旧值并复用 worker token。"""
    from ddp_corpus import db
    from ddp_corpus.indexing import _fail_if_current, claim_for_indexing
    from ddp_corpus.versions import advance_index_generation

    # 用户住在 control schema（Go 拥有），语料侧只存裸 actor id


    actor_id = "actor-fencing-owner"
    document = Document(
        uploaded_by=actor_id, doc_id="f" * 64, origin="web", filename="fence.pdf",
        mime="application/pdf", size_bytes=1, index_status="pending", index_generation=5)
    session.add(document)
    await session.flush()
    job = ParseJob(
        document_id=document.id, engine="borndigital", options={}, options_hash="fence-job",
        status="succeeded", result_prefix="fence/", document_version=1)
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    await session.commit()
    document_id, job_id = document.id, job.id

    factory = db.get_sessionmaker()
    async with factory() as stale:
        stale_document = await stale.get(Document, document_id)
        assert stale_document.index_generation == 5

        async with factory() as first:
            assert await advance_index_generation(
                first, document_id, expected_current_job_id=job_id, deleted=False,
                values={"index_status": "pending", "index_lease_until": None}) == 6
            await first.commit()
        async with factory() as first_worker:
            assert await claim_for_indexing(first_worker, document_id) == 7

        # stale 的 identity map 仍是 g5；原子 UPDATE 必须基于数据库 g7 得到 g8。
        assert stale_document.index_generation == 5
        assert await advance_index_generation(
            stale, document_id, expected_current_job_id=job_id, deleted=False,
            values={"index_status": "pending", "index_lease_until": None}) == 8
        await stale.commit()

    async with factory() as successor:
        assert await claim_for_indexing(successor, document_id) == 9
    async with factory() as late_old_worker:
        await _fail_if_current(
            late_old_worker, document_id, job_id, 7, "late old worker")
    async with factory() as verify:
        current = await verify.get(Document, document_id)
        assert current.index_generation == 9
        assert current.index_status == "indexing"
        assert current.index_error is None


async def test_active_index_worker_renews_lease(session, monkeypatch):
    from ddp_corpus.indexing import _heartbeat_lease

    # 用户住在 control schema（Go 拥有），语料侧只存裸 actor id


    actor_id = "actor-heartbeat"
    document = Document(
        uploaded_by=actor_id, doc_id="h" * 64, origin="web", filename="heartbeat.pdf",
        mime="application/pdf", size_bytes=1, index_status="indexing",
        index_generation=7, index_lease_until=utcnow() + timedelta(milliseconds=20))
    session.add(document)
    await session.commit()
    document_id = document.id
    old_lease = document.index_lease_until
    monkeypatch.setattr(settings, "index_heartbeat_seconds", 0.01)
    monkeypatch.setattr(settings, "index_lease_seconds", 1)
    task = asyncio.create_task(_heartbeat_lease(session.bind, document_id, 7))
    await asyncio.sleep(0.04)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    session.expire_all()
    renewed = await session.get(Document, document_id)
    assert as_aware(renewed.index_lease_until) > as_aware(old_lease)


@respx.mock
async def test_stale_index_worker_cannot_overwrite_new_current_job(
        session, app_state, monkeypatch):
    _mock_service()
    # 用户住在 control schema（Go 拥有），语料侧只存裸 actor id

    actor_id = "actor-race"
    document = Document(
        uploaded_by=actor_id, doc_id="d" * 64, origin="web", filename="race.pdf",
        mime="application/pdf", size_bytes=1, object_key="source.pdf",
        index_status="pending")
    session.add(document)
    await session.flush()
    old = ParseJob(
        document_id=document.id, engine="borndigital", options={}, options_hash="old",
        status="succeeded", result_prefix="old/", document_version=1)
    new = ParseJob(
        document_id=document.id, engine="borndigital", options={}, options_hash="new",
        status="succeeded", result_prefix="new/", document_version=2)
    session.add_all([old, new])
    await session.flush()
    document_id, new_job_id = document.id, new.id
    document.current_job_id = old.id
    sentinel = Chunk(
        document_id=document.id, parse_job_id=old.id, seq=99, page_idx=0,
        text="existing", search_text="existing", char_len=8, block_type="text")
    session.add(sentinel)
    await session.commit()
    await app_state.storage.put("old/layout.json", json.dumps(
        VISUAL_RESULT["layout_json"]).encode(), "application/json")
    provider = provider_of(
        layout=VISUAL_RESULT["layout_json"], parse_options_hash=old.options_hash,
        embedding_model=settings.embedding_model, vision_model=settings.chat_model)

    async def delayed_compile(**_kwargs):
        row = await session.get(Document, document_id)
        row.current_job_id = new_job_id
        row.index_status = "pending"
        row.index_generation += 1
        await session.commit()
        return CompileOutput(chunks=[{
            "seq": 0, "page_idx": 0, "bbox": [1, 1, 2, 2], "page_size": [10, 10],
            "text": "late old", "search_text": "late old", "derived_text": None,
            "char_len": 8, "block_type": "text", "table_html": None,
            "text_tokenized": "late old", "provider": provider,
            "provider_fingerprint": "fp",
        }], crop_keys={}, degraded=[], provider=provider, vision_requests=0)

    monkeypatch.setattr("ddp_corpus.indexing.compile_document", delayed_compile)
    assert await index_document(session, app_state.storage, app_state.http, document_id) == 0
    remaining = (await session.execute(select(Chunk).where(
        Chunk.document_id == document_id))).scalars().all()
    assert [c.seq for c in remaining] == [99]
    document = await session.get(Document, document_id)
    assert document.current_job_id == new_job_id and document.index_status == "pending"


async def test_exact_identifier_query_gives_code_keyword_route_extra_weight(session):
    # 用户住在 control schema（Go 拥有），语料侧只存裸 actor id

    actor_id = "actor-coder"
    document = Document(uploaded_by=actor_id, doc_id="c" * 64, origin="web",
                        filename="code.pdf", mime="application/pdf", size_bytes=1)
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="o" * 64)
    session.add(job)
    await session.flush()
    text = "fetchUser"
    prose = Chunk(document_id=document.id, parse_job_id=job.id, seq=0, page_idx=0,
                  text=f"正文提到了 {text}", search_text=f"正文提到了 {text}",
                  text_tokenized=tokenized(f"正文提到了 {text}"), block_type="text")
    code = Chunk(document_id=document.id, parse_job_id=job.id, seq=1, page_idx=0,
                 text=f"client.{text}(user_id)", search_text=f"client.{text}(user_id)",
                 text_tokenized=code_tokenized(f"client.{text}(user_id)"), block_type="code")
    session.add_all([prose, code])
    await session.commit()

    hits = await MemoryIndex().search(
        session, vector=None, query="fetchUser", document_id=document.id,
        limit=2, candidates=8, min_similarity=0.45)
    assert [hit["block_type"] for hit in hits] == ["code", "text"]
    assert hits[0]["score"] > hits[1]["score"]


def test_generated_visual_description_is_labeled_in_answer_context():
    retrieval = Retrieval(hits=[{
        "page_idx": 0, "text": "图 1 延迟曲线", "derived_text": "80ms 后趋稳",
        "evidence_id": "source-e1",
    }])
    messages = build_messages("趋势如何？", retrieval, [], [])
    body = messages[-1]["content"][-1]["text"]
    assert "[生成理解，原子证据 source-e1]" in body
    assert "[原文/OCR]" in body
    assert "80ms 后趋稳" in body and "图 1 延迟曲线" in body


def test_exact_identifier_extraction_works_inside_natural_language_questions():
    assert exact_identifiers("Where is HttpRequestParser defined?") == ["httprequestparser"]
    assert exact_identifiers("Find std::vector<Result> and --skip-special-tokens") == [
        "std::vector<result>", "--skip-special-tokens"]
    assert exact_identifiers("Which page has evidence.content_digest or parse_job_id?") == [
        "evidence.content_digest", "parse_job_id"]
    assert exact_identifiers("Where can I find the identifier?") == []
