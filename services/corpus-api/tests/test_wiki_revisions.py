"""Versioned Wiki invariants use actual routes and persistent SQL rows."""
import hashlib
import json
from datetime import timedelta

import httpx
import respx
from sqlalchemy import func, select

from ddp_corpus.models import (
    ClaimEvidenceBinding, DependencyManifest, Document, Evidence, ParseJob, Resource,
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
    await session.commit()
    return resource, version, evidence, document


def body(resource, version, **kwargs):
    return {"title": "Manual", "sources": [{"resource_id": resource.id,
            "source_version_id": version.id}], **kwargs}


def model(evidence, *, title="Overview", cited=True):
    def respond(request):
        prompt = json.loads(request.content)
        assert prompt["max_tokens"] > 0
        planning = "Plan a Wiki" in prompt["messages"][0]["content"]
        payload = {"pages": [{"title": title, "sections": ["Facts"]}]} if planning else {
            "sections": [{"heading": "Facts", "sentences": [{"text": "Original fact.",
                "evidence_ids": [evidence.id] if cited else ["invented-evidence"]}]}]}
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
    assert await session.scalar(select(func.count()).select_from(ClaimEvidenceBinding)) == 1
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
async def test_private_context_cannot_launder_via_public_citation(actor_client, session):
    private, pv, _, _ = await source(session, text="Private fact.")
    public, uv, ue, _ = await source(session, publication="published", text="Public fact.")
    model(ue)
    request = body(private, pv)
    request["sources"].append({"resource_id": public.id, "source_version_id": uv.id})
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
            "pages": [{"title": "Overview", "sections": ["Facts"]}]})}}]})
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
        "content": json.dumps({"pages": [{"title": "A"}, {"title": "B"}]})}}]}))
    too_many = await actor_client.post("/api/wikis", json=body(resource, version, max_pages=1),
                                       headers={"Idempotency-Key": "budget"})
    assert too_many.status_code == 409 and too_many.json()["error"]["code"] == "wiki_budget_exceeded"
    assert await session.scalar(select(func.count()).select_from(WikiRevision)) == 0
    assert await session.scalar(select(func.count()).select_from(DependencyManifest)) == 0


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
async def test_legacy_generators_isolated_by_author_and_exact_resource(actor_client, session):
    from ddp_corpus.models import KnowledgeEntity
    resource, version, evidence, document = await source(session, publication="published")
    legacy_model(evidence)
    request = {"evidence_ids": [evidence.id]}
    path = f"/api/knowledge/build?resource_id={resource.id}"
    alice = await actor_client.post(path, json=request)
    assert alice.status_code == 201, alice.text
    alice_wiki = (await actor_client.get("/api/wiki")).json()[0]
    # A public original does not publish its author's generated draft.
    assert (await actor_client.get("/api/wiki", headers=actor_headers("bob"))).json() == []
    bob = await actor_client.post(path, json=request, headers=actor_headers("bob"))
    assert bob.status_code == 201, bob.text
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

