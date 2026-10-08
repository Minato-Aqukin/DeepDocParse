"""Versioned Wiki invariants use actual routes and persistent SQL rows."""
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from ddp_corpus.models import (
    Chunk, ClaimEvidenceBinding, DependencyManifest, Document, Evidence, ParseJob, Resource,
    ResourceVersion, Wiki, WikiHumanEdit, WikiRevision, WikiWriteKey, utcnow,
)
from tests.conftest import ACTOR, CHAT, ORG, actor_headers


async def source(session, *, owner=ACTOR, publication="private", text="Original fact.",
                 organization=ORG):
    digest = hashlib.sha256(text.encode()).hexdigest()
    document = Document(uploaded_by=owner, organization_id=organization, doc_id=digest,
                        filename="manual.pdf")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options_hash="v1", status="succeeded")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    resource = Resource(owner_id=owner, uploaded_by=owner, organization_id=organization,
                        publication=publication)
    session.add(resource)
    await session.flush()
    version = ResourceVersion(resource_id=resource.id, document_id=document.id, parse_job_id=job.id,
                              source_digest=digest, version_no=1)
    evidence = Evidence(document_id=document.id, parse_job_id=job.id, seq=0, atom_key="text-0",
                        content=text, content_digest=digest, kind="text", page_idx=0,
                        bbox=[0, 0, 50, 50], page_size=[100, 100])
    session.add_all([version, evidence])
    await session.flush()
    # 与真实索引同形：原始 Evidence 由当前索引的 Chunk 指回（Wiki 只从这里选证）
    session.add(index_chunk(evidence))
    await session.commit()
    return resource, version, evidence, document


def index_chunk(evidence) -> Chunk:
    return Chunk(document_id=evidence.document_id, parse_job_id=evidence.parse_job_id,
                 seq=evidence.seq, page_idx=evidence.page_idx, text=evidence.content,
                 text_tokenized=evidence.content, evidence_id=evidence.id)


def body(resource, version, **kwargs):
    return {"title": "Manual", "sources": [{"resource_id": resource.id,
            "source_version_id": version.id}], **kwargs}


def model(evidence, *, title="Overview", cited=True):
    def respond(request):
        prompt = json.loads(request.content)
        assert prompt["max_tokens"] > 0
        assert prompt["response_format"]["type"] == "json_schema"
        assert prompt["response_format"]["json_schema"]["strict"] is True
        planning = "pages" in prompt["response_format"]["json_schema"]["schema"]["properties"]
        payload = {"pages": [{"title": title, "sections": ["Facts"], "references": [1],
                              "source_term": None}]} if planning else {
            "sections": [{"heading": "Facts", "sentences": [{"text": evidence.content,
                "evidence_ids": [evidence.id] if cited else ["invented-evidence"],
                "conflict_group": None}]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})
    return respx.post(CHAT).mock(side_effect=respond)


@respx.mock
async def test_fixed_manifest_human_edits_cas_rebuild_and_retry(actor_client, session):
    resource, version, evidence, _ = await source(session)
    calls = model(evidence)
    create_body = body(resource, version)
    created = await actor_client.post("/api/wikis", json=create_body, headers={"Idempotency-Key": "create"})
    assert created.status_code == 201, created.text
    first = created.json()
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    manifest = first["revision"]["dependency_manifest"][0]
    assert (manifest["resource_id"], manifest["source_version_id"], manifest["parse_revision"],
            manifest["evidence_id"], manifest["excerpt_digest"]) == (
                resource.id, version.id, version.parse_job_id, evidence.id, evidence.content_digest)
    # 这里没有向量化服务：选证只走关键词路，必须在报告里说出来
    assert first["revision"]["limits"]["evidence_selection"] == {
        "total_original_evidence": 1, "selected_evidence": 1, "omitted_evidence": 0,
        "complete": True, "ranking_degraded": "embedding_unavailable",
        "sources": [{"resource_id": resource.id, "source_version_id": version.id,
                     "total_original_evidence": 1, "selected_evidence": 1}]}
    retried = await actor_client.post("/api/wikis", json=create_body, headers={"Idempotency-Key": "create"})
    assert retried.json()["revision"]["id"] == revision_id and calls.call_count == 2
    conflict = await actor_client.post("/api/wikis", json={**create_body, "title": "Other"},
                                      headers={"Idempotency-Key": "create"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    key = first["revision"]["pages"][0]["page_key"]
    edit_body = {"base_revision_id": revision_id, "paragraphs": [{"id": "note", "text": "My note."}]}
    edited = await actor_client.patch(f"/api/wikis/{wiki_id}/pages/{key}", json=edit_body,
                                      headers={"Idempotency-Key": "edit"})
    assert edited.status_code == 201, edited.text
    second = edited.json()["revision"]
    stale_write = await actor_client.patch(f"/api/wikis/{wiki_id}/pages/{key}", json=edit_body,
                                          headers={"Idempotency-Key": "stale"})
    assert stale_write.status_code == 409
    historical = (await actor_client.get(f"/api/wikis/{wiki_id}/revisions/{revision_id}")).json()
    assert historical["revision"]["pages"][0]["human_paragraphs"] == []
    rebuilt = await actor_client.post(f"/api/wikis/{wiki_id}/revisions",
        json={**create_body, "base_revision_id": second["id"]}, headers={"Idempotency-Key": "rebuild"})
    assert rebuilt.status_code == 201, rebuilt.text
    assert rebuilt.json()["revision"]["pages"][0]["human_paragraphs"][0]["text"] == "My note."
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 3
    assert await session.scalar(select(func.count()).select_from(WikiHumanEdit)) == 1
    assert await session.scalar(select(func.count()).select_from(WikiWriteKey)) == 3


@respx.mock
async def test_source_with_unsucceeded_parse_is_refused_before_any_model_call(actor_client, session):
    """A bound parse_job_id is not a successful parse; the server must not trust the chooser."""
    resource, version, evidence, _ = await source(session)
    calls = model(evidence)
    job = await session.get(ParseJob, version.parse_job_id)
    for status in ("running", "failed"):
        job.status = status
        await session.commit()
        refused = await actor_client.post("/api/wikis", json=body(resource, version),
                                          headers={"Idempotency-Key": f"parse-{status}"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "wiki_source_unavailable"
    assert calls.call_count == 0
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


def scripted(plan_pages, sentences):
    def respond(request):
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        payload = ({"pages": plan_pages} if "pages" in schema["properties"]
                   else {"sections": [{"heading": "Facts", "sentences": sentences}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})
    return respx.post(CHAT).mock(side_effect=respond)


@respx.mock
async def test_claims_citing_the_reference_number_bind_to_that_evidence(actor_client, session):
    """The page prompt shows each evidence with its planner `reference` and its `evidence_id`.
    Qwen3-4B cited references ("4") and every SRAM claim came back unsupported although it
    named the right block (2026-09-25, §4F retest). A known reference resolves; others drop."""
    resource, version, evidence, _ = await source(session)
    page = {"title": "Overview", "sections": ["Facts"], "references": [1], "source_term": None}
    scripted([page], [{"text": evidence.content, "evidence_ids": ["1"], "conflict_group": None},
                      {"text": "Made up.", "evidence_ids": ["7"], "conflict_group": None}])
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "by-reference"})
    assert created.status_code == 201, created.text
    claims = [claim for page in created.json()["revision"]["pages"]
              for section in page["generated_sections"] for claim in section["sentences"]]
    assert [(claim["evidence_ids"], claim["unsupported"]) for claim in claims] == [
        ([evidence.id], False), ([], True)]
    bindings = (await session.execute(select(ClaimEvidenceBinding.claim_id,
                                             ClaimEvidenceBinding.evidence_id))).all()
    assert [tuple(row) for row in bindings] == [(claims[0]["id"], evidence.id)]


@respx.mock
async def test_permission_rechecks_and_public_revision_title(actor_client, session):
    resource, version, evidence, _ = await source(session, publication="published")
    model(evidence)
    first = (await actor_client.post("/api/wikis", json=body(resource, version),
        headers={"Idempotency-Key": "first"})).json()
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    assert (await actor_client.get(f"/api/wikis/{wiki_id}", headers=actor_headers("bob"))).status_code == 404
    assert (await actor_client.post(f"/api/wikis/{wiki_id}/publish",
        json={"base_revision_id": revision_id})).status_code == 200
    await actor_client.post(f"/api/wikis/{wiki_id}/revisions",
        json=body(resource, version, title="Secret draft title", base_revision_id=revision_id),
        headers={"Idempotency-Key": "private-draft"})
    public = await actor_client.get(f"/api/wikis/{wiki_id}", headers=actor_headers("bob"))
    assert public.status_code == 200 and public.json()["wiki"]["title"] == "Manual"
    listing = await actor_client.get("/api/wikis", headers=actor_headers("bob"))
    assert "Secret draft title" not in listing.text
    resource.publication = "private"
    await session.commit()
    assert (await actor_client.get(f"/api/wikis/{wiki_id}", headers=actor_headers("bob"))).status_code == 404
    assert (await actor_client.get(f"/api/wikis/{wiki_id}")).status_code == 200


@respx.mock
async def test_exact_parse_binding_stale_and_missing_binding(actor_client, session):
    resource, version, evidence, document = await source(session)
    newer = ParseJob(document_id=document.id, engine="borndigital", options_hash="v2", document_version=2)
    session.add(newer)
    await session.flush()
    document.current_job_id = newer.id
    await session.commit()
    model(evidence)
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                       headers={"Idempotency-Key": "fixed"})
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["revision"]["dependency_manifest"][0]["parse_revision"] == evidence.parse_job_id
    assert result["revision"]["stale"] is False
    next_doc = Document(uploaded_by=ACTOR, organization_id=ORG, doc_id="b" * 64, filename="v2.pdf")
    session.add(next_doc)
    await session.flush()
    session.add(ResourceVersion(resource_id=resource.id, document_id=next_doc.id,
                                version_no=2, source_digest="b" * 64))
    await session.commit()
    stale = (await actor_client.get(f"/api/wikis/{result['wiki']['id']}")).json()["revision"]
    assert stale["stale"] is True
    assert "source_version_changed" in next(iter(stale["stale_reasons"].values()))
    version.parse_job_id = None
    await session.commit()
    unavailable = await actor_client.post("/api/wikis", json=body(resource, version),
                                           headers={"Idempotency-Key": "missing"})
    assert unavailable.status_code == 409


@respx.mock
async def test_rebuild_on_the_newest_version_clears_stale_and_keeps_human_paragraphs(
        actor_client, session):
    """资料更新 → 过期 → 在新版本上重建：人工段落保留、页面不再过期、可以发布，旧修订仍指旧版本。
    重建把旧页的依赖随人工段落一起带上（发布仍要查它们）；逐行判版本时，这一页在最新版本上
    重建后也永远"待更新"、永远发布不了（D 阶段浏览器实测）。"""
    resource, v1, e1, _ = await source(session, publication="published", text="Reset delay is 17 ms.")
    model(e1)
    created = (await actor_client.post("/api/wikis", json=body(resource, v1),
                                       headers={"Idempotency-Key": "v1"})).json()
    wiki_id, first = created["wiki"]["id"], created["revision"]
    edited = (await actor_client.patch(
        f"/api/wikis/{wiki_id}/pages/{first['pages'][0]['page_key']}",
        json={"base_revision_id": first["id"], "paragraphs": [{"id": "n", "text": "Wait longer."}]},
        headers={"Idempotency-Key": "note"})).json()["revision"]

    text = "Reset delay is 23 ms."
    digest = hashlib.sha256(text.encode()).hexdigest()
    doc2 = Document(uploaded_by=ACTOR, organization_id=ORG, doc_id=digest, filename="manual.pdf")
    session.add(doc2)
    await session.flush()
    job2 = ParseJob(document_id=doc2.id, engine="borndigital", options_hash="v1", status="succeeded")
    session.add(job2)
    await session.flush()
    v2 = ResourceVersion(resource_id=resource.id, document_id=doc2.id, parse_job_id=job2.id,
                         source_digest=digest, version_no=2)
    e2 = Evidence(document_id=doc2.id, parse_job_id=job2.id, seq=0, atom_key="text-0", content=text,
                  content_digest=digest, kind="text", page_idx=0, bbox=[0, 0, 50, 50],
                  page_size=[100, 100])
    session.add_all([v2, e2])
    await session.flush()
    session.add(index_chunk(e2))
    await session.commit()
    assert (await actor_client.get(f"/api/wikis/{wiki_id}")).json()["revision"]["stale"] is True

    model(e2)
    rebuilt = await actor_client.post(f"/api/wikis/{wiki_id}/revisions", json=body(
        resource, v2, base_revision_id=edited["id"]), headers={"Idempotency-Key": "v2"})
    assert rebuilt.status_code == 201, rebuilt.text
    revision = rebuilt.json()["revision"]
    assert revision["pages"][0]["human_paragraphs"][0]["text"] == "Wait longer."
    assert {d["source_version_id"] for d in revision["dependency_manifest"]} == {v1.id, v2.id}
    assert revision["stale"] is False
    published = await actor_client.post(f"/api/wikis/{wiki_id}/publish",
                                        json={"base_revision_id": revision["id"]})
    assert published.status_code == 200, published.text
    old = (await actor_client.get(f"/api/wikis/{wiki_id}/revisions/{first['id']}")).json()["revision"]
    assert old["stale"] is True
    assert {d["source_version_id"] for d in old["dependency_manifest"]} == {v1.id}


@respx.mock
async def test_private_context_cannot_launder_via_public_citation(actor_client, session):
    private, pv, pe, _ = await source(session, text="Private fact.")
    public, uv, ue, _ = await source(session, publication="published", text="Public fact.")
    model(ue)
    request = body(private, pv)
    request["sources"].append({"resource_id": public.id, "source_version_id": uv.id})
    created = await actor_client.post("/api/wikis", json=request, headers={"Idempotency-Key": "mixed"})
    assert created.status_code == 502, created.text
    assert await session.scalar(select(func.count()).select_from(Wiki)) == 0
    scripted([{"title": "Overview", "sections": ["Facts"], "references": [1, 2], "source_term": None}],
             [{"text": e.content, "evidence_ids": [e.id], "conflict_group": None} for e in (pe, ue)])
    created = await actor_client.post("/api/wikis", json=request, headers={"Idempotency-Key": "mixed"})
    assert created.status_code == 201
    result = created.json()
    assert {d["resource_id"] for d in result["revision"]["dependency_manifest"]} == {private.id, public.id}
    denied = await actor_client.post(f"/api/wikis/{result['wiki']['id']}/publish",
                                     json={"base_revision_id": result["revision"]["id"]})
    assert denied.status_code == 403
    bad = await actor_client.post("/api/wikis", json=body(public, pv),
                                  headers={"Idempotency-Key": "mismatch"})
    assert bad.status_code == 404


@respx.mock
async def test_revoke_between_model_calls_stops_next_request(actor_client, session):
    resource, version, _, _ = await source(session, owner="publisher", publication="published")
    async def revoke(request):
        resource.publication = "private"
        await session.commit()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "pages": [{"title": "Overview", "sections": ["Facts"], "references": [1],
                       "source_term": None}]})}, "finish_reason": "stop"}]})
    calls = respx.post(CHAT).mock(side_effect=revoke)
    result = await actor_client.post("/api/wikis", json=body(resource, version),
                                     headers={"Idempotency-Key": "revocation"})
    assert result.status_code == 404
    assert calls.call_count == 1
    assert await session.scalar(select(func.count()).select_from(Wiki)) == 0


@respx.mock
async def test_budget_generated_evidence_and_failure_leave_no_partial_revision(actor_client, session):
    resource, version, evidence, _ = await source(session)
    evidence.derived_from = evidence.id
    await session.commit()
    calls = model(evidence)
    failed = await actor_client.post("/api/wikis", json=body(resource, version),
                                     headers={"Idempotency-Key": "derived"})
    assert failed.status_code == 409 and calls.call_count == 0
    evidence.derived_from = None
    await session.commit()
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps({"pages": [{"title": "A"}, {"title": "B"}]})}, "finish_reason": "stop"}]}))
    too_many = await actor_client.post("/api/wikis", json=body(resource, version, max_pages=1),
                                       headers={"Idempotency-Key": "budget"})
    assert too_many.status_code == 409 and too_many.json()["error"]["code"] == "wiki_budget_exceeded"
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0
    assert await session.scalar(select(func.count()).select_from(DependencyManifest)) == 0


async def long_source(session, *, count=4, topic="watchdog timer triggers controller reset"):
    return await source_with(session, [
        f"{topic} detail {seq}." if seq < 2 else f"Unrelated appendix filler {seq}."
        for seq in range(count)])


async def source_with(session, texts: list[str], *, kinds: list[str] | None = None):
    kinds = list(kinds) if kinds is not None else ["text"] * len(texts)
    assert len(kinds) == len(texts), "kinds must align with texts"
    digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
    document = Document(uploaded_by=ACTOR, organization_id=ORG, doc_id=digest, filename="manual.pdf")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options_hash="v1", status="succeeded")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    resource = Resource(owner_id=ACTOR, uploaded_by=ACTOR, organization_id=ORG, publication="private")
    session.add(resource)
    await session.flush()
    version = ResourceVersion(resource_id=resource.id, document_id=document.id, parse_job_id=job.id,
                              source_digest=digest, version_no=1)
    session.add(version)
    await session.flush()
    rows = []
    for seq, (text, kind) in enumerate(zip(texts, kinds)):
        row = Evidence(document_id=document.id, parse_job_id=job.id, seq=seq, atom_key=f"text-{seq}",
                       content=text, content_digest=hashlib.sha256(text.encode()).hexdigest(),
                       kind=kind, page_idx=seq, bbox=[0, 0, 50, 50], page_size=[100, 100])
        session.add(row)
        rows.append(row)
    await session.flush()
    session.add_all([index_chunk(row) for row in rows])
    await session.commit()
    return resource, version, rows


def topic_model(rows, *, relations=()):
    def respond(request):
        prompt = json.loads(request.content)
        assert prompt["response_format"]["json_schema"]["strict"] is True
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "pages" in properties:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            references = [item["reference"] for item in context
                          if item["evidence_id"] in {row.id for row in rows}]
            payload = {"pages": [
                {"title": "Controller reset", "sections": ["Reset"], "references": references,
                 "source_term": "controller reset"},
                {"title": "Watchdog settings", "sections": ["Watchdog"], "references": references,
                 "source_term": "watchdog"},
            ]}
        elif "selected_relations" in properties:
            payload = {"selected_relations": list(relations)}
        else:
            payload = {"sections": [{"heading": "Facts", "sentences": [
                {"text": rows[0].content, "evidence_ids": [rows[0].id], "conflict_group": None},
                {"text": rows[1].content, "evidence_ids": [rows[1].id], "conflict_group": None}]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})
    return respx.post(CHAT).mock(side_effect=respond)


@respx.mock
async def test_bounded_selection_stores_source_grounded_relations(actor_client, session):
    resource, version, evidence, _ = await source(
        session, text="Controller reset uses watchdog settings and status register coordination.")
    available_tokens = 2048

    def respond(request):
        nonlocal available_tokens
        prompt = json.loads(request.content)
        available_tokens -= prompt["max_tokens"]
        if available_tokens < 0:
            return httpx.Response(429, json={"error": {"message": "Completion allowance exhausted"}})
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "pages" in properties:
            payload = {"pages": [
                {"title": title, "sections": ["Facts"], "references": [1], "source_term": term}
                for title, term in [
                    ("Controller reset", "Controller reset"),
                    ("Watchdog settings", "watchdog settings"),
                    ("Status register", "status register"),
                ]
            ]}
        elif "selected_relations" in properties:
            candidates = json.loads(prompt["messages"][1]["content"])["candidates"]
            payload = {"selected_relations": [candidate["id"] for candidate in candidates]}
        else:
            payload = {"sections": [{"heading": "Facts", "sentences": [{
                "text": evidence.content, "evidence_ids": [evidence.id], "conflict_group": None,
            }]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset, watchdog settings and status register",
        max_pages=3, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "relations"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    titles = {page["page_key"]: page["title"] for page in revision["pages"]}
    assert {(titles[edge["subject_id"]], titles[edge["object_id"]])
            for edge in revision["relations"]} == {
        ("Controller reset", "Watchdog settings"),
        ("Controller reset", "Status register"),
        ("Watchdog settings", "Status register"),
    }
    for edge in revision["relations"]:
        assert edge["predicate"] == evidence.content
        assert edge["evidence_ids"] == [evidence.id]

    wiki_id = created.json()["wiki"]["id"]
    edited = await actor_client.patch(
        f"/api/wikis/{wiki_id}/pages/{revision['pages'][0]['page_key']}",
        json={"base_revision_id": revision["id"],
              "paragraphs": [{"id": "operator-note", "text": "Check these settings before startup."}]},
        headers={"Idempotency-Key": "relation-human-edit"})
    assert edited.status_code == 201, edited.text
    assert edited.json()["revision"]["relations"] == revision["relations"]
    historical = await actor_client.get(f"/api/wikis/{wiki_id}/revisions/{revision['id']}")
    assert historical.json()["revision"]["relations"] == revision["relations"]


@respx.mock
async def test_bounded_selection_reports_omitted_coverage(actor_client, session):
    resource, version, rows = await long_source(session)
    topic_model(rows)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "bounded"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert revision["limits"]["evidence_selection"] == {
        "total_original_evidence": 4, "selected_evidence": 2, "omitted_evidence": 2,
        "complete": False, "ranking_degraded": "embedding_unavailable",
        "sources": [{"resource_id": resource.id, "source_version_id": version.id,
                     "total_original_evidence": 4, "selected_evidence": 2}]}
    assert {d["evidence_id"] for d in revision["dependency_manifest"]} == {rows[0].id, rows[1].id}
    assert {c for page in revision["pages"] for section in page["generated_sections"]
            for claim in section["sentences"] for c in claim["evidence_ids"]} == {rows[0].id, rows[1].id}


@respx.mock
async def test_selection_ignores_evidence_superseded_by_an_index_rebuild(actor_client, session):
    """重建索引后旧 Evidence 行留给历史出处，但不能再当 Wiki 候选：真栈上 Pico 474 行里
    只有 162 行是当前索引，旧行带整页 bbox、同文不同 ID，覆盖数也被放大到 8319。"""
    resource, version, rows = await long_source(session, count=2)
    text = "watchdog timer triggers controller reset detail 0. (old whole-page chunk)"
    superseded = Evidence(document_id=rows[0].document_id, parse_job_id=rows[0].parse_job_id, seq=0,
                          atom_key="source:0:old", content=text, kind="text", page_idx=0,
                          content_digest=hashlib.sha256(text.encode()).hexdigest(),
                          bbox=[0, 0, 100, 100], page_size=[100, 100])
    session.add(superseded)
    await session.commit()
    topic_model(rows)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=10, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "current-index"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert superseded.id not in {d["evidence_id"] for d in revision["dependency_manifest"]}
    assert revision["limits"]["evidence_selection"]["total_original_evidence"] == 2

@respx.mock
async def test_freeze_sources_keeps_every_contract_block_type(actor_client, session):
    """Wiki 候选按契约块类型词汇表过滤：figure/equation/list 必须留下，契约外的不算。"""
    from ddp_contracts import BLOCK_TYPE_VALUES

    texts = [f"Controller reset detail in {kind} block {seq}."
             for seq, kind in enumerate([*BLOCK_TYPE_VALUES, "image"])]
    resource, version, rows = await source_with(
        session, texts, kinds=[*BLOCK_TYPE_VALUES, "image"])
    phantom = rows[-1]
    assert phantom.kind == "image"
    assert phantom.kind not in BLOCK_TYPE_VALUES
    topic_model(rows[:len(BLOCK_TYPE_VALUES)], relations=())
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=len(BLOCK_TYPE_VALUES), max_input_chars=12000,
        max_output_tokens=2048), headers={"Idempotency-Key": "contract-block-types"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selected = {d["evidence_id"] for d in revision["dependency_manifest"]}
    assert {row.id for row in rows[:len(BLOCK_TYPE_VALUES)]} <= selected
    assert phantom.id not in selected
    assert revision["limits"]["evidence_selection"]["total_original_evidence"] == len(BLOCK_TYPE_VALUES)
    assert {"figure", "equation", "list"} <= {
        row.kind for row in rows if row.id in selected}


@respx.mock
async def test_sources_take_turns_so_a_repeated_title_word_cannot_fill_the_budget(
        actor_client, session):
    """双源 Wiki：一个来源每块都重复标题里的词（Pico 页眉），也不能占满名额、
    把两个来源各自最相关的那条挤掉。真栈上 Pico 拿了 24 个名额里的 23 个。"""
    pico, pico_version, pico_rows = await source_with(session, [
        *(f"Raspberry Pi Pico RP2040 datasheet page {n}" for n in range(6)),
        "RP2040 provides 264 kB of SRAM."])
    esp, esp_version, esp_rows = await source_with(session, [
        *(f"Series datasheet page {n}" for n in range(6)), "ESP32 has 520 KB of on-chip SRAM."])
    facts = [pico_rows[-1], esp_rows[-1]]
    topic_model(facts)
    request = body(pico, pico_version, title="Raspberry Pi Pico RP2040 SRAM compared with ESP32 on-chip SRAM",
                   max_pages=2, max_evidence=4, max_input_chars=12000, max_output_tokens=2048)
    request["sources"].append({"resource_id": esp.id, "source_version_id": esp_version.id})
    created = await actor_client.post("/api/wikis", json=request, headers={"Idempotency-Key": "turns"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert [s["selected_evidence"] for s in revision["limits"]["evidence_selection"]["sources"]] == [2, 2]
    assert {fact.id for fact in facts} <= {d["evidence_id"] for d in revision["dependency_manifest"]}


@respx.mock
async def test_evidence_backlinks_list_the_visible_wiki_revision_claims(actor_client, session):
    """版本化 Wiki 的结论绑定不在 citations 表里，证据的反链曾经完全看不到它们。
    只算读者从 Wiki 本身能读到的那一版：属主看当前修订，其他人看已发布修订。"""
    resource, version, evidence, _ = await source(session, publication="published")
    model(evidence)
    created = (await actor_client.post("/api/wikis", json=body(resource, version),
                                       headers={"Idempotency-Key": "backlinks"})).json()
    wiki_id, first = created["wiki"]["id"], created["revision"]
    page_key = first["pages"][0]["page_key"]
    edited = (await actor_client.patch(f"/api/wikis/{wiki_id}/pages/{page_key}", json={
        "base_revision_id": first["id"], "paragraphs": [{"id": "n", "text": "Note."}]},
        headers={"Idempotency-Key": "backlinks-edit"})).json()["revision"]
    path = f"/api/evidence/{evidence.id}/backlinks"

    mine = [item for item in (await actor_client.get(path)).json()["backlinks"]
            if item["source_kind"] == "wiki_claim"]
    assert [(item["label"], item["revision_id"], item["wiki_id"]) for item in mine] == [
        (evidence.content, edited["id"], wiki_id)], "只算当前修订，历史修订不重复出现"
    bob = actor_headers("bob")
    assert not [item for item in (await actor_client.get(path, headers=bob)).json()["backlinks"]
                if item["source_kind"] == "wiki_claim"], "未发布的 Wiki 不能从反链泄露给别人"
    published = await actor_client.post(f"/api/wikis/{wiki_id}/publish",
                                        json={"base_revision_id": edited["id"]})
    assert published.status_code == 200, published.text
    theirs = [item for item in (await actor_client.get(path, headers=bob)).json()["backlinks"]
              if item["source_kind"] == "wiki_claim"]
    assert [item["revision_id"] for item in theirs] == [edited["id"]]


@respx.mock
async def test_bounded_selection_rejects_unsupported_and_revoked_sources(actor_client, session):
    resource, version, rows = await long_source(session, count=2)
    topic_model(rows)
    bad_body = body(resource, version, title="Controller reset delay and watchdog settings",
                    max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048)
    bad_body["sources"][0]["source_version_id"] = "missing-version"
    bad = await actor_client.post("/api/wikis", json=bad_body,
                                  headers={"Idempotency-Key": "unsupported"})
    assert bad.status_code == 404
    resource2, version2, rows2 = await long_source(session, count=2, topic="second manual topic")
    topic_model(rows2)
    request = body(resource, version, title="Controller reset delay and watchdog settings",
                   max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048)
    request["sources"].append({"resource_id": resource2.id, "source_version_id": version2.id})
    resource2.publication = "withdrawn"
    await session.commit()
    # Withdrawn second source is still resolved (never silently skipped): its
    # authorization fails closed before any selection or model call.
    revoked = await actor_client.post("/api/wikis", json=request, headers={"Idempotency-Key": "revoked"})
    assert revoked.status_code == 409
    assert revoked.json()["error"]["code"] == "wiki_source_unavailable"


@respx.mock
async def test_bounded_selection_keeps_human_paragraphs(actor_client, session):
    resource, version, rows = await long_source(session, count=2)
    topic_model(rows)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "human"})
    assert created.status_code == 201, created.text
    first = created.json()
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    key = first["revision"]["pages"][0]["page_key"]
    edited = await actor_client.patch(f"/api/wikis/{wiki_id}/pages/{key}",
        json={"base_revision_id": revision_id, "paragraphs": [{"id": "note", "text": "Keep me."}]},
        headers={"Idempotency-Key": "human-edit"})
    assert edited.status_code == 201, edited.text
    rebuilt = await actor_client.post(f"/api/wikis/{wiki_id}/revisions", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048,
        base_revision_id=edited.json()["revision"]["id"]), headers={"Idempotency-Key": "human-rebuild"})
    assert rebuilt.status_code == 201, rebuilt.text
    assert rebuilt.json()["revision"]["pages"][0]["human_paragraphs"][0]["text"] == "Keep me."


@respx.mock
async def test_invalid_relation_selection_cannot_become_an_empty_graph(actor_client, session):
    resource, version, rows = await long_source(session, count=2)
    topic_model(rows, relations=[999])
    response = await actor_client.post("/api/wikis", json=body(resource, version, max_pages=2),
                                       headers={"Idempotency-Key": "invalid-relation"})
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "wiki_generation_failed"
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


@respx.mock
async def test_bounded_selection_cannot_launder_an_omitted_private_source(actor_client, session):
    public, public_version, rows = await long_source(session, topic="Controller reset")
    public.publication = "published"
    await session.commit()
    private, private_version, _, _ = await source(session, text="Private appendix details.")
    model(rows[0])
    request = body(public, public_version, title="Controller reset", max_evidence=1)
    request["sources"].append({"resource_id": private.id, "source_version_id": private_version.id})
    headers = {"Idempotency-Key": "source-coverage"}
    denied = await actor_client.post("/api/wikis", json=request, headers=headers)
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "wiki_budget_exceeded"
    assert await session.scalar(select(func.count()).select_from(Wiki)) == 0

    request["max_evidence"] = 2
    created = await actor_client.post("/api/wikis", json=request, headers=headers)
    assert created.status_code == 502, created.text
    assert await session.scalar(select(func.count()).select_from(Wiki)) == 0
    def cover_selected(request):
        prompt = json.loads(request.content)
        context = json.loads(prompt["messages"][-1]["content"])["evidence"]
        planning = "pages" in prompt["response_format"]["json_schema"]["schema"]["properties"]
        payload = {"pages": [{"title": "Overview", "sections": ["Facts"], "references": [1, 2],
                             "source_term": None}]} if planning else {
            "sections": [{"heading": "Facts", "sentences": [
                {"text": e["text"], "evidence_ids": [e["evidence_id"]], "conflict_group": None}
                for e in context]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                     "finish_reason": "stop"}]})
    respx.post(CHAT).mock(side_effect=cover_selected)
    created = await actor_client.post("/api/wikis", json=request, headers=headers)
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert {item["resource_id"] for item in revision["dependency_manifest"]} == {public.id, private.id}
    assert all(item["selected_evidence"] == 1
               for item in revision["limits"]["evidence_selection"]["sources"])
    published = await actor_client.post(f"/api/wikis/{created.json()['wiki']['id']}/publish",
        json={"base_revision_id": revision["id"]})
    assert published.status_code == 403, published.text
    assert published.json()["error"]["code"] == "wiki_source_permission"


def legacy_model(evidence):
    def respond(request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        if "关系抽取" in prompt:
            output = {"entities": [{"name": "A"}, {"name": "B"}], "relations": [{
                "subject": "A", "predicate": "uses", "object": "B", "evidence_ids": [evidence.id]}]}
        elif "不写正文" in prompt:
            output = {"entries": [{"entity": "A", "sections": ["Facts"]}]}
        else:
            output = {"sections": [{"heading": "Facts", "sentences": [{
                "text": "A uses B.", "evidence_ids": [evidence.id]}]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})
    return respx.post(CHAT).mock(side_effect=respond)


@respx.mock
async def test_legacy_generators_isolated_by_author_and_exact_resource(
        actor_client, session, app_state):
    """Author/exact-resource isolation of generated knowledge (build route gone).

    POST /knowledge/build now returns 410 (legacy_wiki_build_removed); the same
    isolation is seeded through the generate() library function with per-author
    providers, and reads stay isolated by author + exact resource binding.
    """
    from ddp_corpus.deps import Actor
    from ddp_corpus.knowledge import generate as generate_knowledge
    from ddp_corpus.models import KnowledgeEntity
    resource, version, evidence, document = await source(session, publication="published")
    legacy_model(evidence)
    gone = await actor_client.post("/api/knowledge/build", json={"evidence_ids": [evidence.id]})
    assert gone.status_code == 410, gone.text
    assert gone.json()["error"]["code"] == "legacy_wiki_build_removed"
    alice_actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    alice_provider = {"scope_key": "legacy-alice", "generated_by": ACTOR,
                      "organization_id": ORG, "kind": "knowledge_generation",
                      "source_bindings": [{"resource_id": resource.id,
                                           "source_version_id": version.id,
                                           "document_id": document.id,
                                           "parse_revision": evidence.parse_job_id}],
                      "input_document_ids": [document.id]}
    alice_result = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=alice_provider, actor=alice_actor)
    assert alice_result["status"] == "ok", alice_result
    alice_wiki = (await actor_client.get("/api/wiki")).json()[0]
    # A public original does not publish its author's generated draft.
    assert (await actor_client.get("/api/wiki", headers=actor_headers("bob"))).json() == []
    bob_actor = Actor(id="bob", kind="user", organization_id=ORG, role="contributor")
    bob_provider = {"scope_key": "legacy-bob", "generated_by": "bob",
                    "organization_id": ORG, "kind": "knowledge_generation",
                    "source_bindings": [{"resource_id": resource.id,
                                         "source_version_id": version.id,
                                         "document_id": document.id,
                                         "parse_revision": evidence.parse_job_id}],
                    "input_document_ids": [document.id]}
    bob_result = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=bob_provider, actor=bob_actor)
    assert bob_result["status"] == "ok", bob_result
    bob_wiki = (await actor_client.get("/api/wiki", headers=actor_headers("bob"))).json()[0]
    assert bob_wiki["id"] != alice_wiki["id"]
    assert (await actor_client.get("/api/wiki/A")).json()["entry"]["id"] == alice_wiki["id"]
    assert (await actor_client.get("/api/wiki/A", headers=actor_headers("bob"))).json()["entry"]["id"] == bob_wiki["id"]
    assert await session.scalar(select(func.count()).select_from(KnowledgeEntity)) == 4
    # A second same-content resource cannot revive an artifact bound to a revoked first one.
    mirror = Resource(owner_id="bob", uploaded_by="bob", organization_id=ORG, publication="published")
    session.add(mirror)
    await session.flush()
    session.add(ResourceVersion(resource_id=mirror.id, document_id=document.id,
        parse_job_id=version.parse_job_id, source_digest=version.source_digest, version_no=1))
    resource.publication = "private"
    await session.commit()
    assert (await actor_client.get("/api/wiki", headers=actor_headers("bob"))).json() == []
    assert (await actor_client.get(f"/api/wiki/{bob_wiki['id']}", headers=actor_headers("bob"))).status_code == 404
    assert (await actor_client.get("/api/knowledge/graph", headers=actor_headers("bob"))).json()["edges"] == []
    assert (await actor_client.get("/api/wiki")).json()[0]["id"] == alice_wiki["id"]


@respx.mock
async def test_legacy_backlinks_and_reviews_do_not_expose_other_authors(actor_client, session):
    from ddp_corpus.models import (
        Assertion, Citation, Conversation, ExtractionItem, ExtractionRun, Message,
    )
    resource, version, evidence, document = await source(session, publication="published")
    conversation = Conversation(actor_id="bob", organization_id=ORG, document_id=document.id,
                                resource_id=resource.id)
    run = ExtractionRun(actor_id="bob", organization_id=ORG, name="private run", schema_json={},
                         resource_context={"resources": {document.id: resource.id}, "principal_id": "bob"})
    session.add_all([conversation, run])
    await session.flush()
    message = Message(conversation_id=conversation.id, role="assistant", content="Bob private question")
    item = ExtractionItem(run_id=run.id, document_id=document.id,
                          fields={"secret": {"value": "Bob secret", "review_state": "unreviewed"}})
    session.add_all([message, item])
    await session.flush()
    assertion = Assertion(message_id=message.id, position=0, text="Bob private conclusion")
    session.add(assertion)
    await session.flush()
    session.add_all([Citation(evidence_id=evidence.id, source_kind="assertion", source_id=assertion.id,
                             content_digest=evidence.content_digest),
                     Citation(evidence_id=evidence.id, source_kind="extract_field", source_id=f"{item.id}:secret",
                              content_digest=evidence.content_digest)])
    await session.commit()
    backlinks = await actor_client.get(f"/api/evidence/{evidence.id}/backlinks")
    assert backlinks.status_code == 200 and backlinks.json()["backlinks"] == []
    assert (await actor_client.get("/api/reviews")).json()["items"] == []
    denied = await actor_client.post(f"/api/reviews/extract_field/{item.id}:secret", json={"action": "pass"})
    assert denied.status_code == 404
    own = await actor_client.get(f"/api/evidence/{evidence.id}/backlinks", headers=actor_headers("bob"))
    assert len(own.json()["backlinks"]) == 2
    resource.publication = "private"
    await session.commit()
    assert (await actor_client.get("/api/reviews", headers=actor_headers("bob"))).json()["items"] == []


@respx.mock
async def test_unattributed_legacy_knowledge_quarantined(actor_client, session):
    from ddp_corpus.models import KnowledgeEntity
    await source(session, publication="published")
    session.add(KnowledgeEntity(canonical_name="Private generated concept", normalized_name="private"))
    await session.commit()
    assert (await actor_client.get("/api/knowledge/entities")).json()["entities"] == []


@respx.mock
async def test_published_wiki_listing_has_an_organization_boundary(actor_client, session):
    """已发布 Wiki 的列表查询必须带组织谓词（不变式 8）。

    旧行为：已发布分支不带组织条件，靠后面逐条 404 兜底 —— 别的组织的行会占掉这一页
    的名额，本组织的 Wiki 因此可能根本不出现在列表里（静默少给，不是报错）。
    """
    resource, version, evidence, _ = await source(session, publication="published")
    model(evidence)
    response = await actor_client.post("/api/wikis", json=body(resource, version),
                                       headers={"Idempotency-Key": "org-boundary"})
    assert response.status_code == 201, response.text
    created = response.json()
    wiki_id, revision_id = created["wiki"]["id"], created["revision"]["id"]
    published = await actor_client.post(f"/api/wikis/{wiki_id}/publish",
                                        json={"base_revision_id": revision_id})
    assert published.status_code == 200, published.text

    # 别的组织有一大批更新的已发布 Wiki：查询不带组织谓词时，它们会把这一页
    # （limit 200）占满，本组织的 Wiki 直接从列表里消失 —— 静默少给，不是报错。
    later = utcnow() + timedelta(hours=1)
    session.add_all([Wiki(owner_id="actor-filler", organization_id="org-filler",
                          title="别处的", current_revision_id=revision_id,
                          published_revision_id=revision_id, created_at=later)
                     for _ in range(200)])
    await session.commit()

    # 别的组织里有一份**自己的**已发布 Wiki（独立资源与依赖）：加了组织谓词之后，
    # 他们该看见自己的那份，看不见我们的。
    their_resource, their_version, their_evidence, _ = await source(
        session, owner="actor-elsewhere", organization="org-elsewhere",
        publication="published", text="Their own fact.")
    model(their_evidence)
    outsider_headers = actor_headers("actor-elsewhere", organization_id="org-elsewhere")
    theirs = await actor_client.post("/api/wikis", json=body(their_resource, their_version),
                                     headers={**outsider_headers, "Idempotency-Key": "theirs"})
    assert theirs.status_code == 201, theirs.text
    their_wiki = theirs.json()["wiki"]["id"]
    await actor_client.post(f"/api/wikis/{their_wiki}/publish",
                            json={"base_revision_id": theirs.json()["revision"]["id"]},
                            headers=outsider_headers)

    mine = (await actor_client.get("/api/wikis")).json()
    assert [item["wiki"]["id"] for item in mine] == [wiki_id], "本组织的已发布 Wiki 不许被挤掉"
    outsider = await actor_client.get("/api/wikis", headers=outsider_headers)
    assert outsider.status_code == 200
    listed = [item["wiki"]["id"] for item in outsider.json()]
    assert their_wiki in listed, "他们还得看得见自己的那份"
    assert wiki_id not in listed, "别的组织的已发布 Wiki 不该进这张列表"
    # 谓词不能收得过头：**同组织的非所有者**要能看到已发布的那份
    # （上面那条查询者恰好是所有者本人，测不到这件事）。
    colleague = (await actor_client.get("/api/wikis", headers=actor_headers("bob"))).json()
    assert [item["wiki"]["id"] for item in colleague] == [wiki_id]
    # 直读同样有组织边界，不只靠依赖资源那一层兜底。上面那份 Wiki 的 404 其实来自
    # 依赖资源的组织校验，钉不住 `get_wiki` 自己的谓词 —— 所以再造一份**没有依赖行**的
    # 已发布 Wiki（将来真允许无依赖的 Wiki 时就是这条路径漏出去）。
    bare = Wiki(owner_id=ACTOR, organization_id=ORG, title="无依赖")
    session.add(bare)
    await session.flush()
    bare_revision = WikiRevision(wiki_id=bare.id, title="无依赖", created_by=ACTOR, kind="generated")
    session.add(bare_revision)
    await session.flush()
    bare.current_revision_id = bare.published_revision_id = bare_revision.id
    await session.commit()
    assert (await actor_client.get(f"/api/wikis/{bare.id}")).status_code == 200, "所有者自己读得到"
    leaked = await actor_client.get(f"/api/wikis/{bare.id}", headers=outsider_headers)
    assert leaked.status_code == 404, "别的组织不该读到，哪怕这份 Wiki 没有依赖可判权"
    assert (await actor_client.get(f"/api/wikis/{wiki_id}", headers=outsider_headers)).status_code == 404


@respx.mock
async def test_same_title_plans_merge_without_losing_sources_or_human_edits(actor_client, session):
    first, first_version, first_evidence, _ = await source(session, text="RP2040 has 264 kB SRAM.")
    second, second_version, second_evidence, _ = await source(session, text="ESP32 has 520 KB SRAM.")
    pages = [{"title": "SRAM", "sections": ["RP2040"], "references": [1], "source_term": None},
             {"title": " sram ", "sections": ["ESP32"], "references": [2], "source_term": None}]
    claims = [{"text": evidence.content, "evidence_ids": [evidence.id], "conflict_group": None}
              for evidence in (first_evidence, second_evidence)]
    scripted(pages, claims)
    request = body(first, first_version, title="SRAM comparison")
    request["sources"].append({"resource_id": second.id, "source_version_id": second_version.id})
    created = await actor_client.post("/api/wikis", json=request,
                                      headers={"Idempotency-Key": "merged"})
    assert created.status_code == 201, created.text
    wiki_id, revision = created.json()["wiki"]["id"], created.json()["revision"]
    assert [page["title"] for page in revision["pages"]] == ["SRAM"]
    page = revision["pages"][0]
    assert {eid for section in page["generated_sections"] for claim in section["sentences"]
            for eid in claim["evidence_ids"]} == {first_evidence.id, second_evidence.id}
    edited = await actor_client.patch(f"/api/wikis/{wiki_id}/pages/{page['page_key']}", json={
        "base_revision_id": revision["id"], "paragraphs": [{"id": "note", "text": "Check units."}]},
        headers={"Idempotency-Key": "merged-note"})
    assert edited.status_code == 201, edited.text
    rebuilt = await actor_client.post(f"/api/wikis/{wiki_id}/revisions", json={
        **request, "base_revision_id": edited.json()["revision"]["id"]},
        headers={"Idempotency-Key": "merged-rebuild"})
    assert rebuilt.status_code == 201, rebuilt.text
    assert [(p["page_key"], p["human_paragraphs"]) for p in rebuilt.json()["revision"]["pages"]] == [
        (page["page_key"], edited.json()["revision"]["pages"][0]["human_paragraphs"])]


@pytest.mark.parametrize("omission_stage", ["plan", "write"])
@respx.mock
async def test_cross_source_omission_rejects_rebuild_without_consuming_key(
        actor_client, session, omission_stage):
    first, first_version, first_evidence, _ = await source(session, text="RP2040 has 264 kB SRAM.")
    second, second_version, second_evidence, _ = await source(session, text="ESP32 has 520 KB SRAM.")
    page = {"title": "SRAM", "sections": ["Comparison"], "references": [1, 2], "source_term": None}
    claims = [{"text": evidence.content, "evidence_ids": [evidence.id], "conflict_group": None}
              for evidence in (first_evidence, second_evidence)]
    scripted([page], claims)
    request = body(first, first_version, title="SRAM comparison")
    request["sources"].append({"resource_id": second.id, "source_version_id": second_version.id})
    created = await actor_client.post("/api/wikis", json=request, headers={"Idempotency-Key": "full"})
    assert created.status_code == 201, created.text
    wiki_id, revision_id = created.json()["wiki"]["id"], created.json()["revision"]["id"]
    request["base_revision_id"] = revision_id
    scripted([{**page, "references": [1]}] if omission_stage == "plan" else [page],
             claims[:1] if omission_stage == "write" else claims)
    refused = await actor_client.post(f"/api/wikis/{wiki_id}/revisions", json=request,
                                      headers={"Idempotency-Key": "retry-after-omission"})
    assert refused.status_code == 502, refused.text
    assert refused.json()["error"]["code"] == "wiki_generation_failed"
    assert (await actor_client.get(f"/api/wikis/{wiki_id}")).json()["revision"]["id"] == revision_id
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 1
    assert await session.scalar(select(func.count()).select_from(WikiWriteKey)) == 1
    scripted([page], claims)
    retried = await actor_client.post(f"/api/wikis/{wiki_id}/revisions", json=request,
                                      headers={"Idempotency-Key": "retry-after-omission"})
    assert retried.status_code == 201, retried.text
    assert retried.json()["revision"]["id"] != revision_id
