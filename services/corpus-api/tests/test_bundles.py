"""T11/T12: actual HTTP import/export with the production actor and resource ACL."""

import io
import json
from datetime import timedelta

import pytest
from ddp_bundle_fixture import ORIGINAL, sample_bundle, sample_parts
from ddp_core.bundle import build_bundle, digest, json_bytes, read_bundle
from ddp_corpus import db
from ddp_corpus.config import settings
from ddp_corpus.gc import collect_deleted_objects
from ddp_corpus.models import Document, Resource, ResourceVersion, utcnow
from sqlalchemy import func, select
from tests.conftest import actor_headers


def upload_headers(key="import-1", **kwargs):
    return {**actor_headers(**kwargs), "Idempotency-Key": key, "Content-Type": "application/zip"}


async def import_one(client, **kwargs):
    response = await client.post(
        "/api/bundles/import", content=sample_bundle(), headers=upload_headers(**kwargs)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_t11_import_export_keeps_source_version_evidence_and_private_local_owner(
    actor_client, session
):
    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    assert resource.owner_id == "actor-alice" and resource.publication == "private"
    assert resource.uploaded_by == "actor-alice"
    assert result["source"]["uploader_ref"] == "original-uploader"
    assert result["source"]["resource_id"] != result["resource_id"]
    path = f"/api/resources/{resource.id}/versions/{result['source_version_id']}/bundle"
    response = await actor_client.get(path)
    assert response.status_code == 200, response.text
    actual = read_bundle(io.BytesIO(response.content))
    expected = read_bundle(io.BytesIO(sample_bundle()))
    assert actual.source == expected.source
    assert actual.files == expected.files
    assert actual.evidence == expected.evidence
    evidence = await actor_client.get(path + "/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["semantic_review"] == "needs_review"
    assert evidence.json()["evidence"] == expected.evidence
    for endpoint in (path, path + "/evidence"):
        other = await actor_client.get(endpoint, headers=actor_headers("actor-bob"))
        assert other.status_code == 404


async def test_t11_idempotency_same_actor_key_body_and_independent_owners(actor_client, session):
    first = await import_one(actor_client)
    retry = await actor_client.post(
        "/api/bundles/import", content=sample_bundle(), headers=upload_headers()
    )
    assert retry.status_code == 200
    assert retry.json()["resource_id"] == first["resource_id"]
    conflict = await actor_client.post(
        "/api/bundles/import",
        content=sample_bundle(filename="renamed.pdf"),
        headers=upload_headers(),
    )
    assert conflict.status_code == 409
    bob = await import_one(actor_client, actor_id="actor-bob")
    assert bob["resource_id"] != first["resource_id"]
    assert (await session.scalar(select(func.count()).select_from(Resource))) == 2


async def test_t03_delete_first_copy_and_collect_cannot_erase_second(
    actor_client, session, app_state
):
    first = await import_one(actor_client)
    second = await import_one(actor_client, actor_id="actor-bob")
    resource = await session.get(Resource, first["resource_id"])
    version = await session.get(ResourceVersion, first["source_version_id"])
    aged = utcnow() - timedelta(seconds=settings.gc_grace_seconds + 10)
    resource.deleted_at = version.deleted_at = aged
    document = await session.get(Document, first["document_id"])
    document.deleted_at = aged  # also protect against a legacy content tombstone
    await session.commit()
    before = dict(app_state.storage.objects)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert app_state.storage.objects == before
    endpoint = (
        f"/api/resources/{second['resource_id']}/versions/{second['source_version_id']}/bundle"
    )
    response = await actor_client.get(endpoint, headers=actor_headers("actor-bob"))
    assert response.status_code == 200
    assert read_bundle(io.BytesIO(response.content)).files["source.bin"] == ORIGINAL


async def test_t12_invalid_archive_never_publishes_or_writes_storage(
    actor_client, session, app_state
):
    response = await actor_client.post(
        "/api/bundles/import", content=b"not a ZIP", headers=upload_headers()
    )
    assert response.status_code == 422
    assert (await session.scalar(select(func.count()).select_from(Resource))) == 0
    assert not app_state.storage.objects


async def test_t13_missing_original_cannot_claim_known_private_content(
    actor_client, session, app_state
):
    first = await import_one(actor_client)
    before = dict(app_state.storage.objects)
    response = await actor_client.post(
        "/api/bundles/import",
        content=sample_bundle(missing=True),
        headers=upload_headers(actor_id="actor-bob"),
    )
    assert response.status_code == 409
    assert response.json()["state"] == "source_missing"
    assert (await session.scalar(select(func.count()).select_from(Resource))) == 1
    assert app_state.storage.objects == before
    resource = await session.get(Resource, first["resource_id"])
    assert resource.owner_id == "actor-alice"

    # 存在性信息也不许外泄（T13 判据后半句）：摘要指向"别人已有的私有文件"与指向
    # "从没见过的内容"，响应必须同形 —— 否则摘要本身就成了存在性探针。
    unknown_source, unknown_files = sample_parts(missing=True)
    unseen = digest(b"%PDF-1.4\nnever uploaded anywhere\n%%EOF\n")
    unknown_source["source_digest"] = unseen
    records = json.loads(unknown_files["evidence.json"])
    records[0]["evidence"]["source_digest"] = unseen
    unknown_files["evidence.json"] = json_bytes(records)
    unknown = await actor_client.post(
        "/api/bundles/import",
        content=build_bundle(unknown_source, unknown_files),
        headers=upload_headers(key="import-unknown", actor_id="actor-bob"),
    )
    assert unknown.status_code == response.status_code
    known_body, unknown_body = response.json(), unknown.json()
    assert unknown_body["source"]["source_digest"] == unseen
    assert set(known_body["source"]) == set(unknown_body["source"])
    known_body.pop("source")
    unknown_body.pop("source")
    assert known_body == unknown_body, "已知与未知摘要的响应不同形，等于泄露存在性"


async def test_storage_failure_never_publishes_ready_version(
    actor_client, session, app_state, monkeypatch
):
    original_put = app_state.storage.put

    async def broken_put(key, data, mime):
        await original_put(key, data, mime)
        if key.endswith("evidence.json"):
            raise OSError("storage interrupted")

    monkeypatch.setattr(app_state.storage, "put", broken_put)
    with pytest.raises(OSError, match="storage interrupted"):
        await actor_client.post(
            "/api/bundles/import", content=sample_bundle(), headers=upload_headers()
        )
    assert (await session.scalar(select(func.count()).select_from(Resource))) == 0
    assert not app_state.storage.objects


async def test_bundle_entrypoints_require_service_and_principal(client):
    denied = await client.post(
        "/api/bundles/import",
        content=sample_bundle(),
        headers={"Content-Type": "application/zip", "Idempotency-Key": "x"},
    )
    assert denied.status_code == 401
    for options in (
        {"role": "viewer"},
        {"actor_id": "service", "kind": "service", "role": "admin"},
    ):
        response = await client.post(
            "/api/bundles/import", content=sample_bundle(), headers=upload_headers(**options)
        )
        assert response.status_code == 403


async def test_native_export_freezes_parse_revision_and_reports_unbound_parse(
    actor_client, session, app_state, monkeypatch
):
    import hashlib

    from ddp_core.bundle import json_bytes
    from ddp_corpus.models import Evidence, ParseJob, new_id

    monkeypatch.setattr(settings, "bundle_node_id", "node-centre")
    document = Document(
        id=new_id(),
        uploaded_by="actor-alice",
        organization_id="org-test",
        doc_id=hashlib.sha256(ORIGINAL).hexdigest(),
        filename="manual.pdf",
        mime="application/pdf",
        size_bytes=len(ORIGINAL),
        object_key="sources/frozen/source.pdf",
    )
    session.add(document)
    await session.flush()
    old = ParseJob(
        id=new_id(),
        document_id=document.id,
        engine="borndigital",
        status="succeeded",
        options_hash="old",
        document_version=1,
    )
    newer = ParseJob(
        id=new_id(),
        document_id=document.id,
        engine="borndigital",
        status="succeeded",
        options_hash="new",
        document_version=2,
    )
    session.add_all([old, newer])
    resource = Resource(
        id=new_id(),
        owner_id="actor-alice",
        uploaded_by="actor-alice",
        organization_id="org-test",
        display_name="manual.pdf",
    )
    session.add(resource)
    await session.flush()
    document.current_job_id = newer.id
    version = ResourceVersion(
        id=new_id(),
        resource_id=resource.id,
        document_id=document.id,
        source_digest=document.doc_id,
        filename="manual.pdf",
        size_bytes=len(ORIGINAL),
        parse_job_id=old.id,
    )
    session.add(version)
    evidence = Evidence(
        id=new_id(),
        document_id=document.id,
        parse_job_id=old.id,
        seq=0,
        atom_key="source:0",
        content="old original",
        page_idx=0,
        bbox=[10, 20, 200, 40],
        page_size=[612, 792],
    )
    session.add(evidence)
    await session.commit()
    await app_state.storage.put(document.object_key, ORIGINAL, "application/pdf")
    layout = {
        "layout_version": "ddp-layout/1",
        "pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": []}],
    }
    await app_state.storage.put(
        f"results/{old.id}/layout.json", json_bytes(layout), "application/json"
    )
    endpoint = f"/api/resources/{resource.id}/versions/{version.id}/bundle"
    response = await actor_client.get(endpoint)
    assert response.status_code == 200, response.text
    snapshot = read_bundle(io.BytesIO(response.content))
    assert snapshot.source["parse_revision"] == old.id
    assert snapshot.evidence[0]["evidence"]["evidence_id"] == evidence.id
    assert snapshot.evidence[0]["excerpt"] == "old original"
    version.parse_job_id = None
    await session.commit()
    response = await actor_client.get(endpoint)
    assert response.status_code == 200, response.text
    snapshot = read_bundle(io.BytesIO(response.content))
    assert snapshot.source["parse_revision"] is None
    assert snapshot.layout["reason"] == "parse_revision_unbound"
    assert snapshot.evidence == []


async def test_last_deleted_bundle_reference_is_collected_after_grace(
    actor_client, session, app_state
):
    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    version = await session.get(ResourceVersion, result["source_version_id"])
    resource.deleted_at = version.deleted_at = utcnow() - timedelta(
        seconds=settings.gc_grace_seconds + 10
    )
    await session.commit()
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 1
    assert app_state.storage.objects == {}


@pytest.mark.parametrize(
    "kind,payload_key",
    [("index", "document_id"), ("extract", "document_ids"), ("compile", "resource_version_id")],
)
async def test_t03_running_task_retains_deleted_bundle_until_terminal(
    actor_client, session, app_state, kind, payload_key
):
    from ddp_corpus.models import Task

    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    version = await session.get(ResourceVersion, result["source_version_id"])
    resource.deleted_at = version.deleted_at = utcnow() - timedelta(
        seconds=settings.gc_grace_seconds + 10
    )
    identity = version.id if payload_key == "resource_version_id" else result["document_id"]
    task = Task(
        kind=kind,
        status="running",
        payload={payload_key: [identity] if payload_key == "document_ids" else identity},
    )
    session.add(task)
    await session.commit()
    before = dict(app_state.storage.objects)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert app_state.storage.objects == before
    task.status = "succeeded"
    await session.commit()
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 1
    assert not app_state.storage.objects


async def test_t03_durable_citation_protects_unique_original(actor_client, session, app_state):
    from ddp_corpus.models import Citation, Evidence, ParseJob, new_id

    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    version = await session.get(ResourceVersion, result["source_version_id"])
    job = ParseJob(
        id=new_id(),
        document_id=version.document_id,
        engine="borndigital",
        status="succeeded",
        options_hash="local-parse",
    )
    session.add(job)
    await session.flush()
    evidence = Evidence(
        id=new_id(),
        document_id=version.document_id,
        parse_job_id=job.id,
        atom_key="source:0",
        content="retained source",
    )
    session.add(evidence)
    await session.flush()
    session.add(Citation(evidence_id=evidence.id, source_kind="message", source_id="saved-answer"))
    resource.deleted_at = version.deleted_at = utcnow() - timedelta(
        seconds=settings.gc_grace_seconds + 10
    )
    await session.commit()
    before = dict(app_state.storage.objects)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert app_state.storage.objects == before


async def test_gc_partial_failure_keeps_durable_remaining_keys_and_retries(
    actor_client, session, app_state, monkeypatch
):
    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    version = await session.get(ResourceVersion, result["source_version_id"])
    resource.deleted_at = version.deleted_at = utcnow() - timedelta(
        seconds=settings.gc_grace_seconds + 10
    )
    await session.commit()
    original_delete = app_state.storage.delete
    attempts = 0

    async def fail_once(key):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("interrupted storage deletion")
        await original_delete(key)

    monkeypatch.setattr(app_state.storage, "delete", fail_once)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    document = await session.get(Document, result["document_id"], populate_existing=True)
    assert document.object_key == ""
    assert document.gc_error == "delete_failed:OSError"
    assert document.gc_pending_keys
    assert set(document.gc_pending_keys) == set(app_state.storage.objects)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 1
    await session.refresh(document)
    assert document.gc_pending_keys == [] and document.gc_error is None
    assert not app_state.storage.objects


@pytest.mark.parametrize("reference_kind", ["dependency", "claim_binding"])
async def test_t03_wiki_revision_references_protect_unique_source_without_old_citation(
    actor_client, session, app_state, reference_kind
):
    from ddp_corpus.models import (
        ClaimEvidenceBinding,
        DependencyManifest,
        Evidence,
        ParseJob,
        Wiki,
        WikiRevision,
        new_id,
    )

    result = await import_one(actor_client)
    resource = await session.get(Resource, result["resource_id"])
    version = await session.get(ResourceVersion, result["source_version_id"])
    job = ParseJob(
        id=new_id(),
        document_id=version.document_id,
        engine="borndigital",
        status="succeeded",
        options_hash="local-parse",
    )
    wiki = Wiki(id=new_id(), owner_id="actor-alice", organization_id="org-test", title="Saved Wiki")
    session.add_all([job, wiki])
    await session.flush()
    evidence = Evidence(
        id=new_id(),
        document_id=version.document_id,
        parse_job_id=job.id,
        atom_key="source:0",
        content="saved original",
        content_digest="a" * 64,
    )
    revision = WikiRevision(
        id=new_id(), wiki_id=wiki.id, title="Saved Wiki", created_by="actor-alice"
    )
    session.add_all([evidence, revision])
    await session.flush()
    if reference_kind == "dependency":
        session.add(
            DependencyManifest(
                revision_id=revision.id,
                page_key="overview",
                resource_id=resource.id,
                source_version_id=version.id,
                document_id=version.document_id,
                source_digest=version.source_digest,
                parse_revision=job.id,
                evidence_id=evidence.id,
                excerpt_digest=evidence.content_digest,
            )
        )
    else:
        session.add(
            ClaimEvidenceBinding(
                revision_id=revision.id,
                page_key="overview",
                claim_id="claim-one",
                evidence_id=evidence.id,
                excerpt_digest=evidence.content_digest,
            )
        )
    resource.deleted_at = version.deleted_at = utcnow() - timedelta(
        seconds=settings.gc_grace_seconds + 10
    )
    await session.commit()
    before = dict(app_state.storage.objects)
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert app_state.storage.objects == before
