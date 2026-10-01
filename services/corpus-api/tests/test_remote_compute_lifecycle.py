"""Consumer-visible delivery identity, retention, and late-completion boundaries."""
import hashlib
import io
from datetime import timedelta

import pytest
from sqlalchemy import select

from ddp_core.bundle import json_bytes, read_bundle
from ddp_corpus.config import settings
from ddp_corpus.models import Chunk, Document, Evidence, ParseJob, Resource, ResourceVersion, new_id, utcnow
from ddp_corpus.remote_compute_ingest import record_parse_outcome
from ddp_corpus.remote_compute_models import RemoteCompute
from ddp_corpus.routers.internal import _record_remote_outcome
from ddp_corpus.routers.remote_compute import cleanup_compute, sweep_remote_computes
from ddp_corpus.storage import job_result_prefix
from tests.conftest import ACTOR, ORG
from tests.test_remote_compute import _bind, _create


async def _parsed(actor_client, session, app_state, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", "node-center")
    original = b"%PDF-1.4\nfrozen delivery original\n%%EOF"
    record, _ = await _create(actor_client, session, original, key="lifecycle-input")
    row, job = await _bind(session, app_state, record, original)
    job.status = "succeeded"
    job.index_status = "ready"
    job.compile_status = "ready"
    job.result_prefix = job_result_prefix(job.id)
    version = await session.scalar(select(ResourceVersion).where(ResourceVersion.resource_id == job.resource_id))
    version.parse_job_id = job.id
    evidence = Evidence(id=new_id(), document_id=job.document_id, parse_job_id=job.id,
                        seq=0, atom_key="source:0", content="Launch code 8712.",
                        page_idx=0, bbox=[10, 20, 200, 40], page_size=[612, 792])
    session.add(evidence)
    await session.commit()
    layout = {"layout_version": "ddp-layout/1", "pdf_info": [{"page_idx": 0,
              "page_size": [612, 792], "para_blocks": [{"type": "text",
              "bbox": [10, 20, 200, 40], "text": "Launch code 8712."}]}]}
    await app_state.storage.put(job.result_prefix + "layout.json", json_bytes(layout), "application/json")
    return row, job, version, evidence, original


async def test_actual_parse_delivery_uses_frozen_source_and_evidence(actor_client, session, app_state, monkeypatch):
    row, job, version, evidence, original = await _parsed(actor_client, session, app_state, monkeypatch)
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 200, response.text
    bundle = read_bundle(io.BytesIO(response.content))
    assert bundle.source["origin_node_id"] == "node-center"
    assert bundle.source["source_version_id"] == version.id
    assert bundle.source["parse_revision"] == job.id
    assert bundle.source["source_digest"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert bundle.files["source.bin"] == original
    assert bundle.evidence[0]["evidence"]["evidence_id"] == evidence.id
    assert bundle.evidence[0]["excerpt"] == "Launch code 8712."
    assert bundle.evidence[0]["evidence"]["locator"]["bbox"] == [10, 20, 200, 40]
    assert row.manifest_json["output_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert row.manifest_json["degraded"] == []


async def test_failed_index_and_missing_evidence_still_deliver_with_visible_gaps(actor_client, session, app_state, monkeypatch):
    row, job, _, evidence, original = await _parsed(actor_client, session, app_state, monkeypatch)
    await session.delete(evidence)
    job.index_status = "failed"
    job.compile_degraded = ["code_detection_unavailable"]
    await session.commit()
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    assert row.status == "succeeded"
    assert row.manifest_json["degraded"] == ["resource_index_unavailable", "evidence_unavailable"]
    assert row.manifest_json["compile_degraded"] == ["code_detection_unavailable"]
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 200, response.text
    bundle = read_bundle(io.BytesIO(response.content))
    assert bundle.files["source.bin"] == original
    assert bundle.evidence == []


async def test_delivery_waits_for_pending_evidence_compilation(actor_client, session, app_state, monkeypatch):
    row, job, _, _, _ = await _parsed(actor_client, session, app_state, monkeypatch)
    job.index_status = "pending"
    job.compile_status = "pending"
    await session.commit()
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    assert row.status == "running"
    assert "output_sha256" not in row.manifest_json


@pytest.mark.parametrize("terminal", ["cancelled", "expired", "failed", "acked"])
async def test_late_parse_outcome_cannot_resurrect_terminal_compute(actor_client, session, app_state, terminal):
    record, _ = await _create(actor_client, session, key="late-outcome")
    row = await session.get(RemoteCompute, record["id"])
    row.status = terminal
    row.manifest_json = {"error": terminal}
    await session.commit()
    await record_parse_outcome(session, compute_id=row.id, organization_id=ORG,
                               parse_job_id=row.parse_job_id, ok=True,
                               bundle_key="late/output.zip", output_sha256="a" * 64)
    await session.refresh(row)
    assert row.status == terminal
    assert row.manifest_json == {"error": terminal}


async def _fixed_output(actor_client, session, app_state):
    record, _ = await _create(actor_client, session, key="fixed-output")
    row = await session.get(RemoteCompute, record["id"])
    payload = b"fixed immutable result bytes"
    key = f"tmp-remote-compute/{ORG}/{row.id}/output.zip"
    await app_state.storage.put(key, payload, "application/zip")
    row.status = "succeeded"
    row.manifest_json = {"bundle_key": key, "output_sha256": hashlib.sha256(payload).hexdigest()}
    await session.commit()
    return row, key, payload


async def test_unacknowledged_output_remains_available_until_expiry(actor_client, session, app_state):
    row, key, payload = await _fixed_output(actor_client, session, app_state)
    row.updated_at = utcnow() - timedelta(hours=2)
    row.expires_at = utcnow() + timedelta(hours=20)
    await session.commit()
    await sweep_remote_computes(session, app_state.storage)
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert await app_state.storage.get(key) == payload


async def test_successful_unacknowledged_compute_can_be_cancelled(actor_client, session, app_state):
    row, _, _ = await _fixed_output(actor_client, session, app_state)
    cancelled = await actor_client.post(f"/api/v1/remote-compute/{row.id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    download = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert download.status_code == 404


@pytest.mark.parametrize("operation", ["status", "bundle", "ack"])
async def test_output_expiry_is_visible_without_background_sweep(actor_client, session, app_state, operation):
    row, _, _ = await _fixed_output(actor_client, session, app_state)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    path = f"/api/v1/remote-compute/{row.id}"
    if operation == "ack":
        response = await actor_client.post(path + "/ack", json={"output_sha256": row.manifest_json["output_sha256"]})
        assert response.status_code == 409
    elif operation == "bundle":
        response = await actor_client.get(path + "/bundle")
        assert response.status_code == 404
    else:
        response = await actor_client.get(path)
        assert response.json()["status"] == "expired"
    await session.refresh(row)
    assert row.status == "expired"


async def test_terminal_cleanup_tombstones_only_owned_resource_and_preserves_shared_original(actor_client, session, app_state, monkeypatch):
    row, job, version, _, original = await _parsed(actor_client, session, app_state, monkeypatch)
    document = await session.get(Document, job.document_id)
    permanent = Resource(id=new_id(), owner_id=ACTOR, uploaded_by=ACTOR,
                         organization_id=ORG, display_name="separate permanent resource")
    session.add(permanent)
    await session.flush()
    session.add(ResourceVersion(id=new_id(), resource_id=permanent.id, document_id=document.id,
                                source_digest=version.source_digest, filename="permanent.pdf",
                                size_bytes=len(original), parse_job_id=job.id))
    row.status = "cancelled"
    row.updated_at = utcnow() - timedelta(hours=2)
    await session.commit()
    await cleanup_compute(session, app_state.storage, row)
    await session.refresh(permanent)
    own = await session.get(Resource, job.resource_id, populate_existing=True)
    assert own.deleted_at is not None
    assert permanent.deleted_at is None
    assert await app_state.storage.get(document.object_key) == original


async def test_lost_callback_is_reconciled_from_completed_parse_without_reparse(actor_client, session, app_state, monkeypatch):
    from ddp_corpus import db
    from ddp_corpus.reconcile import reconcile_once
    from ddp_corpus.service_client import ServiceClient

    row, job, version, _, _ = await _parsed(actor_client, session, app_state, monkeypatch)
    jobs_before = len((await session.execute(select(ParseJob))).scalars().all())
    await reconcile_once(db.get_sessionmaker(), app_state.storage,
                         ServiceClient(app_state.http), app_state.http)
    await session.refresh(row)
    assert row.status == "succeeded"
    # Recovered from the durable binding: no second parse job was created.
    assert len((await session.execute(select(ParseJob))).scalars().all()) == jobs_before
    assert row.parse_job_id == job.id
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 200, response.text
    assert read_bundle(io.BytesIO(response.content)).source["source_version_id"] == version.id


async def test_reference_safe_gc_reclaims_closed_compute_original_and_derived_bytes(actor_client, session, app_state, monkeypatch):
    from ddp_corpus import db
    from ddp_corpus.gc import collect_deleted_objects

    row, job, _, _, original = await _parsed(actor_client, session, app_state, monkeypatch)
    document = await session.get(Document, job.document_id)
    chunk = Chunk(document_id=document.id, parse_job_id=job.id, seq=0,
                  text="Launch code 8712.", search_text="Launch code 8712.",
                  text_tokenized="launch code 8712", derived_text="derived secret",
                  embedding=[0.5] * 1024)
    session.add(chunk)
    original_key, layout_key = document.object_key, job.result_prefix + "layout.json"
    row.status = "cancelled"
    row.updated_at = utcnow() - timedelta(hours=2)
    await session.commit()
    await cleanup_compute(session, app_state.storage, row)
    # A durable citation must outlive the remote task's own retention.
    from ddp_corpus.models import Citation
    evidence = await session.scalar(select(Evidence).where(Evidence.parse_job_id == job.id))
    citation = Citation(evidence_id=evidence.id, source_kind="message", source_id="retained-answer")
    session.add(citation)
    await session.commit()
    monkeypatch.setattr(settings, "gc_grace_seconds", 0)
    await collect_deleted_objects(db.get_sessionmaker(), app_state.storage)
    assert await app_state.storage.get(original_key) == original
    assert await app_state.storage.exists(layout_key)
    assert await session.scalar(select(Chunk.id).where(Chunk.document_id == document.id)) == chunk.id
    await session.delete(citation)
    await session.commit()
    await collect_deleted_objects(db.get_sessionmaker(), app_state.storage)
    assert not await app_state.storage.exists(original_key)
    assert not await app_state.storage.exists(layout_key)
    assert await session.scalar(select(Chunk.id).where(Chunk.document_id == document.id)) is None
    assert await session.get(Evidence, evidence.id, populate_existing=True) is not None


async def test_gc_purges_historical_reclaimed_chunks_without_touching_live_document(
    session, app_state, monkeypatch
):
    from ddp_corpus import db
    from ddp_corpus.gc import collect_deleted_objects

    reclaimed = [
        Document(id=new_id(), doc_id=new_id(), filename="reclaimed.pdf", uploaded_by=ACTOR,
                 organization_id=ORG, object_key="", deleted_at=utcnow() - timedelta(hours=2))
        for _ in range(2)
    ]
    live = Document(id=new_id(), doc_id=new_id(), filename="live.pdf", uploaded_by=ACTOR,
                    organization_id=ORG, object_key="live.pdf")
    session.add_all([*reclaimed, live])
    await session.flush()
    for document in [*reclaimed, live]:
        job = ParseJob(id=new_id(), document_id=document.id, engine="borndigital",
                       status="succeeded", options_hash="historical")
        session.add(job)
        await session.flush()
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, text="retained secret", seq=0))
    await session.commit()
    await app_state.storage.put(live.object_key, b"live original", "application/pdf")
    monkeypatch.setattr(settings, "gc_grace_seconds", 0)

    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage, limit=1) == 1
    remaining = set((await session.execute(select(Chunk.document_id))).scalars())
    assert live.id in remaining
    assert len(remaining.intersection(document.id for document in reclaimed)) == 1
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage, limit=1) == 1
    assert set((await session.execute(select(Chunk.document_id))).scalars()) == {live.id}
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage, limit=1) == 0
    assert await app_state.storage.get(live.object_key) == b"live original"


@pytest.mark.parametrize("outcome", ["failed", "expired"])
async def test_closed_callback_revokes_owned_resource_before_cleanup_grace(actor_client, session, app_state, monkeypatch, outcome):
    row, job, _, _, original = await _parsed(actor_client, session, app_state, monkeypatch)
    if outcome == "expired":
        row.expires_at = utcnow() - timedelta(seconds=1)
    else:
        job.status = "failed"
    await session.commit()
    await _record_remote_outcome(session, app_state.storage, job,
                                 ok=outcome != "failed", error="parse_failed")
    await session.refresh(row)
    resource = await session.get(Resource, job.resource_id, populate_existing=True)
    assert row.status == outcome
    assert resource.deleted_at is not None
    assert await app_state.storage.get(row.input_object_key) == original


async def _delivered_then_cancelled(actor_client, session, app_state, monkeypatch, *, parse_status):
    row, job, _, _, _ = await _parsed(actor_client, session, app_state, monkeypatch)
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    fixed = dict(row.manifest_json)
    cancelled = await actor_client.post(f"/api/v1/remote-compute/{row.id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    await session.refresh(row)
    row.updated_at = utcnow() - timedelta(hours=2)
    job.status = parse_status
    await session.commit()
    return row, fixed


async def test_closed_delivery_artifact_is_reclaimed_after_grace_without_touching_manifest(actor_client, session, app_state, monkeypatch):
    row, fixed = await _delivered_then_cancelled(actor_client, session, app_state, monkeypatch,
                                                 parse_status="succeeded")
    assert await app_state.storage.exists(fixed["bundle_key"])
    removed = await cleanup_compute(session, app_state.storage, row)
    await session.refresh(row)
    assert fixed["bundle_key"] in removed
    assert not await app_state.storage.exists(fixed["bundle_key"])
    assert row.cleaned_at is not None
    assert row.manifest_json == fixed
    # The Document-owned original waits for reference-safe GC, not this sweep.
    assert await app_state.storage.exists(row.input_object_key)
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 404


async def test_reclamation_waits_while_the_bound_parse_is_still_running(actor_client, session, app_state, monkeypatch):
    row, fixed = await _delivered_then_cancelled(actor_client, session, app_state, monkeypatch,
                                                 parse_status="running")
    assert await cleanup_compute(session, app_state.storage, row) == []
    await session.refresh(row)
    assert row.cleaned_at is None
    assert await app_state.storage.exists(fixed["bundle_key"])


@pytest.mark.parametrize("drift", ["deleted", "digest", "foreign_owner"])
async def test_drifted_fixed_source_fails_instead_of_delivering(actor_client, session, app_state, monkeypatch, drift):
    row, job, version, _, _ = await _parsed(actor_client, session, app_state, monkeypatch)
    if drift == "deleted":
        version.deleted_at = utcnow()
    elif drift == "digest":
        version.source_digest = "0" * 64
    else:
        resource = await session.get(Resource, job.resource_id)
        resource.owner_id = "actor-mallory"
    await session.commit()
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    assert row.status == "failed"
    assert row.manifest_json["error"] == "fixed_source_unavailable"
    assert "output_sha256" not in row.manifest_json
    response = await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")
    assert response.status_code == 404


async def test_second_publisher_cannot_replace_delivered_bytes(actor_client, session, app_state, monkeypatch):
    row, job, _, evidence, _ = await _parsed(actor_client, session, app_state, monkeypatch)
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    fixed = dict(row.manifest_json)
    first = (await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")).content
    # Change what a republish would snapshot, so an overwrite would produce new bytes.
    evidence.content = "Launch code 0000."
    await session.commit()
    # A late duplicate callback or reconciler pass after success is a no-op.
    await _record_remote_outcome(session, app_state.storage, job, ok=True)
    await session.refresh(row)
    assert row.manifest_json == fixed
    assert (await actor_client.get(f"/api/v1/remote-compute/{row.id}/bundle")).content == first
    prefix = f"tmp-remote-compute/{ORG}/{row.id}/"
    outputs = [key for key in await app_state.storage.list_prefix(prefix) if "/output-" in key]
    assert outputs == [fixed["bundle_key"]]
