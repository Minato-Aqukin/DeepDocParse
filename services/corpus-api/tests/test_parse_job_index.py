"""Independent asset-owned index generations over one deduplicated document."""
import json
from datetime import timedelta

import httpx
import respx
from sqlalchemy import select

from ddp_corpus.archive import archive_job
from ddp_corpus.indexing import (
    _fail_if_current, _index_claimed, _renew_lease_once, claim_for_indexing,
    index_document, mark_index_pending,
)
from ddp_corpus.models import Chunk, Document, Evidence, ParseJob, Resource, ResourceVersion, utcnow
from tests.conftest import EMBEDDINGS


def layout(text):
    return {"layout_version": "ddp-layout/1", "engine": "borndigital", "code_detection": "native",
        "pdf_info": [{"page_idx": 0, "page_size": [100, 100], "para_blocks": [{
            "type": "text", "bbox": [0, 0, 90, 90], "lines": [{"spans": [{"content": text}]}]}]}]}


def embedding_mock():
    def respond(request):
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1] * 1024}
                                                 for i in range(count)]})
    return respx.post(EMBEDDINGS).mock(side_effect=respond)


async def pair(session, storage, *, archived=True):
    document = Document(uploaded_by="alice", organization_id="org-a", doc_id="a" * 64,
                        filename="shared.pdf", mime="application/pdf")
    session.add(document)
    await session.flush()
    result = []
    for number, (owner, org) in enumerate((("alice", "org-a"), ("bob", "org-b")), 1):
        resource = Resource(owner_id=owner, uploaded_by=owner, organization_id=org)
        session.add(resource)
        await session.flush()
        job = ParseJob(document_id=document.id, resource_id=resource.id, initiated_by=owner,
            engine="borndigital", options_hash=str(number), document_version=number,
            service_task_id=f"service-{owner}", status="succeeded" if archived else "running",
            result_prefix=f"parse-{owner}/" if archived else None,
            index_status="pending" if archived else "none")
        session.add(job)
        await session.flush()
        version = ResourceVersion(resource_id=resource.id, document_id=document.id,
                                   source_digest=document.doc_id, parse_job_id=job.id)
        session.add(version)
        await storage.put(f"parse-{owner}/layout.json", json.dumps(layout(f"{owner} original fact")).encode(),
                          "application/json")
        result.append((resource, job, version))
    document.current_job_id = result[0][1].id if archived else None
    await session.commit()
    return document, result[0], result[1]


@respx.mock
async def test_two_assets_both_ready_and_rebuild_preserves_other_evidence(session, app_state):
    document, (ra, a, _), (rb, b, _) = await pair(session, app_state.storage)
    embedding_mock()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=b.id) == 1
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 1
    await session.refresh(a)
    await session.refresh(b)
    assert a.index_status == b.index_status == "ready"
    chunks = list((await session.execute(select(Chunk))).scalars())
    a_chunk = next(c for c in chunks if c.parse_job_id == a.id)
    a_evidence = await session.get(Evidence, a_chunk.evidence_id)
    assert a_evidence.content == "alice original fact"
    assert {c.text for c in chunks} == {"alice original fact", "bob original fact"}
    await mark_index_pending(session, b.id)
    await session.commit()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=b.id) == 1
    assert await session.get(Chunk, a_chunk.id) is not None
    assert (await session.get(Evidence, a_evidence.id)).content == a_evidence.content
    from ddp_corpus.models import CorpusOutbox
    usage = list((await session.execute(select(CorpusOutbox).where(CorpusOutbox.type == "UsageRecorded"))).scalars())
    assert {(event.payload["actor_id"], event.organization_id) for event in usage if event.payload["kind"] == "embed"} == {
        ("alice", "org-a"), ("bob", "org-b")}

@respx.mock
async def test_takeover_retry_rebills_its_own_attempt_reindex_rebills_again(session, app_state):
    """每次真正干活的索引尝试都记一笔用量，重跑同一代会计不加行。

    业务键里带 claim 代次：租约接管（新代、重跑）为它自己的向量化工作记一笔，
    同一代的重试重放同一事件 ID 不加行，重建索引的新代再记一笔。代次不进键
    会把接管/重建的工作吞掉（少记账），事件 ID 随机则重试会重复扣配额。
    """
    from ddp_corpus.models import CorpusOutbox
    from ddp_corpus.usage import business_event_id
    from tests.conftest import usage_events

    document, (_, a, _), _ = await pair(session, app_state.storage)
    embedding_mock()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 1
    await session.refresh(a)
    first_generation = a.index_generation
    # Simulate the takeover shape: the lease holder stalls, a successor claims the
    # next generation and re-runs the attempt, then the queue redelivers the same
    # completion (same generation context) without adding a row.
    await mark_index_pending(session, a.id)
    await session.commit()
    await session.refresh(a)
    takeover_generation = a.index_generation
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 1
    await session.refresh(a)
    assert a.index_generation == takeover_generation + 1
    rerun_generation = a.index_generation
    a.index_status = "indexing"
    await session.commit()
    assert await _index_claimed(session, app_state.storage, app_state.http,
        document_id=document.id, job_id=a.id, generation=rerun_generation) == 1
    embed = await usage_events(session, kind="embed")
    assert [event["_event_id"] for event in embed] == [
        business_event_id(f"index:{a.id}:{first_generation}:embed"),
        business_event_id(f"index:{a.id}:{rerun_generation}:embed")]
    await mark_index_pending(session, a.id)
    await session.commit()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 1
    await session.refresh(a)
    embed = await usage_events(session, kind="embed")
    assert [event["_event_id"] for event in embed] == [
        business_event_id(f"index:{a.id}:{first_generation}:embed"),
        business_event_id(f"index:{a.id}:{rerun_generation}:embed"),
        business_event_id(f"index:{a.id}:{a.index_generation}:embed")]
    assert all(event.organization_id == "org-a" for event in
               list((await session.execute(select(CorpusOutbox).where(
                   CorpusOutbox.type == "UsageRecorded"))).scalars()))


@respx.mock
async def test_vision_usage_is_scoped_to_its_own_generation(session, app_state):
    """K1 视觉半：compile_vision 的业务键同样带 claim 代次。

    embed 腿已有 `test_takeover_retry_rebills_its_own_attempt_reindex_rebills_again`
    钉住；视觉腿走同一套记账但键不同 —— 只去掉 vision 键里的代次时
    embed 断言照样绿，只有这里变红。变异确认：把 indexing.py 的
    `index:{job}:{generation}:compile_vision` 改成 `index:{job}:compile_vision`，
    接管/重建的视觉工作会被吞掉（事件 ID 相同 → 不加行），本用例变红。
    """
    from ddp_corpus.usage import business_event_id
    from tests.conftest import CHAT, usage_events

    def visual_layout(text):
        return {"layout_version": "ddp-layout/1", "engine": "borndigital",
                "code_detection": "native",
                "pdf_info": [{"page_idx": 0, "page_size": [100, 100], "para_blocks": [{
                    "type": "figure", "bbox": [0, 0, 90, 90],
                    "lines": [{"spans": [{"content": text}]}]}]}]}

    document, (resource, job, version), _ = await pair(session, app_state.storage)
    await app_state.storage.put(
        f"{job.result_prefix}layout.json",
        json.dumps(visual_layout("latency chart")).encode(), "application/json")
    from tests.test_qa import _real_pdf
    document.object_key = "uploads/org-a/vision-source.pdf"
    await app_state.storage.put(document.object_key, _real_pdf(), "application/pdf")
    await session.commit()
    embedding_mock()
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={
        "model": "test-vision",
        "choices": [{"message": {"content": json.dumps({"description": "a chart"})}}]}))
    assert await index_document(
        session, app_state.storage, app_state.http, document.id, job_id=job.id) >= 1
    await session.refresh(job)
    first_generation = job.index_generation
    vision = await usage_events(session, kind="compile_vision")
    assert [event["_event_id"] for event in vision] == [
        business_event_id(f"index:{job.id}:{first_generation}:compile_vision")], vision

    await mark_index_pending(session, job.id)
    await session.commit()
    assert await index_document(
        session, app_state.storage, app_state.http, document.id, job_id=job.id) >= 1
    await session.refresh(job)
    second_generation = job.index_generation
    assert second_generation != first_generation
    vision = await usage_events(session, kind="compile_vision")
    assert [event["_event_id"] for event in vision] == [
        business_event_id(f"index:{job.id}:{first_generation}:compile_vision"),
        business_event_id(f"index:{job.id}:{second_generation}:compile_vision")], vision

@respx.mock
async def test_revoke_a_never_dispatches_b_still_indexes(session, app_state):
    document, (ra, a, _), (_, b, _) = await pair(session, app_state.storage)
    ra.publication = "withdrawn"
    await session.commit()
    calls = embedding_mock()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 0
    assert calls.call_count == 0
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=b.id) == 1
    await session.refresh(a)
    await session.refresh(b)
    assert a.index_status == "failed" and "withdrawn" in a.index_error
    assert b.index_status == "ready"


@respx.mock
async def test_late_generation_and_lease_are_keyed_to_job(session, app_state):
    from ddp_corpus.db import get_sessionmaker
    document, (_, a, _), (_, b, _) = await pair(session, app_state.storage)
    generation = await claim_for_indexing(session, document.id, job_id=a.id)
    await session.refresh(a)
    a.index_lease_until = utcnow() - timedelta(seconds=1)
    await session.commit()
    successor = await claim_for_indexing(session, document.id, job_id=a.id)
    assert successor == generation + 1
    b_generation = await claim_for_indexing(session, document.id, job_id=b.id)
    assert b_generation == 1
    assert not await _renew_lease_once(get_sessionmaker(), a.id, generation)
    assert await _renew_lease_once(get_sessionmaker(), b.id, b_generation)
    calls = embedding_mock()
    document_id, a_id = document.id, a.id
    assert await _index_claimed(session, app_state.storage, app_state.http,
        document_id=document.id, job_id=a.id, generation=generation) == 0
    await _fail_if_current(session, document_id, a_id, generation, "late failure")
    assert calls.call_count == 0
    await session.refresh(a)
    await session.refresh(b)
    assert a.index_generation == successor and a.index_status == "indexing" and a.index_error is None
    assert b.index_generation == b_generation and b.index_status == "indexing"


@respx.mock
async def test_reverse_callback_order_schedules_each_job_and_keeps_bindings(session, app_state):
    document, (_, a, va), (_, b, vb) = await pair(session, app_state.storage, archived=False)
    class Service:
        async def get_result(self, task_id, **kwargs):
            return {"layout_json": layout(task_id), "markdown": task_id, "images": []}
    service = Service()
    assert await archive_job(session, app_state.storage, service, b.id)
    assert await archive_job(session, app_state.storage, service, a.id)
    await session.refresh(a)
    await session.refresh(b)
    await session.refresh(document)
    assert a.index_status == b.index_status == "pending"
    assert document.current_job_id == b.id
    assert va.parse_job_id == a.id and vb.parse_job_id == b.id
    embedding_mock()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=a.id) == 1
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=b.id) == 1
    await session.refresh(a)
    await session.refresh(b)
    assert a.index_status == b.index_status == "ready"
    assert len(list((await session.execute(select(Chunk))).scalars())) == 2


@respx.mock
async def test_withdraw_between_embedding_batches_blocks_next_dispatch(session, app_state, monkeypatch):
    from ddp_corpus.config import settings
    from ddp_corpus.models import CorpusOutbox
    document, (resource, job, _), (_, other, _) = await pair(session, app_state.storage)
    value = layout("first atom")
    value["pdf_info"][0]["para_blocks"].append({"type": "text", "bbox": [0, 90, 90, 100],
        "lines": [{"spans": [{"content": "second atom"}]}]})
    await app_state.storage.put(f"{job.result_prefix}layout.json", json.dumps(value).encode(), "application/json")
    monkeypatch.setattr(settings, "embedding_batch_size", 1)
    async def withdraw_after_first(request):
        resource.publication = "withdrawn"
        await session.commit()
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    route = respx.post(EMBEDDINGS).mock(side_effect=withdraw_after_first)
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 0
    assert route.call_count == 1
    await session.refresh(job)
    await session.refresh(other)
    assert job.index_status == "failed" and other.index_status == "pending"
    assert not list((await session.execute(select(Chunk))).scalars())
    event = await session.scalar(select(CorpusOutbox).where(CorpusOutbox.type == "UsageRecorded"))
    assert event.organization_id == "org-a" and event.payload["requests"] == 1


@respx.mock
async def test_b_qa_uses_its_ready_job_when_a_cache_failed(actor_client, session, app_state, monkeypatch):
    from ddp_corpus.config import settings
    from tests.conftest import CHAT, actor_headers
    from tests.test_qa import _grounded_doc, _grounded_side_effect
    document, (resource_a, a, _), (resource_b, b, _) = await pair(session, app_state.storage)
    embedding_mock()
    for job in (a, b):
        await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id)
    resource_a.publication = "withdrawn"
    a.index_status = "failed"
    document.index_status = "failed"
    await session.commit()
    headers = actor_headers("bob", organization_id="org-b")
    created = await actor_client.post(f"/api/documents/{document.id}/conversations?resource_id={resource_b.id}", headers=headers)
    assert created.status_code == 201, created.text
    monkeypatch.setattr(settings, "qa_verify_parse", False)
    respx.post(CHAT).mock(side_effect=_grounded_side_effect(
        _grounded_doc(("bob original fact", [1]))))
    answer = await actor_client.post(f"/api/conversations/{created.json()['id']}/ask",
                                     json={"question": "bob original fact"}, headers=headers)
    assert answer.status_code == 200, answer.text
    evidence_ids = {row.id for row in (await session.execute(select(Evidence).where(
        Evidence.parse_job_id == b.id))).scalars()}
    assert any(eid in answer.text for eid in evidence_ids)
    private_ids = {row.id for row in (await session.execute(select(Evidence).where(
        Evidence.parse_job_id == a.id))).scalars()}
    assert not any(eid in answer.text for eid in private_ids)


@respx.mock
async def test_owned_private_origin_can_index_until_withdrawn(session, app_state):
    document, (resource, job, _), _ = await pair(session, app_state.storage)
    origin = Resource(owner_id="alice", uploaded_by="alice", organization_id="org-a")
    session.add(origin)
    await session.flush()
    resource.copied_from = origin.id
    await session.commit()
    calls = embedding_mock()

    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 1
    await session.refresh(job)
    assert job.index_status == "ready"
    dispatched = calls.call_count

    origin.publication = "withdrawn"
    await mark_index_pending(session, job.id)
    await session.commit()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 0
    await session.refresh(job)
    assert job.index_status == "failed"
    assert calls.call_count == dispatched


@respx.mock
async def test_another_owners_private_origin_blocks_index_dispatch(session, app_state):
    document, (resource, job, _), _ = await pair(session, app_state.storage)
    origin = Resource(owner_id="charlie", uploaded_by="charlie", organization_id="org-a")
    session.add(origin)
    await session.flush()
    resource.copied_from = origin.id
    await session.commit()
    calls = embedding_mock()

    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 0
    await session.refresh(job)
    assert job.index_status == "failed"
    assert calls.call_count == 0


@respx.mock
async def test_reindex_with_changed_page_label_keeps_history_row_and_citations(session, app_state):
    """重建索引时页码标签变了，历史证据行不动，旧出处仍指回当年那块。

    标签是**显示别名**（封面改版、前言增页都会漂），不是定位键：跟着新版面
    改写旧行等于替历史作证 —— 已发出去的引用会凭空换一页。断言分两半：
    旧 Evidence 行的标签保持原值；当年的引用仍 resolved 并回放当年的标签。
    """
    from ddp_corpus.evidence import load_citations, record_evidence

    document, (_, job, _), _ = await pair(session, app_state.storage)
    labelled = layout("labelled atom")
    labelled["pdf_info"][0]["printed_page_label"] = "iv"
    await app_state.storage.put(f"{job.result_prefix}layout.json",
                                json.dumps(labelled).encode(), "application/json")
    embedding_mock()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 1
    chunk = await session.scalar(select(Chunk).where(Chunk.parse_job_id == job.id))
    evidence_id = chunk.evidence_id
    assert (await session.get(Evidence, evidence_id)).printed_page_label == "iv"
    assert await record_evidence(session, [{
        "parse_job_id": job.id, "seq": chunk.seq, "evidence_id": evidence_id,
        "snippet": chunk.text}], source_kind="message", source_id="labelled-answer") == 1
    await session.commit()

    relabelled = layout("labelled atom")
    relabelled["pdf_info"][0]["printed_page_label"] = "v"
    await app_state.storage.put(f"{job.result_prefix}layout.json",
                                json.dumps(relabelled).encode(), "application/json")
    await mark_index_pending(session, job.id)
    await session.commit()
    assert await index_document(session, app_state.storage, app_state.http, document.id, job_id=job.id) == 1
    await session.refresh(job)
    assert job.index_status == "ready"
    # History row keeps the label it was cited with; the fresh chunk carries the new one.
    assert (await session.get(Evidence, evidence_id)).printed_page_label == "iv"
    fresh = await session.scalar(select(Chunk).where(Chunk.parse_job_id == job.id))
    assert fresh.printed_page_label == "v" and fresh.evidence_id == evidence_id
    cited = (await load_citations(session, source_kind="message",
                                  source_ids=["labelled-answer"]))["labelled-answer"]
    assert len(cited) == 1 and cited[0]["resolved"] is True
    assert cited[0]["evidence_id"] == evidence_id
    assert cited[0]["printed_page_label"] == "iv" and cited[0]["page_idx"] == 0
