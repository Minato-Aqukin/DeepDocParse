"""P1 negative acceptance: asset isolation and permission checks on real HTTP routes."""
import httpx
import pytest
import respx
from sqlalchemy import select
from ddp_corpus.models import (
    Chunk, Conversation, Document, Evidence, Message, ParseJob, Resource, ResourceVersion,
    UploadEvent, new_id,
)
from tests.conftest import ACTOR, ORG, EMBEDDINGS, SERVICE, actor_headers, submit_document
from tests.test_documents import _mock_service


async def seed(session, *, owner=ACTOR, org=ORG, publication="private", text="secret plutonium"):
    document = Document(id=new_id(), uploaded_by=owner, organization_id=org,
        doc_id=new_id()*2, filename="confidential.pdf", object_key="private/source.pdf",
        index_status="ready")
    session.add(document)
    await session.flush()
    resource = Resource(id=new_id(), owner_id=owner, uploaded_by=owner, organization_id=org,
        display_name=document.filename, publication=publication)
    session.add(resource)
    await session.flush()
    job = ParseJob(id=new_id(), document_id=document.id, resource_id=resource.id,
        initiated_by=owner, engine="borndigital", options_hash=new_id()*2,
        status="succeeded", index_status="ready", result_prefix="results/private/", service_task_id=new_id())
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    version = ResourceVersion(id=new_id(), resource_id=resource.id, document_id=document.id,
        source_digest=document.doc_id, filename=document.filename, parse_job_id=job.id)
    evidence = Evidence(id=new_id(), document_id=document.id, parse_job_id=job.id,
                        atom_key="source", content=text)
    session.add_all([version, evidence])
    await session.flush()
    session.add(Chunk(id=new_id(), document_id=document.id, parse_job_id=job.id,
                      text=text, text_tokenized=text, evidence_id=evidence.id))
    await session.commit()
    return document, resource, version, job, evidence


@respx.mock
async def test_t01_t02_real_upload_has_independent_assets_and_retry_dedup(actor_client, app_state, session):
    _mock_service()
    first = await submit_document(actor_client, app_state.storage, b"same bytes", event_id="upload-a")
    repeat = await submit_document(actor_client, app_state.storage, b"same bytes", event_id="upload-a")
    second = await submit_document(actor_client, app_state.storage, b"same bytes", actor_id="bob", event_id="upload-b")
    assert first.status_code == second.status_code == 200
    assert repeat.status_code == 409
    assert first.json()["result_id"] == second.json()["result_id"]
    resources = list((await session.execute(select(Resource))).scalars())
    assert {r.owner_id for r in resources} == {ACTOR, "bob"}
    assert len(resources) == 2 and all(r.publication == "private" for r in resources)
    assert len(list((await session.execute(select(UploadEvent))).scalars())) == 2
    mine = (await actor_client.get("/api/v1/resources")).json()["items"]
    assert [r["owner_id"] for r in mine] == [ACTOR]
    bob = (await actor_client.get("/api/v1/resources", headers=actor_headers("bob"))).json()["items"]
    assert [r["owner_id"] for r in bob] == ["bob"]


@pytest.mark.parametrize("path", [
    "/api/documents/{doc}", "/api/documents/{doc}/jobs", "/api/documents/{doc}/result",
    "/api/documents/{doc}/pages", "/api/documents/{doc}/layout",
    "/api/documents/{doc}/source-url", "/api/documents/{doc}/download?format=source",
    "/api/documents/{doc}/jobs/{job}/images/a.png", "/api/documents/{doc}/crops/{job}/0_digest.png",
    "/api/evidence/{evidence}", "/v1/parse/{task}", "/v1/parse/{task}/result",
    "/internal/file-access/{doc}",
])
async def test_t05_private_multi_entry_read_denied_before_storage_or_upstream(actor_client, session, path):
    doc, resource, version, job, evidence = await seed(session, owner="bob")
    response = await actor_client.get(path.format(doc=doc.id, job=job.id,
        evidence=evidence.id, task=job.service_task_id), headers={"if-none-match": '"digest"'})
    assert response.status_code == 404, response.text
    assert "plutonium" not in response.text


async def test_t07_private_listing_statistics_and_history_do_not_disclose(actor_client, session):
    doc, resource, _, _, _ = await seed(session, owner="bob")
    c = Conversation(id=new_id(), actor_id=ACTOR, organization_id=ORG,
        document_id=doc.id, resource_id=resource.id, title="secret title")
    session.add(c)
    await session.flush()
    session.add(Message(conversation_id=c.id, role="assistant", content="secret plutonium"))
    await session.commit()
    assert (await actor_client.get("/api/documents")).json() == []
    assert (await actor_client.get("/api/documents/stats/summary")).json()["documents"] == 0
    assert (await actor_client.get("/api/conversations")).json() == []
    assert (await actor_client.get(f"/api/conversations/{c.id}/messages")).status_code == 404


@respx.mock
async def test_t06_search_scope_applies_before_candidate_limit(actor_client, session):
    await seed(session, owner="bob", text="secret secret secret secret")
    doc, _, _, _, _ = await seed(session, text="secret public fact")
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))
    response = await actor_client.get("/api/search?q=secret&limit=1")
    assert response.status_code == 200
    assert [g["document_id"] for g in response.json()["groups"]] == [doc.id]
    assert "secret secret" not in response.text


async def test_t32_same_subject_other_issuer_and_node_creds_not_owner(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    for headers in [actor_headers(ACTOR, organization_id="other-issuer"),
                    actor_headers(ACTOR, kind="service", role="admin")]:
        assert (await actor_client.get(f"/api/documents/{doc.id}", headers=headers)).status_code == 404
        assert (await actor_client.get(f"/api/resources/{resource.id}", headers=headers)).status_code == 404
    key = actor_headers("key-id", kind="api_key")
    key["X-DDP-User"] = ACTOR
    assert (await actor_client.get(f"/api/documents/{doc.id}", headers=key)).status_code == 200
    del key["X-DDP-User"]
    assert (await actor_client.get(f"/api/documents/{doc.id}", headers=key)).status_code == 404


async def test_t03_delete_one_resource_keeps_other_content_and_owner(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    other = Resource(id=new_id(), owner_id="bob", uploaded_by="bob", organization_id=ORG)
    session.add(other)
    await session.flush()
    session.add(ResourceVersion(resource_id=other.id, document_id=doc.id, source_digest=doc.doc_id))
    await session.commit()
    assert (await actor_client.delete(f"/api/resources/{resource.id}")).status_code == 204
    assert (await actor_client.get(f"/api/documents/{doc.id}")).status_code == 404
    assert (await actor_client.get(f"/api/documents/{doc.id}", headers=actor_headers("bob"))).status_code == 200
    await session.refresh(doc)
    assert doc.deleted_at is None and doc.object_key == "private/source.pdf"


async def test_t08_ambiguous_legacy_context_is_never_first_row(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    other = Resource(id=new_id(), owner_id=ACTOR, uploaded_by=ACTOR, organization_id=ORG)
    session.add(other)
    await session.flush()
    session.add(ResourceVersion(resource_id=other.id, document_id=doc.id, source_digest=doc.doc_id))
    await session.commit()
    ambiguous = await actor_client.get(f"/api/documents/{doc.id}")
    assert ambiguous.status_code == 409 and ambiguous.json()["error"]["code"] == "resource_context_required"
    assert (await actor_client.get(f"/api/documents/{doc.id}?resource_id={resource.id}")).status_code == 200
    assert (await actor_client.get(f"/api/documents/{doc.id}?resource_id=unknown")).status_code == 404


async def test_t02_create_api_retries_conflicts_and_explicit_new_asset(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    body = {"document_id": doc.id, "display_name": "my copy"}
    headers = {"Idempotency-Key": "copy-op"}
    first = await actor_client.post("/api/resources", json=body, headers=headers)
    again = await actor_client.post("/api/resources", json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    conflict = await actor_client.post("/api/resources", json={**body, "display_name": "changed"}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    different = await actor_client.post(f"/api/resources?resource_id={resource.id}", json=body,
                                        headers={"Idempotency-Key": "new-copy"})
    assert different.status_code == 201 and different.json()["id"] != first.json()["id"]


async def test_t09_catalog_explicit_local_scope_and_no_private_or_temporary(actor_client, session):
    await seed(session, owner="bob")
    doc, resource, _, _, _ = await seed(session, owner="charlie", publication="published")
    temporary, _, _, _, _ = await seed(session, publication="published")
    temporary.origin = "external"
    doc.index_status = "failed"  # The fixed parse remains ready; the compatibility mirror is irrelevant.
    await session.commit()
    response = (await actor_client.get("/api/v1/resources?scope=site_public")).json()
    assert [i["id"] for i in response["items"]] == [resource.id]
    assert response["coverage"]["scope"] == "site_public"
    assert "total" not in response and response["coverage"]["snapshot_complete"] is False
    assert (await actor_client.get("/api/v1/resources?scope=federation")).status_code == 422


async def test_t06_private_copy_cannot_publish_derived_content(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    made = await actor_client.post("/api/resources", json={"document_id": doc.id,
        "copied_from": resource.id}, headers={"Idempotency-Key": "derived"})
    assert made.status_code == 201
    response = await actor_client.patch(f"/api/resources/{made.json()['id']}", json={"publication": "published"})
    assert response.status_code == 403


async def test_publication_revocation_propagates_through_copy_chain(actor_client, session):
    doc, resource, _, _, _ = await seed(session, owner="bob", publication="published")
    child = Resource(id=new_id(), owner_id="charlie", uploaded_by="charlie", organization_id=ORG,
                     publication="published", copied_from=resource.id)
    session.add(child)
    await session.flush()
    session.add(ResourceVersion(resource_id=child.id, document_id=doc.id, source_digest=doc.doc_id))
    await session.commit()
    assert (await actor_client.get(f"/api/resources/{child.id}")).status_code == 200
    resource.publication = "withdrawn"
    await session.commit()
    assert (await actor_client.get(f"/api/resources/{child.id}")).status_code == 404
    assert (await actor_client.get(f"/api/documents/{doc.id}?resource_id={child.id}")).status_code == 404


async def test_t09_temporary_resource_cannot_be_published(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    doc.origin = "external"
    await session.commit()
    response = await actor_client.patch(f"/api/resources/{resource.id}", json={"publication": "published"})
    assert response.status_code == 409
    await session.refresh(resource)
    assert resource.publication == "private"


async def test_t07_history_remains_bound_to_original_resource_after_same_bytes_publish(actor_client, session):
    doc, resource, _, _, _ = await seed(session)
    response = await actor_client.post(f"/api/documents/{doc.id}/conversations")
    assert response.status_code == 201
    cid = response.json()["id"]
    unrelated = Resource(id=new_id(), owner_id="bob", uploaded_by="bob", organization_id=ORG,
                         publication="published")
    session.add(unrelated)
    await session.flush()
    session.add(ResourceVersion(resource_id=unrelated.id, document_id=doc.id, source_digest=doc.doc_id))
    await session.commit()
    assert (await actor_client.delete(f"/api/resources/{resource.id}")).status_code == 204
    assert (await actor_client.get(f"/api/documents/{doc.id}")).status_code == 200
    assert (await actor_client.get(f"/api/conversations/{cid}/messages")).status_code == 404


async def test_t04_same_filename_new_content_creates_fixed_version(actor_client, session):
    doc, resource, first_version, _, _ = await seed(session)
    next_doc, _, _, _, _ = await seed(session)
    result = await actor_client.post(f"/api/resources/{resource.id}/versions",
        json={"document_id": next_doc.id, "display_name": doc.filename},
        headers={"Idempotency-Key": "version-2"})
    assert result.status_code == 201, result.text
    assert [v["version_no"] for v in result.json()["versions"]] == [1, 2]
    old = await actor_client.get(f"/api/resources/{resource.id}/versions/{first_version.id}")
    assert old.status_code == 200
    assert old.json()["document_id"] == doc.id and old.json()["source_digest"] == doc.doc_id
    assert old.json()["source_digest"] != result.json()["versions"][1]["source_digest"]
    assert (await actor_client.patch(f"/api/resources/{resource.id}/versions/{first_version.id}",
                                     json={"document_id": next_doc.id})).status_code == 405


async def test_private_evidence_and_unknown_evidence_are_indistinguishable(actor_client, session):
    _, _, _, _, evidence = await seed(session, owner="bob")
    denied = await actor_client.get(f"/api/evidence/{evidence.id}")
    unknown = await actor_client.get("/api/evidence/does-not-exist")
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json()


async def test_published_resources_never_cross_the_organization_boundary(actor_client, session):
    """企业边界 8：发布只在本组织内公开。

    旧行为：`resource_condition` 的 published 分支没有组织谓词 —— 别的组织的
    调用者能读到本组织已发布的资源、版本、证据，并在 site_public 目录里列出它。
    同组织的调用者行为不变（单组织部署下这就是"全站"）。
    """
    doc, resource, version, _, evidence = await seed(session, owner="bob", publication="published",
                                                     text="published plutonium")
    same_org = actor_headers("charlie")
    foreign = actor_headers("mallory", organization_id="org-foreign")

    assert (await actor_client.get(f"/api/v1/resources/{resource.id}", headers=same_org)).status_code == 200
    listed = (await actor_client.get("/api/v1/resources?scope=site_public", headers=same_org)).json()
    assert [item["id"] for item in listed["items"]] == [resource.id]
    assert (await actor_client.get(f"/api/evidence/{evidence.id}", headers=same_org)).status_code == 200

    denied = await actor_client.get(f"/api/v1/resources/{resource.id}", headers=foreign)
    unknown = await actor_client.get(f"/api/v1/resources/{new_id()}", headers=foreign)
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json(), "越界与不存在同形"
    assert (await actor_client.get(f"/api/v1/resources/{resource.id}/versions/{version.id}",
                                   headers=foreign)).status_code == 404
    assert (await actor_client.get("/api/v1/resources?scope=site_public",
                                   headers=foreign)).json()["items"] == []
    assert (await actor_client.get(f"/api/evidence/{evidence.id}", headers=foreign)).status_code == 404
    assert (await actor_client.get(f"/api/documents/{doc.id}", headers=foreign)).status_code == 404


@pytest.mark.parametrize("operation", ["reparse", "current-job", "reindex", "verification"])
async def test_public_reader_cannot_mutate_shared_document_state(actor_client, session, operation):
    from ddp_corpus.models import EvidenceVerification
    doc, resource, _, job, evidence = await seed(session, owner="bob", publication="published")
    before = (doc.current_job_id, doc.index_status, doc.index_generation,
              job.index_status, job.index_generation, evidence.review_state)
    if operation == "verification":
        response = await actor_client.post(f"/api/evidence/{evidence.id}/verification", json={"verdict": "pass"})
    elif operation == "current-job":
        response = await actor_client.put(f"/api/documents/{doc.id}/current-job", json={"job_id": job.id})
    else:
        response = await actor_client.post(f"/api/documents/{doc.id}/{operation}", json={"engine": "borndigital"})
    assert response.status_code == 404, response.text
    await session.refresh(doc)
    await session.refresh(evidence)
    await session.refresh(job)
    assert (doc.current_job_id, doc.index_status, doc.index_generation,
            job.index_status, job.index_generation, evidence.review_state) == before
    assert list((await session.execute(select(EvidenceVerification))).scalars()) == []
    assert len(list((await session.execute(select(ParseJob))).scalars())) == 1


@respx.mock
@pytest.mark.parametrize("operation", ["reparse", "current-job", "reindex"])
async def test_shared_content_owner_mutates_only_their_own_parse_scope(actor_client, session, app_state, operation):
    import json
    from tests.conftest import drain_tasks
    _mock_service()
    doc, alice, alice_version, alice_job, alice_evidence = await seed(session, text="Alice private annotation")
    bob = Resource(id=new_id(), owner_id="bob", uploaded_by="bob", organization_id=ORG)
    session.add(bob)
    await session.flush()
    bob_job = ParseJob(id=new_id(), document_id=doc.id, resource_id=bob.id, initiated_by="bob",
        engine="borndigital", options_hash=new_id()*2, document_version=2,
        status="succeeded", index_status="ready", result_prefix="results/bob/")
    session.add(bob_job)
    await session.flush()
    bob_version = ResourceVersion(id=new_id(), resource_id=bob.id, document_id=doc.id,
        source_digest=doc.doc_id, parse_job_id=bob_job.id, filename="bob.pdf")
    session.add_all([bob_version, Chunk(id=new_id(), document_id=doc.id,
        parse_job_id=bob_job.id, text="Bob old index", text_tokenized="Bob old index")])
    await session.commit()
    alice_before = (await actor_client.get(f"/api/resources/{alice.id}")).json()
    alice_index_before = (alice_job.index_status, alice_job.index_generation, alice_job.compile_status,
                          alice_job.result_prefix, alice_job.status)
    alice_chunks_before = list((await session.execute(select(Chunk.id, Chunk.text, Chunk.parse_job_id).where(
        Chunk.parse_job_id == alice_job.id))).all())
    doc_before = (doc.current_job_id, doc.index_status, doc.index_generation)
    suffix = f"?resource_id={bob.id}"
    headers = actor_headers("bob")
    if operation == "current-job":
        candidate = ParseJob(id=new_id(), document_id=doc.id, resource_id=bob.id, initiated_by="bob",
            engine="borndigital", options_hash=new_id()*2, document_version=3,
            status="succeeded", index_status="ready", result_prefix="results/bob-next/")
        session.add(candidate)
        await session.flush()
        session.add(Chunk(document_id=doc.id, parse_job_id=candidate.id,
                          text="Bob revised index", text_tokenized="Bob revised index"))
        await session.commit()
        response = await actor_client.put(f"/api/documents/{doc.id}/current-job{suffix}",
            json={"job_id": candidate.id}, headers=headers)
        assert response.status_code == 200, response.text
        versions = (await actor_client.get(f"/api/resources/{bob.id}/versions", headers=headers)).json()
        assert len(versions) == 2
        assert [v["parse_job_id"] for v in versions] == [bob_job.id, candidate.id]
        assert versions[0]["id"] == bob_version.id
    elif operation == "reindex":
        layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [{
            "type": "text", "bbox": [72, 72, 540, 100],
            "lines": [{"spans": [{"content": "Bob rebuilt index"}]}]}]}]}
        await app_state.storage.put(f"{bob_job.result_prefix}layout.json", json.dumps(layout).encode(), "application/json")
        response = await actor_client.post(f"/api/documents/{doc.id}/reindex{suffix}", headers=headers)
        assert response.status_code == 202, response.text
        await drain_tasks(app_state)
        await session.refresh(bob_job)
        assert bob_job.index_status == "ready" and bob_job.index_generation > 0
        bob_chunks = list((await session.execute(select(Chunk.text).where(Chunk.parse_job_id == bob_job.id))).scalars())
        assert bob_chunks == ["Bob rebuilt index"]
    else:
        response = await actor_client.post(f"/api/documents/{doc.id}/reparse{suffix}",
            json={"engine": "borndigital", "options": {"scale": 3}}, headers=headers)
        assert response.status_code == 202, response.text
        new_job = await session.get(ParseJob, response.json()["id"])
        assert new_job.id not in (alice_job.id, bob_job.id)
        assert new_job.resource_id == bob.id and new_job.initiated_by == "bob"
        assert new_job.status == "pending"
        await session.refresh(bob_version)
        assert bob_version.parse_job_id == bob_job.id
    await session.refresh(doc)
    await session.refresh(alice_job)
    await session.refresh(alice_version)
    await session.refresh(alice_evidence)
    assert (doc.current_job_id, doc.index_status, doc.index_generation) == doc_before
    assert (alice_job.index_status, alice_job.index_generation, alice_job.compile_status,
            alice_job.result_prefix, alice_job.status) == alice_index_before
    assert alice_version.parse_job_id == alice_job.id and alice_evidence.review_state == "unreviewed"
    assert list((await session.execute(select(Chunk.id, Chunk.text, Chunk.parse_job_id).where(
        Chunk.parse_job_id == alice_job.id))).all()) == alice_chunks_before
    assert (await actor_client.get(f"/api/resources/{alice.id}")).json() == alice_before


async def test_global_human_review_still_refuses_shared_asset_writes(actor_client, session):
    doc, resource, _, job, evidence = await seed(session)
    other = Resource(id=new_id(), owner_id="bob", uploaded_by="bob", organization_id=ORG)
    session.add(other)
    await session.flush()
    session.add(ResourceVersion(resource_id=other.id, document_id=doc.id, source_digest=doc.doc_id))
    await session.commit()
    response = await actor_client.post(f"/api/evidence/{evidence.id}/verification?resource_id={resource.id}",
                                      json={"verdict": "pass"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "shared_document_write_unsupported"
    await session.refresh(evidence)
    assert evidence.review_state == "unreviewed"


async def test_metadata_copy_cannot_rebuild_its_parents_parse(actor_client, session):
    doc, parent, parent_version, job, _ = await seed(session, owner="bob", publication="published")
    copied = await actor_client.post("/api/resources", json={"document_id": doc.id},
                                     headers={"Idempotency-Key": "borrowed-job"})
    assert copied.status_code == 201, copied.text
    asset = (await actor_client.get(f"/api/resources/{copied.json()['id']}")).json()
    response = await actor_client.post(f"/api/documents/{doc.id}/reindex?resource_id={asset['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "shared_parse_write_unsupported"
    await session.refresh(job)
    await session.refresh(parent_version)
    assert job.index_status == "ready" and job.index_generation == 0
    assert parent_version.parse_job_id == job.id
    assert (await actor_client.get(f"/api/resources/{asset['id']}")).json()["versions"] == asset["versions"]


async def test_omitted_copy_source_cannot_launder_publication_or_revocation(actor_client, session):
    doc, source, _, _, _ = await seed(session, owner="bob", publication="published")
    made = await actor_client.post("/api/resources", json={"document_id": doc.id},
                                   headers={"Idempotency-Key": "cannot-launder"})
    assert made.status_code == 201, made.text
    copy = made.json()
    assert copy["copied_from"] == source.id
    assert (await actor_client.patch(f"/api/resources/{copy['id']}", json={"publication": "published"})).status_code == 200
    source.publication = "withdrawn"
    await session.commit()
    assert (await actor_client.get(f"/api/resources/{copy['id']}", headers=actor_headers("charlie"))).status_code == 404


async def test_added_borrowed_version_preserves_source_dependency(actor_client, session):
    _, own, _, _, _ = await seed(session)
    borrowed, source, _, _, _ = await seed(session, owner="bob", publication="published")
    result = await actor_client.post(f"/api/resources/{own.id}/versions",
        json={"document_id": borrowed.id}, headers={"Idempotency-Key": "borrow-version"})
    assert result.status_code == 201, result.text
    assert result.json()["copied_from"] == source.id
    assert (await actor_client.patch(f"/api/resources/{own.id}", json={"publication": "published"})).status_code == 200
    source.publication = "withdrawn"
    await session.commit()
    assert (await actor_client.get(f"/api/resources/{own.id}", headers=actor_headers("charlie"))).status_code == 404


async def test_metadata_version_failure_does_not_mutate_source_lineage(actor_client, session):
    doc, target, _, _, _ = await seed(session)
    borrowed, source, _, _, _ = await seed(session, owner="bob", publication="published")
    added = await actor_client.post(f"/api/resources/{target.id}/versions",
        json={"document_id": borrowed.id}, headers={"Idempotency-Key": "source-one"})
    assert added.status_code == 201
    other_doc, other_source, _, _, _ = await seed(session, owner="charlie", publication="published")
    rejected = await actor_client.post(f"/api/resources/{target.id}/versions",
        json={"document_id": other_doc.id}, headers={"Idempotency-Key": "source-two"})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "resource_lineage_conflict"
    await session.refresh(target)
    assert target.copied_from == source.id
    assert len((await actor_client.get(f"/api/resources/{target.id}/versions")).json()) == 2
    # A duplicate version must fail before it can reset the parent to the target itself.
    duplicate = await actor_client.post(f"/api/resources/{target.id}/versions?resource_id={target.id}",
        json={"document_id": doc.id}, headers={"Idempotency-Key": "same-content"})
    assert duplicate.status_code == 409
    await session.refresh(target)
    assert target.copied_from == source.id


async def test_metadata_version_rejects_descendant_parent_cycle(actor_client, session):
    doc, target, _, _, _ = await seed(session)
    other_doc, descendant, _, _, _ = await seed(session)
    descendant.copied_from = target.id
    await session.commit()
    response = await actor_client.post(f"/api/resources/{target.id}/versions",
        json={"document_id": other_doc.id}, headers={"Idempotency-Key": "cycle"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "resource_lineage_cycle"
    await session.refresh(target)
    assert target.copied_from is None


async def test_unbound_legacy_history_cannot_adopt_a_later_public_asset(actor_client, session):
    from ddp_corpus.models import ExtractionItem, ExtractionRun
    doc, public_asset, _, _, _ = await seed(session, owner="bob", publication="published")
    conversation = Conversation(id=new_id(), document_id=doc.id, actor_id=ACTOR,
        organization_id=ORG, resource_id=None, title="old private answer")
    run = ExtractionRun(id=new_id(), actor_id=ACTOR, organization_id=ORG,
        name="old extraction", resource_context={}, status="succeeded")
    session.add_all([conversation, run])
    await session.flush()
    session.add_all([Message(conversation_id=conversation.id, role="assistant", content="legacy secret"),
        ExtractionItem(run_id=run.id, document_id=doc.id, fields={"secret": {"value": "legacy secret"}})])
    await session.commit()
    assert (await actor_client.get(f"/api/documents/{doc.id}")).status_code == 200
    for suffix in ("", f"?resource_id={public_asset.id}"):
        assert (await actor_client.get(f"/api/conversations/{conversation.id}/messages{suffix}")).status_code == 404
        assert (await actor_client.get(f"/api/extractions/runs/{run.id}{suffix}")).status_code == 404
    assert (await actor_client.get("/api/conversations")).json() == []
    assert (await actor_client.get("/api/extractions/runs")).json() == []


@respx.mock
async def test_pending_same_bytes_uploads_have_independent_attempts_and_explicit_bindings(actor_client, app_state, session):
    import json
    routes = _mock_service()
    first = await submit_document(actor_client, app_state.storage, b"same pending bytes", event_id="pending-a")
    second = await submit_document(actor_client, app_state.storage, b"same pending bytes", actor_id="bob", event_id="pending-b")
    assert first.status_code == second.status_code == 200
    jobs = list((await session.execute(select(ParseJob).order_by(ParseJob.document_version))).scalars())
    assert len(jobs) == 2 and {j.initiated_by for j in jobs} == {ACTOR, "bob"}
    versions = list((await session.execute(select(ResourceVersion))).scalars())
    assert {v.parse_job_id for v in versions} == {j.id for j in jobs}
    assert all(v.parse_job_id is not None for v in versions)
    assert all(next(j for j in jobs if j.id == v.parse_job_id).resource_id == v.resource_id for v in versions)
    payloads = [json.loads(call.request.content) for call in routes["submit"].calls]
    assert len({p["doc_id"] for p in payloads}) == 2
    grants = [json.loads(call.request.content) for call in routes["file_grant"].calls]
    assert {g["subject_id"] for g in grants} == {ACTOR, "bob"}
    assert len({g["resource_id"] for g in grants}) == 2
    alice_asset = next(j.resource_id for j in jobs if j.initiated_by == ACTOR)
    bob_job = next(j for j in jobs if j.initiated_by == "bob")
    assert (await actor_client.delete(f"/api/resources/{alice_asset}")).status_code == 204
    access = await actor_client.get(f"/internal/file-access/{bob_job.document_id}?resource_id={bob_job.resource_id}",
                                    headers=actor_headers("bob"))
    assert access.status_code == 200 and access.json()["object_key"]
    await session.refresh(bob_job)
    assert bob_job.status == "pending" and bob_job.resource_id != alice_asset


@respx.mock
async def test_extraction_rechecks_permission_before_each_model_dispatch(actor_client, session, app_state, monkeypatch):
    import json
    from ddp_core.extract_format import parse_schema
    from ddp_corpus.config import settings
    from ddp_corpus.errors import APIError
    from ddp_corpus.models import ExtractionItem, ExtractionRun
    from ddp_corpus.routers.extractions import _extract_one
    from tests.conftest import CHAT

    doc, source, _, _, _ = await seed(session, owner="bob", publication="published")
    run = ExtractionRun(id=new_id(), actor_id=ACTOR, organization_id=ORG,
        resource_context={"principal_id": ACTOR, "resources": {doc.id: source.id}})
    session.add(run)
    await session.commit()
    monkeypatch.setattr(settings, "extract_concurrency", 1)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))

    async def revoke_after_first_model(request):
        assert "secret plutonium" in json.loads(request.content)["messages"][-1]["content"]
        source.publication = "withdrawn"
        await session.commit()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"found": True, "value": "secret plutonium", "source": 1})}}]})

    chat = respx.post(CHAT).mock(side_effect=revoke_after_first_model)
    spec = parse_schema({"type": "object", "properties": {
        "secret_a": {"type": "string", "description": "secret plutonium"},
        "secret_b": {"type": "string", "description": "secret plutonium"}}})
    with pytest.raises(APIError):
        await _extract_one(session, run.id, doc.id, spec, storage=app_state.storage,
                           http=app_state.http, index=app_state.search_index, verify=False)
    assert chat.call_count == 1, "revoked source must not enter a later field's model prompt"
    assert await session.scalar(select(ExtractionItem.id).where(ExtractionItem.run_id == run.id)) is None


@respx.mock
async def test_same_content_private_parse_outputs_never_enter_other_asset_models(actor_client, session, app_state, monkeypatch):
    import json
    from ddp_corpus.config import settings
    from ddp_corpus.deps import Actor
    from ddp_corpus.qa import retrieve
    from ddp_corpus.models import ExtractionRun
    from ddp_corpus.routers.extractions import _extract_one
    from ddp_core.extract_format import parse_schema
    from tests.conftest import CHAT

    doc, alice, _, private_job, private_ev = await seed(session, text="secret Alice annotation")
    bob = Resource(id=new_id(), owner_id="bob", uploaded_by="bob", organization_id=ORG)
    session.add(bob)
    await session.flush()
    bob_job = ParseJob(id=new_id(), document_id=doc.id, resource_id=bob.id, initiated_by="bob",
        engine="borndigital", options_hash=new_id()*2, status="succeeded", index_status="ready", result_prefix="results/bob/",
        document_version=2)
    session.add(bob_job)
    await session.flush()
    bob_version = ResourceVersion(id=new_id(), resource_id=bob.id, document_id=doc.id,
        parse_job_id=bob_job.id, source_digest=doc.doc_id, filename="bob.pdf")
    bob_evidence = Evidence(id=new_id(), document_id=doc.id, parse_job_id=bob_job.id,
        atom_key="source", content="secret Bob fact")
    session.add_all([bob_version, bob_evidence])
    await session.flush()
    session.add(Chunk(id=new_id(), document_id=doc.id, parse_job_id=bob_job.id,
        text="secret Bob fact", text_tokenized="secret Bob fact", evidence_id=bob_evidence.id))
    run = ExtractionRun(id=new_id(), actor_id="bob", organization_id=ORG,
        resource_context={"principal_id": "bob", "resources": {doc.id: bob.id},
                          "versions": {doc.id: bob_version.id}})
    session.add(run)
    doc.index_status = "failed"  # Another asset's mirror cannot disable Bob's ready fixed parse.
    await session.commit()
    assert doc.current_job_id == private_job.id
    headers = actor_headers("bob")
    suffix = f"?resource_id={bob.id}"
    assert (await actor_client.get(f"/api/evidence/{private_ev.id}{suffix}", headers=headers)).status_code == 404
    assert (await actor_client.get(f"/api/documents/{doc.id}/crops/{private_job.id}/0_digest.png{suffix}",
        headers={**headers, "if-none-match": '"digest"'})).status_code == 404
    own_evidence = await actor_client.get(f"/api/evidence/{bob_evidence.id}{suffix}", headers=headers)
    assert own_evidence.status_code == 200
    assert own_evidence.json()["source_version_id"] == bob_version.id
    monkeypatch.setattr(settings, "rerank_enabled", False)
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))
    actor = Actor(id="bob", kind="user", role="contributor", organization_id=ORG, resource_id=bob.id)
    retrieved = await retrieve(session, app_state.search_index, app_state.http,
        question="secret", document=doc, actor=actor)
    assert [hit["parse_job_id"] for hit in retrieved.hits] == [bob_job.id]

    def model(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        assert "secret Bob fact" in prompt and "Alice" not in prompt
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"found": True, "value": "Bob fact", "source": 1})}}]})
    chat = respx.post(CHAT).mock(side_effect=model)
    await _extract_one(session, run.id, doc.id,
        parse_schema({"type": "object", "properties": {"secret": {"type": "string"}}}),
        storage=app_state.storage, http=app_state.http, index=app_state.search_index, verify=False)
    assert chat.call_count == 1
    result = await actor_client.get(f"/api/extractions/runs/{run.id}", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["filename"] == "bob.pdf"
    assert "confidential.pdf" not in result.text


@respx.mock
async def test_duplicate_upload_retains_new_bytes_until_new_asset_is_registered(actor_client, app_state, session, monkeypatch):
    _mock_service()
    await submit_document(actor_client, app_state.storage, b"same bytes with GC", event_id="gc-a")
    original_delete = app_state.storage.delete
    observations = []

    async def checked_delete(key):
        asset = await session.scalar(select(Resource).where(Resource.owner_id == "bob", Resource.deleted_at.is_(None)))
        observations.append(asset.id if asset else None)
        await original_delete(key)

    monkeypatch.setattr(app_state.storage, "delete", checked_delete)
    second = await submit_document(actor_client, app_state.storage, b"same bytes with GC",
                                   actor_id="bob", event_id="gc-b")
    assert second.status_code == 200
    assert observations and all(observations), "duplicate bytes are only discarded after registration"


async def test_pending_fixed_version_cannot_borrow_another_assets_ready_index_for_publication(actor_client, session):
    doc, source, version, job, _ = await seed(session)
    pending = ParseJob(id=new_id(), document_id=doc.id, resource_id=source.id, initiated_by=ACTOR,
        engine="borndigital", options_hash=new_id()*2, document_version=2, status="pending")
    session.add(pending)
    await session.flush()
    version.parse_job_id = pending.id
    await session.commit()
    assert doc.index_status == "ready" and job.status == "succeeded"
    response = await actor_client.patch(f"/api/resources/{source.id}", json={"publication": "published"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "resource_not_publishable"
    source.publication = "published"  # Simulate an already-published legacy record.
    await session.commit()
    assert (await actor_client.get("/api/resources?scope=site_public")).json()["items"] == []
