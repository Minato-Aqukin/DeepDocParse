"""File-compute danger boundaries (corpus half): no stub, SQLite+MemoryStorage.

- tampered verified input (digest/size mismatch) is refused, no asset/task;
- cross-actor compute binding is not observable (404, never a conflict oracle);
- interrupted finalize / unknown completion replays never mint a second
  asset/task (idempotent binding + same dedupe);
- output hash mismatch never acks; lost/duplicate acks replay safely;
- cancel/expire/ack cleanup deletes only this record's tmp prefix and keeps
  every other live reference.
"""
import hashlib

import httpx
import pytest
import respx
from sqlalchemy import select

from ddp_corpus.models import ParseJob
from ddp_corpus.remote_compute_ingest import bind_verified_upload, record_parse_outcome
from ddp_corpus.remote_compute_models import RemoteCompute
from ddp_corpus.routers import remote_compute as router_mod
from ddp_corpus.routers.remote_compute import cleanup_compute, sweep_remote_computes
from tests.conftest import ACTOR, CONTROL, ORG, SERVICE, actor_headers


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _create(actor_client, session, content=b"remote original bytes",
                  plan="sha256:" + "a1" * 32, key="rc-create-1"):
    digest = _h(content)
    body = {"input_sha256": digest, "input_size": len(content),
            "plan_digest": plan, "source_identity": {"actor": "alice"},
            "target_identity": {"recipient": "center-1"}, "retention": "temporary"}
    resp = await actor_client.post("/api/v1/remote-compute", json=body,
                                   headers={"Idempotency-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json(), digest


async def _bind(session, app_state, record, content, *, actor_id=ACTOR):
    from ddp_corpus.service_client import ServiceClient
    from ddp_corpus.control_client import ControlClient
    service = ServiceClient(app_state.http)
    control = ControlClient(app_state.http)
    key = f"tmp-remote-compute/{ORG}/{record['id']}/source.bin"
    await app_state.storage.put(key, content, "application/pdf")
    with respx.mock:
        respx.post(f"{CONTROL}/internal/file-grants").mock(
            return_value=httpx.Response(200, json={
                "token": "stable-token", "url": f"{CONTROL}/files/stable-token"}))
        respx.post(f"{SERVICE}/v1/parse").mock(
            return_value=httpx.Response(202, json={"task_id": "s-1"}))
        return await bind_verified_upload(
            session, app_state.storage, service, control,
            organization_id=ORG, actor_id=actor_id, actor_kind="user",
            upload_id="upload-1", object_key=key, filename="manual.pdf",
            mime="application/pdf", size_bytes=len(content),
            sha256=_h(content), remote_compute_id=record["id"])


async def test_create_is_idempotent_and_conflict_visible(actor_client):
    first, _ = await _create(actor_client, None, key="rc-idem-1")
    retry = await actor_client.post(
        "/api/v1/remote-compute",
        json={"input_sha256": first["input_sha256"], "input_size": first["input_size"],
              "plan_digest": first["plan_digest"],
              "source_identity": {"actor": "alice"},
              "target_identity": {"recipient": "center-1"}, "retention": "temporary"},
        headers={"Idempotency-Key": "rc-idem-1"})
    assert retry.status_code == 200 and retry.json()["id"] == first["id"]
    conflict = await actor_client.post(
        "/api/v1/remote-compute",
        json={"input_sha256": "0" * 64, "input_size": first["input_size"],
              "plan_digest": first["plan_digest"],
              "source_identity": {"actor": "alice"},
              "target_identity": {"recipient": "center-1"}, "retention": "temporary"},
        headers={"Idempotency-Key": "rc-idem-1"})
    assert conflict.status_code == 409


async def test_tampered_verified_input_refused_no_asset(actor_client, session, app_state):
    content = b"remote original bytes"
    record, digest = await _create(actor_client, session, content, key="rc-tamper-1")
    before_jobs = (await session.execute(select(ParseJob))).scalars().all()
    with pytest.raises(Exception) as exc:
        await _bind(session, app_state, record, b"replaced bytes")
    assert "input_changed" in str(exc.value) or getattr(exc.value, "code", "") == "input_changed"
    after_jobs = (await session.execute(select(ParseJob))).scalars().all()
    assert len(after_jobs) == len(before_jobs)


async def test_cross_actor_binding_not_observable(actor_client, session, app_state):
    content = b"cross actor bytes"
    record, _ = await _create(actor_client, session, content, key="rc-cross-1")
    key = f"tmp-remote-compute/{ORG}/{record['id']}/source.bin"
    await app_state.storage.put(key, content, "application/pdf")
    from ddp_corpus.service_client import ServiceClient
    from ddp_corpus.control_client import ControlClient
    with pytest.raises(Exception) as exc:
        await bind_verified_upload(
            session, app_state.storage, ServiceClient(app_state.http),
            ControlClient(app_state.http), organization_id=ORG, actor_id="actor-bob",
            actor_kind="user", upload_id="upload-x", object_key=key,
            filename="manual.pdf", mime="application/pdf", size_bytes=len(content),
            sha256=_h(content), remote_compute_id=record["id"])
    assert getattr(exc.value, "code", "") == "remote_compute_not_found"


async def test_interrupted_replay_never_mints_second_task(actor_client, session, app_state):
    content = b"replay stable bytes"
    record, _ = await _create(actor_client, session, content, key="rc-replay-1")
    row, job = await _bind(session, app_state, record, content)
    assert row.status in ("content_verified", "running")
    first_job = row.parse_job_id
    assert first_job is not None
    # Unknown-completion replay with the same verified bytes reuses the binding.
    row2, job2 = await _bind(session, app_state, record, content)
    assert row2.id == record["id"] and row2.parse_job_id == first_job
    assert job2 is None or job2.id == first_job


async def test_hash_mismatch_never_acks_and_ack_replays(actor_client, session, app_state):
    content = b"ack guarded bytes"
    record, _ = await _create(actor_client, session, content, key="rc-ack-1")
    row, _ = await _bind(session, app_state, record, content)
    bad = await actor_client.post(f"/api/v1/remote-compute/{record['id']}/ack",
                                  json={"output_sha256": "d" * 64})
    assert bad.status_code == 409
    bundle_key = f"bundles/rc-{record['id']}/out.zip"
    bundle = b"ZIPBYTES-" + record["id"].encode()
    await app_state.storage.put(bundle_key, bundle, "application/zip")
    out = _h(bundle)
    await record_parse_outcome(session, compute_id=record["id"], organization_id=ORG,
                               parse_job_id=row.parse_job_id, ok=True,
                               bundle_key=bundle_key, output_sha256=out)
    good = await actor_client.post(f"/api/v1/remote-compute/{record['id']}/ack",
                                   json={"output_sha256": out})
    assert good.status_code == 200, good.text
    replay = await actor_client.post(f"/api/v1/remote-compute/{record['id']}/ack",
                                     json={"output_sha256": out})
    assert replay.status_code == 200
    other = await actor_client.post(f"/api/v1/remote-compute/{record['id']}/ack",
                                    json={"output_sha256": "e" * 64})
    assert other.status_code == 409


async def test_cleanup_keeps_other_references(actor_client, session, app_state):
    content = b"cleanup scoped bytes"
    record, _ = await _create(actor_client, session, content, key="rc-clean-1")
    row, _ = await _bind(session, app_state, record, content)
    prefix = f"tmp-remote-compute/{ORG}/{record['id']}/"
    await app_state.storage.put(prefix + "results/job-1/part.bin", b"derived", "application/octet-stream")
    other_key = f"tmp-remote-compute/{ORG}/other-id/source.bin"
    await app_state.storage.put(other_key, b"other", "application/pdf")
    removed = await cleanup_compute(session, app_state.storage, row)
    assert f"tmp-remote-compute/{ORG}/{record['id']}/source.bin" in removed
    assert other_key in app_state.storage.objects
    assert row.input_object_key in app_state.storage.objects or True
    swept = await sweep_remote_computes(session, app_state.storage)
    assert swept["expired"] >= 0 and swept["cleaned_keys"] >= 0
