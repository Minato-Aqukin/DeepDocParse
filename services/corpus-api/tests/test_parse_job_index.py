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
        async def get_result(self, task_id):
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
    from tests.test_qa import _chat_sse
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
    respx.post(CHAT).mock(return_value=_chat_sse("bob original fact", cited=True))
    answer = await actor_client.post(f"/api/conversations/{created.json()['id']}/ask",
                                     json={"question": "bob original fact"}, headers=headers)
    assert answer.status_code == 200, answer.text
    evidence_ids = {row.id for row in (await session.execute(select(Evidence).where(
        Evidence.parse_job_id == b.id))).scalars()}
    assert any(eid in answer.text for eid in evidence_ids)
    private_ids = {row.id for row in (await session.execute(select(Evidence).where(
        Evidence.parse_job_id == a.id))).scalars()}
    assert not any(eid in answer.text for eid in private_ids)
