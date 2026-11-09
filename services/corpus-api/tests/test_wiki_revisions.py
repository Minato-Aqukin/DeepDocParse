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


def model(evidence, *, title="Overview", retrieval_queries=None):
    def respond(request):
        prompt = json.loads(request.content)
        assert prompt["max_tokens"] > 0
        assert prompt["response_format"]["type"] == "json_schema"
        assert prompt["response_format"]["json_schema"]["strict"] is True
        schema = prompt["response_format"]["json_schema"]["schema"]
        if "retrieval_queries" in schema["properties"]:
            requested = json.loads(prompt["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": list(retrieval_queries) if retrieval_queries else [requested]}
        elif "pages" in schema["properties"]:
            payload = {"pages": [{"title": title, "sections": ["Facts"], "references": [1],
                                  "source_term": None}]}
        else:
            supplied = json.loads(prompt["messages"][-1]["content"])["evidence"]
            span_id = sorted(supplied[0]["spans"],
                             key=lambda identifier: int(identifier.split(".")[1]))[0]
            for entry in supplied:
                if entry["evidence_id"] == evidence.id:
                    span_id = sorted(entry["spans"],
                                     key=lambda identifier: int(identifier.split(".")[1]))[0]
                    break
            payload = {"sections": [{"heading": "Facts", "sentences": [{
                "text": evidence.content,
                "support": [span_id], "conflict_group": None}]}]}
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
    selection = first["revision"]["limits"]["evidence_selection"]
    assert selection["total_original_evidence"] == 1
    assert selection["selected_evidence"] == 1
    assert selection["omitted_evidence"] == 0
    assert selection["complete"] is True
    assert selection["ranking_degraded"] == "embedding_unavailable"
    assert selection["sources"] == [{"resource_id": resource.id,
                                     "source_version_id": version.id,
                                     "total_original_evidence": 1, "selected_evidence": 1}]
    assert selection["query_planning"]["status"] == "unavailable"
    assert selection["query_planning"]["reason"] == "planner_returned_title_only"
    assert selection["query_planning"]["reserved_output_tokens"] > 0
    assert "retrieval_queries" not in selection
    retried = await actor_client.post("/api/wikis", json=create_body, headers={"Idempotency-Key": "create"})
    assert retried.json()["revision"]["id"] == revision_id and calls.call_count == 3
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
async def test_table_structure_cannot_bypass_wiki_input_budget(actor_client, session):
    resource, version, evidence, _ = await source(session)
    chunk = await session.scalar(select(Chunk).where(Chunk.evidence_id == evidence.id))
    chunk.block_type = "table"
    chunk.table_html = "<table><tr>" + "<td></td>" * 150 + \
        "<td>Original fact.</td></tr></table>"
    await session.commit()
    model(evidence)
    denied = await actor_client.post("/api/wikis", json=body(
        resource, version, max_pages=1, max_input_chars=1000),
        headers={"Idempotency-Key": "bounded-table-structure"})
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "wiki_budget_exceeded"
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


@respx.mock
async def test_old_wiki_keeps_table_structure_after_index_changes(actor_client, session):
    resource, version, evidence, _ = await source(session)
    chunk = await session.scalar(select(Chunk).where(Chunk.evidence_id == evidence.id))
    original_html = "<table><tr><td colspan=\"2\">Original fact.</td></tr></table>"
    chunk.block_type, chunk.table_html = "table", original_html
    await session.commit()
    model(evidence)
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "fixed-table-structure"})
    assert created.status_code == 201, created.text
    fixed = created.json()
    chunk.table_html = "<table><tr><td>Different later structure.</td></tr></table>"
    await session.commit()
    history = await actor_client.get(
        f"/api/wikis/{fixed['wiki']['id']}/revisions/{fixed['revision']['id']}")
    assert history.status_code == 200
    revision = history.json()["revision"]
    assert revision["limits"]["table_contexts"] == [{
        "evidence_id": evidence.id, "table_html": original_html,
        "table_html_digest": "sha256:" + hashlib.sha256(original_html.encode()).hexdigest(),
    }]
    assert revision["dependency_manifest"][0]["excerpt_digest"] == evidence.content_digest


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


def scripted(plan_pages, sentences, *, retrieval_queries=()):
    def respond(request):
        request_body = json.loads(request.content)
        schema = request_body["response_format"]["json_schema"]["schema"]
        if "retrieval_queries" in schema["properties"]:
            title = json.loads(request_body["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": list(retrieval_queries) or [title]}
        elif "pages" in schema["properties"]:
            payload = {"pages": plan_pages}
        else:
            supplied = json.loads(request_body["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: sorted(
                entry["spans"], key=lambda identifier: int(identifier.split(".")[1]))
                for entry in supplied}
            # Select the intended fixture original without depending on retrieval
            # order. Keep all other fields verbatim, including malformed responses.
            rendered = []
            for sentence in sentences:
                item = dict(sentence)
                if isinstance(item.get("support"), list):
                    item["support"] = [
                        spans[choice["_evidence_id"]][0]
                        if isinstance(choice, dict) and "_evidence_id" in choice else choice
                        for choice in item["support"]]
                rendered.append(item)
            payload = {"sections": [{"heading": "Facts", "sentences": rendered}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})
    return respx.post(CHAT).mock(side_effect=respond)


@respx.mock
async def test_span_selection_binds_original_and_empty_selection_stays_unsupported(
        actor_client, session):
    """One explicit span pointer determines the original; no duplicate ID is needed."""
    resource, version, evidence, _ = await source(session)
    page = {"title": "Overview", "sections": ["Facts"], "references": [1], "source_term": None}
    scripted([page], [{"text": evidence.content, "conflict_group": None,
                       "support": [{"_evidence_id": evidence.id}]},
                      {"text": "The supplied evidence does not state the requested additional fact.",
                       "conflict_group": None, "support": []}])
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "by-selected-span"})
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
             [{"text": e.content, "conflict_group": None,
               "support": [{"_evidence_id": e.id}]} for e in (pe, ue)])
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
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        if "retrieval_queries" in schema["properties"]:
            # Query planning runs first and sees the title alone; revocation lands
            # here so the outline call afterwards is the one that must stop.
            resource.publication = "private"
            await session.commit()
            title = json.loads(json.loads(request.content)["messages"][-1]["content"])["title"]
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "retrieval_queries": [title]})}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "pages": [{"title": "Overview", "sections": ["Facts"], "references": [1],
                       "source_term": None}]})}, "finish_reason": "stop"}]})
    calls = respx.post(CHAT).mock(side_effect=revoke)
    result = await actor_client.post("/api/wikis", json=body(resource, version),
                                     headers={"Idempotency-Key": "revocation"})
    assert result.status_code == 404
    assert calls.call_count == 1  # no request after revocation during the title-only call
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


async def source_with(session, texts: list[str], *, kinds: list[str] | None = None,
                       pages: list[int] | None = None):
    kinds = list(kinds) if kinds is not None else ["text"] * len(texts)
    pages = list(pages) if pages is not None else list(range(len(texts)))
    assert len(kinds) == len(texts), "kinds must align with texts"
    assert len(pages) == len(texts), "pages must align with texts"
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
    for seq, (text, kind, page) in enumerate(zip(texts, kinds, pages)):
        row = Evidence(document_id=document.id, parse_job_id=job.id, seq=seq, atom_key=f"text-{seq}",
                       content=text, content_digest=hashlib.sha256(text.encode()).hexdigest(),
                       kind=kind, page_idx=page, bbox=[0, 0, 50, 50], page_size=[100, 100])
        session.add(row)
        rows.append(row)
    await session.flush()
    session.add_all([index_chunk(row) for row in rows])
    await session.commit()
    return resource, version, rows


def topic_model(rows, *, relations=(), retrieval_queries=()):
    def respond(request):
        prompt = json.loads(request.content)
        assert prompt["response_format"]["json_schema"]["strict"] is True
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            title = json.loads(prompt["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": list(retrieval_queries) or [title]}
        elif "pages" in properties:
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
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: sorted(
                entry["spans"], key=lambda identifier: int(identifier.split(".")[1]))
                for entry in context}
            payload = {"sections": [{"heading": "Facts", "sentences": [
                {"text": row.content, "conflict_group": None,
                 "support": [spans[row.id][0]]}
                for row in rows if row.id in spans][:2]}]}
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
        if "retrieval_queries" in properties:
            title = json.loads(prompt["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": [title]}
        elif "pages" in properties:
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
            supplied = json.loads(prompt["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: sorted(
                entry["spans"], key=lambda identifier: int(identifier.split(".")[1]))
                for entry in supplied}
            payload = {"sections": [{"heading": "Facts", "sentences": [{
                "text": evidence.content, "conflict_group": None,
                "support": [spans[evidence.id][0]],
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
    selection = revision["limits"]["evidence_selection"]
    assert selection["total_original_evidence"] == 4
    assert selection["selected_evidence"] == 2
    assert selection["omitted_evidence"] == 2
    assert selection["complete"] is False
    assert selection["ranking_degraded"] == "embedding_unavailable"
    assert selection["sources"] == [{"resource_id": resource.id,
                                     "source_version_id": version.id,
                                     "total_original_evidence": 4, "selected_evidence": 2}]
    assert selection["query_planning"]["status"] == "unavailable"
    assert selection["query_planning"]["reason"] == "planner_returned_title_only"
    assert "retrieval_queries" not in selection
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
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            title = json.loads(prompt["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": [title]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                         "finish_reason": "stop"}]})
        context = json.loads(prompt["messages"][-1]["content"])["evidence"]
        planning = "pages" in properties
        if planning:
            payload = {"pages": [{"title": "Overview", "sections": ["Facts"], "references": [1, 2],
                                 "source_term": None}]}
        else:
            payload = {"sections": [{"heading": "Facts", "sentences": [
                {"text": entry["spans"][sorted(
                    entry["spans"], key=lambda identifier: int(identifier.split(".")[1]))[0]],
                 "conflict_group": None,
                 "support": [sorted(entry["spans"],
                                    key=lambda identifier: int(identifier.split(".")[1]))[0]]}
                for entry in context]}]}
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
    claims = [{"text": evidence.content, "conflict_group": None,
               "support": [{"_evidence_id": evidence.id}]}
              for evidence in (first_evidence, second_evidence)]
    scripted(pages, claims)
    request = body(first, first_version, title="SRAM comparison", max_pages=2)
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
    claims = [{"text": evidence.content, "conflict_group": None,
               "support": [{"_evidence_id": evidence.id}]}
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


@respx.mock
async def test_extra_citation_channel_is_rejected_without_rebinding(actor_client, session):
    """A producer cannot add a conflicting legacy ID beside its span selection.

    Reject the whole draft without consuming the key; the corrected single-pointer
    response must then persist each span's own immutable original identity."""
    resource, version, rows = await source_with(session, [
        "We use the normalized hidden state of [CLS]: eq = norm(Hq[0]). "
        "The relevance score is sdense = <ep, eq>.",
        "Based on ensemble learning, predictions from different retrieval methods "
        "are integrated as a teacher signal via self-knowledge distillation."])
    formula, distillation = rows
    page = {"title": "Retrieval", "sections": ["Formulas"], "references": [1, 2], "source_term": None}
    scripted([page], [
        {"text": "Dense retrieval scores with sdense = <ep, eq>.",
         "evidence_ids": [distillation.id], "conflict_group": None,
         "support": [{"_evidence_id": formula.id}]},
        {"text": "Distillation integrates a teacher signal.",
         "conflict_group": None,
         "support": [{"_evidence_id": distillation.id}]}])
    refused = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "mismatched-quote"})
    assert refused.status_code == 502, refused.text
    assert refused.json()["error"]["code"] == "wiki_generation_failed"
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0
    assert await session.scalar(select(func.count()).select_from(WikiWriteKey)) == 0
    scripted([page], [
        {"text": "Dense retrieval scores with sdense = <ep, eq>.",
         "conflict_group": None,
         "support": [{"_evidence_id": formula.id}]},
        {"text": "Distillation integrates a teacher signal.",
         "conflict_group": None,
         "support": [{"_evidence_id": distillation.id}]}])
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "mismatched-quote"})
    assert created.status_code == 201, created.text
    claims = [claim for page in created.json()["revision"]["pages"]
              for section in page["generated_sections"] for claim in section["sentences"]]
    assert [(claim["evidence_ids"], claim["support_check"]) for claim in claims] == [
        ([formula.id], {"mechanical": "verbatim_present", "quotes": 1}),
        ([distillation.id], {"mechanical": "verbatim_present", "quotes": 1})]
    observations = created.json()["revision"]["limits"]["generation_observations"]
    assert [entry["stage"] for entry in observations] == ["plan", "page"]
    assert observations[1]["claim_checks"][0]["verdict"] == "verbatim_present"


@pytest.mark.parametrize("selection", [None, ["q1.99"], ["q1.1", "q1.1"]])
@respx.mock
async def test_invalid_support_selection_is_rejected_without_partial_revision(
        actor_client, session, selection):
    """Missing, unknown and duplicate selections must fail, not become an
    apparently supported claim or an implicit insufficient-evidence verdict."""
    resource, version, evidence, _ = await source(
        session, text="The encoder contains self-attention layers where keys, values and queries "
                      "come from the previous layer.")
    page = {"title": "Encoder", "sections": ["Attention"], "references": [1], "source_term": None}
    scripted([page], [{"text": "Self-attention captures long-range dependencies.",
                       "conflict_group": None, "support": selection}])
    refused = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "invented-quote"})
    assert refused.status_code == 502, refused.text
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


@respx.mock
async def test_selected_support_keeps_case_and_short_atoms(actor_client, session):
    """Selected spans preserve the original bytes: math symbols keep their case and a
    short atom is offered as its whole non-empty span."""
    resource, version, rows = await source_with(session, ["Routing key K selects the shard.", "E=mc2"])
    page = {"title": "Keys", "sections": ["Facts"], "references": [1, 2], "source_term": None}
    scripted([page], [{"text": "Shard key is K.", "conflict_group": None,
                       "support": [{"_evidence_id": rows[0].id}]},
                      {"text": "Energy follows E=mc2.", "conflict_group": None,
                       "support": [{"_evidence_id": rows[1].id}]}])
    created = await actor_client.post("/api/wikis", json=body(resource, version),
                                      headers={"Idempotency-Key": "case-quote"})
    assert created.status_code == 201, created.text
    claims = [claim for page in created.json()["revision"]["pages"]
              for section in page["generated_sections"] for claim in section["sentences"]]
    assert [claim["support"][0]["quote"] for claim in claims] == [
        "Routing key K selects the shard.", "E=mc2"]


@respx.mock
async def test_planned_queries_cover_distinct_title_subtopics_within_budget(actor_client, session):
    """A topical title must not collapse onto entity-name blocks: planned per-topic
    queries retrieve through the shared facet seam, each requested subtopic keeps
    its slot, and the planning reservation is charged against max_output_tokens."""
    resource, version, rows = await source_with(session, [
        "Manual supply voltage strapping boot overview manual manual.",
        "Manual supply voltage strapping boot appendix manual manual.",
        "Supply voltage regulation regulation regulation.",
        "Strapping boot pin pin pin.",
    ])
    name_block, _, voltage, boot = rows
    calls = topic_model([voltage, boot],
                        retrieval_queries=["supply voltage regulation", "strapping boot pin"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "subtopic-coverage"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selection = revision["limits"]["evidence_selection"]
    assert selection["query_planning"]["status"] == "applied"
    assert selection["query_planning"]["reserved_output_tokens"] == 256
    assert selection["retrieval_queries"] == ["supply voltage regulation",
                                              "strapping boot pin"]
    # Without the planned queries the title's repeated entity word ranks the two
    # name blocks first and the requested subtopics never reach the model.
    assert {d["evidence_id"] for d in revision["dependency_manifest"]} == {
        voltage.id, boot.id}
    assert name_block.id not in {d["evidence_id"] for d in revision["dependency_manifest"]}
    spent = [json.loads(call.request.content)["max_tokens"] for call in calls.calls]
    assert spent[0] == 256
    assert sum(spent) <= 2048
    assert revision["limits"]["max_output_tokens"] == 2048


@respx.mock
async def test_broken_query_planner_falls_back_to_title_only_and_keeps_reservation(
        actor_client, session):
    """A planner fault never invents queries or retries: retrieval falls back to the
    title, the failure stays visible, and the reservation is still charged."""
    resource, version, rows = await long_source(session)
    calls = topic_model(rows)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Controller reset delay and watchdog settings",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "planner-fallback"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selection = revision["limits"]["evidence_selection"]
    assert selection["query_planning"]["status"] == "unavailable"
    assert selection["query_planning"]["reserved_output_tokens"] == 256
    assert "retrieval_queries" not in selection
    spent = [json.loads(call.request.content)["max_tokens"] for call in calls.calls]
    assert spent[0] == 256
    assert sum(spent) <= 2048


@respx.mock
async def test_each_planned_query_keeps_its_own_ranked_slots(actor_client, session):
    """Per-query ranking, not one recycled round-robin prefix: a subtopic whose
    topical block ranks first under its own planned query keeps its slot even
    when the title query ranks generic entity-name blocks ahead of it."""
    resource, version, rows = await source_with(session, [
        "Manual overview manual manual manual manual.",
        "Manual appendix manual manual manual manual.",
        "Manual index manual manual manual manual.",
        "Supply voltage regulation regulation regulation.",
        "Strapping boot pin pin pin.",
    ])
    voltage, boot = rows[3], rows[4]
    calls = topic_model([voltage, boot],
                        retrieval_queries=["supply voltage regulation", "strapping boot pin"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=3, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "per-query-slots"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selected = {d["evidence_id"] for d in revision["dependency_manifest"]}
    # The title query ranks the three name blocks first (every title term hits
    # them); without per-query slots the single shared prefix would spend all
    # 3 positions on them and neither topical block would reach the model.
    assert voltage.id in selected
    assert boot.id in selected
    assert len(selected) == 3
    assert revision["limits"]["evidence_selection"]["retrieval_queries"] == [
        "supply voltage regulation", "strapping boot pin"]
    spent = [json.loads(call.request.content)["max_tokens"] for call in calls.calls]
    assert spent[0] == 256
    assert sum(spent) <= 2048


@respx.mock
async def test_focused_facets_fill_budget_before_generic_title(actor_client, session):
    resource, version, rows = await source_with(session, [
        "Manual overview interface voltage start.",
        "Output voltage regulation supplies 4 V.",
        "Output voltage safety cutoff is 5 V.",
        "Startup strap mode uses pin X.",
    ])
    overview, *facts = rows
    topic_model(facts, retrieval_queries=[
        "output voltage regulation safety", "startup strap mode"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title=overview.content,
        max_pages=2, max_evidence=3, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "focused-before-title"})

    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert {entry["evidence_id"] for entry in revision["dependency_manifest"]} == {
        row.id for row in facts}
    assert revision["limits"]["evidence_selection"]["selected_evidence"] == 3


@respx.mock
async def test_per_query_selection_respects_evidence_limit(actor_client, session):
    """Three planned queries share two evidence slots, not two slots per query."""
    resource, version, rows = await source_with(session, [
        "Manual overview manual manual manual.",
        "Manual appendix manual manual manual.",
        "Supply voltage regulation regulation regulation.",
        "Strapping boot pin pin pin.",
        "Oscillator trim calibration calibration calibration.",
    ])
    topic_model(rows, retrieval_queries=["supply voltage regulation", "strapping boot pin",
                                        "oscillator trim calibration"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "per-query-bounded"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert len({entry["evidence_id"] for entry in revision["dependency_manifest"]}) == 2
    assert revision["limits"]["evidence_selection"]["selected_evidence"] == 2


@respx.mock
async def test_rerank_outage_keeps_hybrid_order_and_reports_degraded(actor_client, session,
                                                                    monkeypatch):
    """Rerank outage is visible, not silent: the hybrid pool order stands and
    `ranking_degraded` reports `rerank_unavailable` (QA/extraction parity)."""
    from ddp_corpus.config import settings
    from tests.conftest import EMBEDDINGS
    from tests.test_documents import _embed_response

    respx.post(EMBEDDINGS).mock(side_effect=_embed_response)

    resource, version, rows = await source_with(session, [
        "Manual supply voltage strapping boot overview manual manual.",
        "Manual supply voltage strapping boot appendix manual manual.",
        "Supply voltage regulation regulation regulation.",
        "Strapping boot pin pin pin.",
    ])
    _, _, voltage, boot = rows
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_candidates", 4)
    respx.post(settings.rerank_endpoint).mock(
        return_value=httpx.Response(404, json={"error": "no model"}))
    topic_model([voltage, boot],
                retrieval_queries=["supply voltage regulation", "strapping boot pin"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "rerank-outage"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selection = revision["limits"]["evidence_selection"]
    assert selection["ranking_degraded"] == "rerank_unavailable"
    assert {d["evidence_id"] for d in revision["dependency_manifest"]} == {
        voltage.id, boot.id}


@respx.mock
async def test_rerank_pool_excludes_atoms_from_other_source_versions(actor_client, session,
                                                                    monkeypatch):
    """An out-of-scope index result must never reach the reranker or frozen plan."""
    from ddp_corpus import wiki as wiki_service
    from ddp_corpus.config import settings
    from tests.conftest import EMBEDDINGS
    from tests.test_documents import _embed_response

    respx.post(EMBEDDINGS).mock(side_effect=_embed_response)

    resource, version, rows = await source_with(session, [
        "Manual supply voltage strapping boot overview manual manual.",
        "Manual supply voltage strapping boot appendix manual manual.",
        "Supply voltage regulation regulation regulation.",
        "Strapping boot pin pin pin.",
    ])
    _, _, outside = await source_with(session, ["Other source version: confidential control value."])
    search = wiki_service.search_query

    async def search_with_foreign_result(*args, **kwargs):
        hits, degraded = await search(*args, **kwargs)
        return [*hits, {**hits[0], "evidence_id": outside[0].id,
                       "text": outside[0].content, "score": 1000.0}], degraded

    monkeypatch.setattr(wiki_service, "search_query", search_with_foreign_result)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_candidates", 4)

    def reranked(request):
        texts = json.loads(request.content)["texts"]
        assert outside[0].content not in texts, "Out-of-scope text must not leave for reranking."
        # Reverse the hybrid pool order: fusion must still only reorder pool
        # members, never invent or import atoms from outside the pool.
        return httpx.Response(200, json=[
            {"index": index, "score": float(len(texts) - rank)}
            for rank, index in enumerate(reversed(range(len(texts))))])

    respx.post(settings.rerank_endpoint).mock(side_effect=reranked)
    topic_model(rows, retrieval_queries=["supply voltage regulation", "strapping boot pin"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "rerank-bounded"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selected = {d["evidence_id"] for d in revision["dependency_manifest"]}
    assert selected <= {row.id for row in rows}
    assert len(selected) == 2
    assert revision["limits"]["evidence_selection"]["ranking_degraded"] is None


@respx.mock
async def test_withdrawal_during_retrieval_blocks_reranker_egress(
        actor_client, session, monkeypatch):
    from sqlalchemy import update
    from ddp_corpus import wiki as wiki_service
    from ddp_corpus.config import settings

    resource, version, rows = await source_with(
        session, ["manual voltage specification.", "manual current specification."])
    model(rows[0], retrieval_queries=["manual"])
    monkeypatch.setattr(settings, "rerank_enabled", True)
    reranker = respx.post(settings.rerank_endpoint).mock(return_value=httpx.Response(
        200, json=[{"index": 0, "score": 1.0}, {"index": 1, "score": 0.5}]))
    search = wiki_service.search_query

    async def withdraw_after_search(*args, **kwargs):
        hits, degraded = await search(*args, **kwargs)
        await session.execute(update(Resource).where(Resource.id == resource.id)
                              .values(publication="withdrawn"))
        await session.commit()
        return hits, degraded

    monkeypatch.setattr(wiki_service, "search_query", withdraw_after_search)
    response = await actor_client.post("/api/wikis", json=body(
        resource, version, title="manual"), headers={"Idempotency-Key": "withdraw-before-rerank"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wiki_source_unavailable"
    assert not reranker.called, "Withdrawal must stop source text before the reranker request."
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


@respx.mock
async def test_generation_context_restores_source_reading_order(actor_client, session):
    """Ranked budget selection is preserved, but the planner/writer see the
    frozen evidence in source reading order (QA `build_messages` parity): a
    later-ranked continuation keeps its own reference next to its subject."""
    resource, version, rows = await source_with(session, [
        "Zebra manual manual manual manual.",
        "Manual supply voltage regulation regulation.",
        "Manual strapping boot pin pin.",
    ])
    subject, voltage, boot = rows
    seen: dict = {}

    def respond(request):
        prompt = json.loads(request.content)
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            title = json.loads(prompt["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": [title]}
        elif "pages" in properties:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            seen["order"] = [item["evidence_id"] for item in context]
            references = [item["reference"] for item in context]
            payload = {"pages": [{"title": "Manual", "sections": ["Facts"],
                                  "references": references, "source_term": None}]}
        else:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: sorted(
                entry["spans"], key=lambda identifier: int(identifier.split(".")[1]))
                for entry in context}
            payload = {"sections": [{"heading": "Facts", "sentences": [
                {"text": row.content, "conflict_group": None,
                 "support": [spans[row.id][0]]} for row in rows]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=1, max_evidence=3, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "reading-order"})
    assert created.status_code == 201, created.text
    # source_with assigns page_idx == seq, so reading order is creation order
    # regardless of which block the ranker preferred.
    assert seen["order"] == [subject.id, voltage.id, boot.id]
    assert {d["evidence_id"] for d in
            created.json()["revision"]["dependency_manifest"]} == {row.id for row in rows}


@respx.mock
async def test_unauthorized_source_fails_before_any_paid_model_work(actor_client, session):
    """Auth stays before all paid model work: an unknown source fails before
    the planner, the ranker, and the writer — no chat, embedding, or rerank
    call is issued."""
    from ddp_corpus.config import settings
    from tests.conftest import EMBEDDINGS

    resource, version, _ = await source_with(session, ["Manual fact."])
    chat = respx.post(CHAT).mock(side_effect=AssertionError("chat must not be called"))
    embeds = respx.post(EMBEDDINGS).mock(side_effect=AssertionError("embed must not be called"))
    rerank = respx.post(settings.rerank_endpoint).mock(
        side_effect=AssertionError("rerank must not be called"))
    refused = await actor_client.post("/api/wikis", json={
        "title": "Manual", "sources": [{"resource_id": "missing-resource",
                                        "source_version_id": version.id}],
        "max_pages": 1, "max_evidence": 2, "max_input_chars": 12000, "max_output_tokens": 2048},
        headers={"Idempotency-Key": "auth-before-models"})
    assert refused.status_code == 404, refused.text
    assert chat.call_count == 0
    assert embeds.call_count == 0
    assert rerank.call_count == 0


@respx.mock
async def test_ranked_seed_pulls_page_local_continuation_within_limits(actor_client, session):
    """A ranked heading seed pulls its ordered +1 continuation ahead of a
    higher-scoring filler: without region closure the writer would state the
    seed fragment as a complete fact. Context reserves slots under the same
    max_evidence/max_input_chars instead of appending beyond them."""
    resource, version, rows = await source_with(session, [
        "Manual supply voltage strapping boot overview.",
        "Recommended operating conditions supply rail tolerance.",
        "Manual supply boot appendix filler.",
        "Manual supply boot index filler.",
    ], pages=[0, 0, 1, 2])
    seed, continuation, filler_a, filler_b = rows
    calls = topic_model([seed, continuation],
                        retrieval_queries=["Manual supply voltage and strapping boot"])
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Manual supply voltage and strapping boot",
        max_pages=2, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "seed-continuation"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    selected = {d["evidence_id"] for d in revision["dependency_manifest"]}
    # Keyword rank alone orders seed, filler_a, filler_b, continuation: the
    # continuation shares only the crowded term "supply" while each filler
    # also matches "manual" and "boot". The +1 page-local continuation still
    # takes the second slot; the fillers stay out.
    assert selected == {seed.id, continuation.id}
    assert revision["limits"]["evidence_selection"]["selected_evidence"] == 2
    # No new model work: planning, outline and writing calls only.
    assert calls.call_count == 4


@respx.mock
async def test_each_requested_facet_keeps_its_seed_before_neighbor_context(actor_client, session):
    resource, version, rows = await source_with(session, [
        "Supply voltage voltage voltage: minimum 3 V.",
        "The adjacent paragraph discusses optional packaging.",
        "Boot pins boot pins boot pins: sample them when reset ends.",
        "The adjacent paragraph discusses optional connectors.",
        "Sleep current sleep current sleep current: typical 5 microamperes.",
        "The adjacent paragraph discusses optional storage.",
    ], pages=[0, 0, 1, 1, 2, 2])
    facts = {
        rows[0].id: "最低供电电压为 3 V。",
        rows[2].id: "启动引脚在复位结束时采样。",
        rows[4].id: "典型休眠电流为 5 微安。",
    }

    def respond(request):
        prompt = json.loads(request.content)
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        supplied = json.loads(prompt["messages"][-1]["content"])
        if "retrieval_queries" in properties:
            payload = {"retrieval_queries": [
                "minimum supply voltage", "boot pin sampling", "typical sleep current"]}
        elif "pages" in properties:
            payload = {"pages": [{"title": "Electrical settings", "sections": ["Specifications"],
                                  "references": [item["reference"] for item in supplied["evidence"]],
                                  "source_term": None}]}
        else:
            spans = {item["evidence_id"]: list(item["spans"]) for item in supplied["evidence"]}
            payload = {"sections": [{"heading": "Specifications", "sentences": [
                {"text": text if identifier in spans else "本次证据不足以回答这一参数。",
                 "support": [spans[identifier][0]] if identifier in spans else [],
                 "conflict_group": None}
                for identifier, text in facts.items()]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Supply voltage, boot pin sampling and sleep current",
        max_pages=1, max_evidence=3, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "facet-seeds-before-neighbors"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    assert {row["evidence_id"] for row in revision["dependency_manifest"]} == set(facts)
    claims = revision["pages"][0]["generated_sections"][0]["sentences"]
    assert {claim["text"] for claim in claims if not claim["unsupported"]} == set(facts.values())

@pytest.mark.parametrize("extra_ranked_source", [False, True])
@respx.mock
async def test_uncorroborated_heading_neighbor_keeps_its_ranked_tail_position(
        actor_client, session, extra_ranked_source):
    """A page-local heading next to a facet head must not evict a ranked fact:
    without corroboration (identical block geometry or an independent rank
    within max_evidence + facet heads) the neighbor keeps its tail position
    and the ranked second hit still reaches the model. Live-stack shape:
    appendix/TOC headings consumed Wiki slots ahead of ranked facet hits."""
    resource, version, rows = await source_with(session, [
        "Alpha beta head block: minimum supply voltage 3 V.",
        "List of Tables",
        "Alpha beta second block: boot pin sampling at reset end.",
        ("Alpha zebra third block: sleep current 5 microamperes."
         if extra_ranked_source else "Unrelated packaging instructions."),
    ], pages=[0, 0, 1, 2])
    head, heading, second, _ = rows
    heading.bbox = [0, 0, 10, 10]
    await session.commit()

    def respond(request):
        prompt = json.loads(request.content)
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            payload = {"retrieval_queries": ["alpha beta head"]}
        elif "pages" in properties:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            payload = {"pages": [{"title": "Electrical settings", "sections": ["Specifications"],
                                  "references": [item["reference"] for item in context],
                                  "source_term": None}]}
        else:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: list(entry["spans"]) for entry in context}
            payload = {"sections": [{"heading": "Specifications", "sentences": [
                {"text": text, "support": [spans[identifier][0]], "conflict_group": None}
                for identifier, text in ((head.id, "最低供电电压为 3 V。"),
                                         (second.id, "启动引脚在复位结束时采样。"))
                if identifier in spans]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Alpha beta head zebra",
        max_pages=1, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "heading-keeps-tail-position"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    # Document-order fallback is not independent rank corroboration, even
    # when a sparse real retrieval leaves its position below the nominal cap.
    assert {row["evidence_id"] for row in revision["dependency_manifest"]} == {
        head.id, second.id}


@respx.mock
async def test_rank_corroborated_neighbor_still_joins_its_head(actor_client, session):
    """Corroboration is not demotion: a differently-laid-out neighbor that the
    ranker independently placed within max_evidence + facet heads still joins
    its head ahead of deeper tail filler. Live-stack shape: the encoder-stack
    block next to a figure caption keeps its slot."""
    resource, version, rows = await source_with(session, [
        "Figure diagram overview",
        "Overview encoder stack layers six identical",
        "Diagram overview appendix filler",
        "Unrelated index filler.",
    ], pages=[0, 0, 1, 2])
    caption, stack, filler, _ = rows
    caption.bbox = [0, 0, 10, 10]
    await session.commit()

    def respond(request):
        prompt = json.loads(request.content)
        properties = prompt["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            payload = {"retrieval_queries": ["figure diagram overview"]}
        elif "pages" in properties:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            payload = {"pages": [{"title": "Encoder", "sections": ["Layers"],
                                  "references": [item["reference"] for item in context],
                                  "source_term": None}]}
        else:
            context = json.loads(prompt["messages"][-1]["content"])["evidence"]
            spans = {entry["evidence_id"]: list(entry["spans"]) for entry in context}
            payload = {"sections": [{"heading": "Layers", "sentences": [
                {"text": "编码器由六个相同层堆叠。", "support": [spans[stack.id][0]],
                 "conflict_group": None}]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)},
                                                    "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, title="Figure overview and encoder stack",
        max_pages=1, max_evidence=2, max_input_chars=12000, max_output_tokens=2048),
        headers={"Idempotency-Key": "corroborated-neighbor-kept"})
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]
    # Focused keyword rank is caption, appendix filler, stack (strict 1/df
    # separation, no ties): the stack block sits just outside the 2-slot
    # budget but within max_evidence + facet heads, so it still joins its
    # caption head and the filler stays out.
    assert {row["evidence_id"] for row in revision["dependency_manifest"]} == {
        caption.id, stack.id}


def _budget_probe_model(*, length_stage, length_usage="absent"):
    """Truncate only the requested stage; optional statistics cannot mask that terminal reason."""
    def respond(request):
        request_body = json.loads(request.content)
        properties = request_body["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            stage = "query_planning"
            title = json.loads(request_body["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": [title]}
        elif "pages" in properties:
            stage = "plan"
            context = json.loads(request_body["messages"][-1]["content"])["evidence"]
            payload = {"pages": [{"title": "Overview", "sections": ["Facts"],
                                  "references": [item["reference"] for item in context],
                                  "source_term": None}]}
        elif "selected_relations" in properties:
            stage = "relations"
            payload = {"selected_relations": []}
        else:
            stage = "page"
            context = json.loads(request_body["messages"][-1]["content"])["evidence"]
            first = sorted(context[0]["spans"])[0]
            payload = {"sections": [{"heading": "Facts", "sentences": [
                {"text": "The supplied evidence states one fact.",
                 "support": [first], "conflict_group": None}]}]}
        response = {"choices": [{"message": {"content": json.dumps(payload)},
                                "finish_reason": "length" if stage == length_stage else "stop"}]}
        if stage == length_stage and length_usage != "absent":
            response["usage"] = None if length_usage == "null" else []
        return httpx.Response(200, json=response)

    return respx.post(CHAT).mock(side_effect=respond)


@pytest.mark.parametrize("length_usage", ["absent", "null", "non_object"])
@pytest.mark.parametrize("length_stage", ["plan", "page"])
@respx.mock
async def test_token_exhaustion_names_its_stage_and_request_cap(
        actor_client, session, length_stage, length_usage):
    """A truncated stage still fails 409 `wiki_budget_exceeded` without a partial
    revision, but the error now names the exhausted stage and its request cap so
    a budget failure is attributable instead of a guess."""
    resource, version, _, _ = await source(session)
    _budget_probe_model(length_stage=length_stage, length_usage=length_usage)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, max_pages=1, max_evidence=1, max_input_chars=12000,
        max_output_tokens=2048), headers={"Idempotency-Key": f"length-at-{length_stage}"})
    assert created.status_code == 409, created.text
    error = created.json()["error"]
    assert error["code"] == "wiki_budget_exceeded"
    assert length_stage in error["message"]
    assert "request cap" in error["message"]
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0


@pytest.mark.parametrize("case,plan_pages,message_part", [
    ("title", [{"title": "  ", "sections": ["Facts"], "references": [1], "source_term": None}],
     "planned page 1 title"),
    ("section", [{"title": "Overview", "sections": [""], "references": [1], "source_term": None}],
     "planned page 1 section 1"),
    ("anchor", [{"title": "Trailing colon:", "sections": ["Facts"], "references": [1],
                 "source_term": None}], "planned page 1 source anchor from title"),
])
@respx.mock
async def test_rejected_planner_text_names_its_page_and_field(
        actor_client, session, case, plan_pages, message_part):
    """Malformed planner text still fails 502 `wiki_generation_failed` without a
    partial revision, but the rejection names the page and field so the fault is
    locatable in the planner output."""
    resource, version, _, _ = await source(session)

    def respond(request):
        request_body = json.loads(request.content)
        properties = request_body["response_format"]["json_schema"]["schema"]["properties"]
        if "retrieval_queries" in properties:
            title = json.loads(request_body["messages"][-1]["content"])["title"]
            payload = {"retrieval_queries": [title]}
        else:
            payload = {"pages": plan_pages}
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]})

    respx.post(CHAT).mock(side_effect=respond)
    created = await actor_client.post("/api/wikis", json=body(
        resource, version, max_pages=1, max_evidence=1, max_input_chars=12000,
        max_output_tokens=2048), headers={"Idempotency-Key": f"planner-text-{case}"})
    assert created.status_code == 502, created.text
    error = created.json()["error"]
    assert error["code"] == "wiki_generation_failed"
    assert message_part in error["message"]
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0
