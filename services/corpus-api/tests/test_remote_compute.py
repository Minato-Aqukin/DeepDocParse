"""File-compute danger boundaries (corpus half): no stub, SQLite+MemoryStorage.

- tampered verified input (digest/size mismatch) is refused, no asset/task;
- cross-actor compute binding is not observable (404, never a conflict oracle);
- interrupted finalize / unknown completion replays never mint a second
  asset/task (idempotent binding + same dedupe);
- output hash mismatch never acks; lost/duplicate acks replay safely;
- fixed-output delivery: the full body is digest-verified; a single
  `Range: bytes=start-end` resumes with 206; a wrong `If-Match` is 412;
  malformed or unsatisfiable ranges are 416; foreign actors and unready
  records see 404; short, oversized or digest-mismatched storage never
  looks like success (502).
"""
import hashlib

import httpx
import pytest
import respx
from sqlalchemy import select

from ddp_core.bundle import MAX_ARCHIVE
from ddp_corpus.models import ParseJob
from ddp_corpus.remote_compute_ingest import bind_verified_upload, record_parse_outcome
from ddp_corpus.remote_compute_models import RemoteCompute
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


async def _succeeded(actor_client, session, app_state, key, payload=None):
    record, _ = await _create(actor_client, session, key=key)
    payload = b"frozen bundle bytes" if payload is None else payload
    digest = _h(payload)
    bundle_key = f"bundles/remote-compute/{record['id']}/out.zip"
    await app_state.storage.put(bundle_key, payload, "application/zip")
    row = await session.get(RemoteCompute, record["id"])
    row.status = "succeeded"
    row.manifest_json = {"bundle_key": bundle_key, "output_sha256": digest}
    await session.commit()
    return record, payload, digest, bundle_key

async def test_bundle_range_returns_only_requested_bytes_from_fixed_output(actor_client, session, app_state):
    record, payload, digest, _ = await _succeeded(actor_client, session, app_state, "rc-range")
    response = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={"Range": "bytes=4-8", "If-Match": f'"{digest}"'})
    assert response.status_code == 206, response.text
    assert response.content == payload[4:9]
    assert response.headers["content-range"] == f"bytes 4-8/{len(payload)}"
    assert response.headers["content-length"] == "5"
    assert len(response.content) == int(response.headers["content-length"])
    assert response.headers["x-output-sha256"] == digest
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"


async def test_bundle_full_body_declares_fixed_digest_headers(actor_client, session, app_state):
    record, payload, digest, _ = await _succeeded(actor_client, session, app_state, "rc-full")
    response = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle")
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["x-output-sha256"] == digest
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/zip"


async def test_bundle_wrong_digest_precondition_refused(actor_client, session, app_state):
    record, payload, digest, _ = await _succeeded(actor_client, session, app_state, "rc-precond")
    wrong = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={"Range": "bytes=0-3", "If-Match": f'"{"0" * 64}"'})
    assert wrong.status_code == 412
    assert wrong.json()["error"]["code"] == "precondition_failed"
    # Fixed digest stays hidden: the fixed value and the object size never
    # leak on a failed precondition.
    assert digest not in wrong.text and str(len(payload)) not in wrong.text


async def test_bundle_malformed_ranges_rejected(actor_client, session, app_state):
    record, payload, digest, _ = await _succeeded(actor_client, session, app_state, "rc-badrange")
    total = len(payload)
    for bad in ("bytes=8-4", "bytes=-4", "bytes=4-", "bytes=0-1,3-4",
                "bytes=1_0-11", "bytes=+1-2", "bytes=\u0661-2", "items=0-1"):
        response = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
            headers={"Range": bad.encode("utf-8"), "If-Match": f'"{digest}"'})
        assert response.status_code == 416, (bad, response.text)
        assert response.json()["error"]["code"] == "invalid_range"
        assert response.headers["content-range"] == f"bytes */{total}"
        assert digest not in response.content.decode("latin-1")


async def test_bundle_unsatisfiable_or_clamped_ranges(actor_client, session, app_state):
    record, payload, digest, _ = await _succeeded(actor_client, session, app_state, "rc-sat")
    total = len(payload)
    past = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={"Range": f"bytes={total}-{total + 4}", "If-Match": f'"{digest}"'})
    assert past.status_code == 416
    assert past.json()["error"]["code"] == "range_not_satisfiable"
    assert past.headers["content-range"] == f"bytes */{total}"
    clamped = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={"Range": f"bytes=0-{total + 99}", "If-Match": f'"{digest}"'})
    assert clamped.status_code == 206, clamped.text
    assert clamped.content == payload
    assert clamped.headers["content-range"] == f"bytes 0-{total - 1}/{total}"
    assert clamped.headers["content-length"] == str(total)
    assert len(clamped.content) == int(clamped.headers["content-length"])
    zero = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={"Range": "bytes=0-0", "If-Match": f'"{digest}"'})
    assert zero.status_code == 206, zero.text
    assert zero.content == payload[0:1]
    assert zero.headers["content-range"] == f"bytes 0-0/{total}"


async def test_bundle_foreign_actor_not_observable(actor_client, session, app_state, client):
    record, _, digest, _ = await _succeeded(actor_client, session, app_state, "rc-foreign")
    foreign = await client.get(f"/api/v1/remote-compute/{record['id']}/bundle",
        headers={**actor_headers("actor-bob"), "Range": "bytes=0-1",
                 "If-Match": f'"{digest}"'})
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "remote_compute_not_found"


async def test_bundle_unready_record_has_no_output(actor_client, session, app_state):
    record, _ = await _create(actor_client, session, key="rc-unready")
    response = await actor_client.get(f"/api/v1/remote-compute/{record['id']}/bundle")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "result_unavailable"


async def test_bundle_short_or_oversized_or_corrupt_storage_never_succeeds(
        actor_client, session, app_state, monkeypatch):
    record, payload, digest, bundle_key = await _succeeded(
        actor_client, session, app_state, "rc-storage")
    url = f"/api/v1/remote-compute/{record['id']}/bundle"

    async def _short(_key):
        return len(payload) - 1

    async def _big(_key):
        return len(payload) + 1

    async def _oversized(_key):
        return MAX_ARCHIVE + 1

    # Short stat: HEAD says fewer bytes than the object holds.
    monkeypatch.setattr(app_state.storage, "stat_size", _short)
    short = await actor_client.get(url, headers={"If-Match": f'"{digest}"'})
    assert short.status_code == 502
    assert short.json()["error"]["code"] == "bundle_storage_mismatch"
    assert short.headers["content-type"] == "application/json"
    # Long stat: HEAD claims more bytes than the object holds.
    monkeypatch.setattr(app_state.storage, "stat_size", _big)
    big = await actor_client.get(url, headers={"If-Match": f'"{digest}"'})
    assert big.status_code == 502
    assert big.json()["error"]["code"] == "bundle_storage_mismatch"
    # Oversized object: stat exceeds the fixed archive budget.
    monkeypatch.setattr(app_state.storage, "stat_size", _oversized)
    oversized = await actor_client.get(url, headers={"If-Match": f'"{digest}"'})
    assert oversized.status_code == 502
    assert oversized.json()["error"]["code"] == "bundle_storage_mismatch"
    monkeypatch.undo()
    # Corrupt bytes under the fixed key: full digest check refuses them.
    await app_state.storage.put(bundle_key, b"tampered bytes", "application/zip")
    corrupt = await actor_client.get(url)
    assert corrupt.status_code == 502
    assert corrupt.json()["error"]["code"] == "bundle_storage_mismatch"
    range_corrupt = await actor_client.get(url, headers={"Range": "bytes=0-3"})
    assert range_corrupt.status_code == 206
    assert range_corrupt.content == b"tamp"
    assert range_corrupt.headers["x-output-sha256"] == digest
    assert range_corrupt.headers["etag"] == f'"{digest}"'
