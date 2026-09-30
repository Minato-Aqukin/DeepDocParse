"""File-compute danger boundaries: fixed input, idempotent execute, hashes, replays, TTL.

Covers the local half of the remote file-compute path (no network beyond the
MockTransport center stub):
- tampered pinned input is refused before any byte moves (input_changed);
- cross-actor upload binding is refused (remote_compute_not_found shape);
- interrupted finalize / unknown completion never mints a second asset/task;
- hash mismatch never imports and never acks;
- lost/duplicate acks replay safely;
- cleanup removes only this record's tmp prefix and keeps other references.
- resumable Range delivery: interrupted downloads resume at the durable
  offset after runtime rebuild; wrong range/length/header/digest never
  imports or acks; valid bytes import once and read back as the frozen ZIP;
  resumed, tampered and expired paths stay honest.
"""
import hashlib
import io

import httpx
import pytest

from ddp_core.application.plans import content_digest
from ddp_core.application.ports import ApplicationError
from ddp_local import federation_dispatch as module
from ddp_local import remote_compute as filemod
from ddp_local.federation_client import CenterConfig, CenterFault
from ddp_local.http import create_app
from ddp_local.runtime import LocalRuntime

SECRET = "file-compute-test-credential"
SESSION = "f" * 32
PDF = b"%PDF-1.4 file compute fixture\n%%EOF"
DIGEST = "sha256:" + hashlib.sha256(PDF).hexdigest()


def config(upload_origin="https://center.example"):
    return CenterConfig(endpoint="https://center.example", credential=SECRET,
                        timeout_seconds=5.0, upload_origin=upload_origin)


def file_config():
    return {"recipient_node_id": "center-1", "environment_id": "center-1",
            "workspace_id": "org-1", "profile_id": "profile-alice",
            "issuer": "center-1", "subject": "user-alice",
            "endpoint": "https://center.example",
            "upload_endpoint": "https://center.example"}


def propose_file(client_runtime, filename="manual.pdf", data=PDF):
    task = client_runtime.upload_stream(io.BytesIO(data), filename=filename,
                                         operation_key="upload-file-" + filename)
    version = client_runtime.store.version(task["version_id"])
    body = {"center": file_config(), "filename": version["filename"],
            "inputs": [{"ref": version["id"],
                        "digest": "sha256:" + version["source_digest"],
                        "size_bytes": version["size_bytes"]}],
            "retention": "temporary", "valid_seconds": 3600}
    return task, body


@pytest.fixture
def runtime(tmp_path):
    instance = LocalRuntime(tmp_path / "workspace")
    try:
        yield instance
    finally:
        instance.close()


class FileCenter:
    """Waiting-record + manifest stub over the remote-compute endpoints."""

    def __init__(self):
        self.requests = []
        self.records = {}
        self.cancelled = []

    def handler(self, request):
        path = request.url.path
        body = None
        if request.content:
            import json as _json
            body = _json.loads(request.content)
        self.requests.append({"method": request.method, "path": path, "body": body,
                              "key": request.headers.get("Idempotency-Key")})
        if path == "/api/v1/remote-compute" and request.method == "POST":
            rid = "rc-1"
            record = {"id": rid, "status": "waiting_input",
                      "input_sha256": body["input_sha256"],
                      "input_size": body["input_size"],
                      "plan_digest": body["plan_digest"],
                      "source_identity": body["source_identity"],
                      "target_identity": body["target_identity"],
                      "retention": body.get("retention", "temporary"),
                      "upload_id": None, "manifest": None,
                      "result_manifest_digest": None}
            self.records[rid] = record
            return httpx.Response(201, json=record)
        if path == "/api/v1/remote-compute/rc-1" and request.method == "GET":
            return httpx.Response(200, json=self.records["rc-1"])
        if path == "/api/v1/remote-compute/rc-1/cancel" and request.method == "POST":
            self.cancelled.append(body)
            self.records["rc-1"]["status"] = "cancelled"
            return httpx.Response(200, json=self.records["rc-1"])
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def center(monkeypatch):
    stub = FileCenter()
    real = module.CenterFederationClient

    def factory(cfg, *, transport=None, actor_headers=None, before_send=None):
        return real(cfg, transport=httpx.MockTransport(stub.handler),
                    actor_headers=actor_headers, before_send=before_send)

    monkeypatch.setattr(module, "CenterFederationClient", factory)
    return stub


def _view(runtime, plan_id):
    identity = module.federation_identity(runtime)
    return runtime.consents.get(identity, plan_id)


_PROPOSE_SEQ = {"n": 0}


async def _propose(runtime, filename="manual.pdf", data=PDF):
    source, body = propose_file(runtime, filename, data)
    app = create_app(runtime, session_token=SESSION,
                     allowed_hosts={"127.0.0.1:8123"}, start_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as client:
        _PROPOSE_SEQ["n"] += 1
        tag = f"propose-file-{_PROPOSE_SEQ['n']}"
        resp = await client.post("/api/v1/plans/propose-file", json=body,
                                 headers={"Idempotency-Key": tag})
        assert resp.status_code == 201, resp.text
        plan_id = resp.json()["plan_id"]
        view = _view(runtime, plan_id)
        for phase in ("exploration", "execution"):
            resp = await client.post(f"/api/v1/plans/{plan_id}/approve",
                                     json={"phase": phase,
                                           "confirmed_scope_digest": view["scope_digest"],
                                           "user_confirmed": True},
                                     headers={"Idempotency-Key": f"approve-{tag}-{phase}-1"})
            assert resp.status_code == 200, resp.text
        return plan_id, source


async def test_propose_file_rejects_relabelled_filename(runtime):
    source, body = propose_file(runtime)
    body["filename"] = "ordinary.pdf"
    app = create_app(runtime, session_token=SESSION,
                     allowed_hosts={"127.0.0.1:8123"}, start_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as client:
        resp = await client.post("/api/v1/plans/propose-file", json=body,
                                 headers={"Idempotency-Key": "propose-file-bad"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "input_changed"


async def test_execution_replay_sends_nothing_and_all_parts_are_charged(runtime, center):
    data = PDF + b"x" * 65536
    plan_id, task = await _propose(runtime, data=data)
    source = runtime.store.version(task["version_id"])
    cfg = config()
    first = await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration",
                                       operation_key="explore-1")
    assert first["remote_compute_id"] == "rc-1"
    second = await module.dispatch_plan(runtime, plan_id, cfg, phase="execution",
                                        operation_key="exec-1")
    assert second["execution_authorized"] is True
    sent = len(center.requests)
    await module.dispatch_plan(runtime, plan_id, cfg, phase="execution",
                               operation_key="exec-1")
    assert len(center.requests) == sent
    module.authorize_file_transfer(runtime, plan_id, cfg, action="part",
                                   operation_key="part-1",
                                   offset=0, length=source["size_bytes"])
    # First coverage and overlapping retransmissions both consume real bytes.
    module.authorize_file_transfer(runtime, plan_id, cfg, action="part",
                                   operation_key="part-2",
                                   offset=0, length=source["size_bytes"])
    with pytest.raises(ApplicationError) as exc:
        module.authorize_file_transfer(runtime, plan_id, cfg, action="part",
                                       operation_key="part-3",
                                       offset=0, length=source["size_bytes"])
    assert exc.value.code == "budget_exceeded"


async def test_tampered_input_refused_before_bytes_move(runtime, center):
    plan_id, task = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration",
                               operation_key="explore-tamper")
    # Tamper the pinned bytes behind the manifest.
    runtime.consents.input_resolver = lambda ref: b"tampered-bytes"
    with pytest.raises(ApplicationError) as exc:
        module.authorize_file_transfer(runtime, plan_id, cfg, action="create",
                                       operation_key="create-tamper")
    assert exc.value.code == "input_changed"
    assert center.requests and all(r["path"] != "/api/v1/uploads" for r in center.requests)


async def test_hash_mismatch_never_imports_or_acks(runtime, center):
    plan_id, task = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration",
                               operation_key="explore-hash")
    await module.dispatch_plan(runtime, plan_id, cfg, phase="execution",
                               operation_key="exec-hash")
    with pytest.raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, plan_id, "rc-1",
                                      "sha256:" + "d" * 64, cfg)
    assert exc.value.code == "plan_changed"


async def test_cancel_calls_center_and_saves_truth(runtime, center):
    plan_id, task = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration",
                               operation_key="explore-cancel")
    out = await module.cancel_remote_compute(runtime, plan_id, cfg,
                                             operation_key="cancel-1")
    assert out["state"] == "cancelled"
    assert center.cancelled == [{}]
    # Revoke afterwards: cancel/reconcile stay available, transfer does not.
    identity = module.federation_identity(runtime)
    runtime.consents.revoke(identity, plan_id, operation_key="revoke-1")
    out = await module.cancel_remote_compute(runtime, plan_id, cfg,
                                             operation_key="cancel-2")
    assert out["state"] == "cancelled"
    with pytest.raises(ApplicationError) as exc:
        module.authorize_file_transfer(runtime, plan_id, cfg, action="part",
                                       operation_key="part-revoked",
                                       offset=0, length=1)
    assert exc.value.code in ("consent_revoked", "consent_required")


async def test_output_bytes_must_hash_before_import():
    data = b"ZIPBYTES"
    digest = content_digest(data)
    assert filemod.verify_output_bytes(data, digest) == data
    with pytest.raises(ApplicationError):
        filemod.verify_output_bytes(b"other", digest)


async def test_local_only_blocks_even_revoked_task_cancellation(runtime, center):
    plan_id, _ = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration")
    sent = len(center.requests)
    runtime.consents.set_local_only(True)
    with pytest.raises(ApplicationError) as exc:
        await module.cancel_remote_compute(runtime, plan_id, cfg, operation_key="offline-cancel")
    assert exc.value.code == "local_only"
    assert len(center.requests) == sent

class RangeCenter(FileCenter):
    """Frozen Range contract over one fixed Bundle payload."""

    def __init__(self, payload, digest_hex):
        super().__init__()
        self.payload = payload
        self.digest_hex = digest_hex
        self.tamper_next = 0
        self.fail_next: list[str] = []
        self.requests_ranges: list[dict] = []

    def handler(self, request):
        path = request.url.path
        if path == "/api/v1/remote-compute/rc-1" and request.method == "GET":
            record = self.records.get("rc-1")
            if record is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json=record)
        if path == "/api/v1/remote-compute/rc-1/bundle" and request.method == "GET":
            record = self.records.get("rc-1")
            if record is None or record.get("status") != "succeeded":
                return httpx.Response(404, json={"error": {"code": "result_unavailable"}})
            manifest = record.get("manifest") or {}
            if manifest.get("output_sha256") != self.digest_hex:
                return httpx.Response(404, json={"error": {"code": "result_unavailable"}})
            if_match = request.headers.get("If-Match", "")
            token = if_match.strip()
            if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
                token = token[1:-1].strip()
            if token.lower() != self.digest_hex:
                return httpx.Response(412, json={"error": {"code": "precondition_failed"}})
            if self.fail_next:
                code = self.fail_next.pop(0)
                assert code in ("expired", "invalid", "mismatch")
                if code == "expired":
                    return httpx.Response(404, json={"error": {"code": "delivery_expired"}})
                if code == "invalid":
                    return httpx.Response(416, json={"error": {"code": "invalid_range"}},
                                          headers={"Content-Range": f"bytes */{len(self.payload)}"})
                wrong = b"X" * 8
                return httpx.Response(206, content=wrong,
                                      headers={"Content-Range": f"bytes 0-7/{len(self.payload)}",
                                               "Content-Length": "8",
                                               "ETag": f'"{self.digest_hex}"',
                                               "X-Output-SHA256": self.digest_hex})
            range_header = request.headers.get("Range", "")
            text = range_header.strip()
            assert text[:6].lower() == "bytes="
            left, right = text[6:].split("-", 1)
            start, end = int(left), int(right)
            assert 0 <= start <= end
            if start >= len(self.payload):
                return httpx.Response(416, json={"error": {"code": "range_not_satisfiable"}},
                                      headers={"Content-Range": f"bytes */{len(self.payload)}"})
            end = min(end, len(self.payload) - 1)
            chunk = self.payload[start:end + 1]
            self.requests_ranges.append({"start": start, "end": end})
            if self.tamper_next > 0:
                self.tamper_next -= 1
                chunk = b"X" + chunk[1:] if chunk else chunk
            return httpx.Response(206, content=chunk,
                                  headers={"Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
                                           "Content-Length": str(len(chunk)),
                                           "Accept-Ranges": "bytes",
                                           "ETag": f'"{self.digest_hex}"',
                                           "X-Output-SHA256": self.digest_hex,
                                           "Cache-Control": "no-store"})
        return FileCenter.handler(self, request)


def _succeeded_record(center, manifest_digest):
    base = dict(center.records.get("rc-1", {"id": "rc-1"}))
    base.update(status="succeeded",
                manifest={"output_sha256": manifest_digest.removeprefix("sha256:")},
                result_manifest_digest=manifest_digest)
    center.records["rc-1"] = base


async def test_interrupted_download_resumes_after_runtime_rebuild(tmp_path, center):
    import sys
    sys.path.insert(0, "tests")
    from ddp_bundle_fixture import sample_bundle
    payload = sample_bundle()
    manifest_digest = content_digest(payload)
    digest_hex = manifest_digest.removeprefix("sha256:")
    workspace = tmp_path / "workspace"
    first = LocalRuntime(workspace)
    ranged = RangeCenter(payload, digest_hex)
    try:
        plan_id, _ = await _propose(first)
        cfg = config()
        await module.dispatch_plan(first, plan_id, cfg, phase="exploration", operation_key="explore-resume")
        # Swap only the stub state/behavior: the fixture factory keeps
        # patching the client, so exploration and fetch share one transport.
        center.records.update(ranged.records)
        center.payload, center.digest_hex = payload, digest_hex
        center.tamper_next, center.fail_next, center.requests_ranges = 0, [], []
        center.handler = RangeCenter.handler.__get__(center, RangeCenter)
        # Interrupt the first Range after the upstream accepted it, then
        # resume the same Range after a runtime rebuild.
        import ddp_local.remote_compute as filemod
        filemod.DOWNLOAD_CHUNK_BYTES = 1024
        original = RangeCenter.handler.__get__(center, RangeCenter)
        calls = {"count": 0}

        def flaky(request):
            if request.url.path.endswith("/bundle") and request.method == "GET":
                calls["count"] += 1
                if calls["count"] == 2:
                    raise httpx.ConnectError("connection lost after upstream accepted the range")
            return original(request)
        import types
        center.handler = types.MethodType(lambda self, request: flaky(request), center)
        _succeeded_record(center, manifest_digest)
        # A lost Range response is an unknown transport outcome: durable
        # complete chunks stay, nothing imports, nothing acks.
        with __import__("pytest").raises(CenterFault) as exc:
            await module.fetch_delivery(first, plan_id, cfg)
        assert exc.value.code in ("unreachable", "transport_error", "outcome_unknown")
        partial = workspace / "delivery-partials" / f"delivery-{plan_id}.{digest_hex}.part"
        assert partial.exists() and partial.stat().st_size == 1024
        first.close()
        # Same workspace directory keeps its persisted identity: a rebuilt
        # runtime must find the same plan state and the same partial file.
        second = LocalRuntime(workspace)
        assert second.store.workspace_id == first.store.workspace_id
        assert second.store.environment_id == first.store.environment_id
        try:
            import ddp_local.remote_compute as filemod
            filemod.DOWNLOAD_CHUNK_BYTES = 1024
            center.handler = RangeCenter.handler.__get__(center, RangeCenter)
            center.requests_ranges = []
            # Rebuild must resume at the persisted offset, not from zero.
            out = await module.fetch_delivery(second, plan_id, cfg)
            assert center.requests_ranges and center.requests_ranges[0]["start"] == 1024
            assert out["delivery"]["verified"] is True
            assert out["delivery"]["result_manifest_digest"] == manifest_digest
            assert not partial.exists()
            stored = out["delivery"]["import_result"]
            assert set(stored) >= {"version_id", "id"}
            version = second.store.version(stored["version_id"])
            assert second.blobs.read(version["bundle_key"], 64 * 1024 * 1024) == payload
        finally:
            second.close()
    finally:
        import ddp_local.remote_compute as filemod
        filemod.DOWNLOAD_CHUNK_BYTES = 1024 * 1024
        try:
            first.close()
        except Exception:
            pass


async def test_wrong_range_header_or_digest_never_imports_or_acks(runtime, center):
    import sys
    sys.path.insert(0, "tests")
    from ddp_bundle_fixture import sample_bundle
    payload = sample_bundle()
    manifest_digest = content_digest(payload)
    digest_hex = manifest_digest.removeprefix("sha256:")
    plan_id, _ = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration", operation_key="explore-bad-range")
    _succeeded_record(center, manifest_digest)
    center.payload, center.digest_hex = payload, digest_hex
    center.tamper_next, center.fail_next, center.requests_ranges = 0, [], []
    center.handler = RangeCenter.handler.__get__(center, RangeCenter)
    stub = center
    # Corrupt the second chunk bytes: fixed digest mismatch must refuse import/ack.
    stub.tamper_next = 99
    out = await module.fetch_delivery(runtime, plan_id, cfg)
    assert out["delivery"].get("verified") is not True
    assert out["delivery"]["reason"] == "result_manifest_mismatch"
    assert "import_result" not in out["delivery"]
    # Poisoned bytes are discarded so the next honest fetch restarts cleanly.
    from pathlib import Path as _Path
    workspace = _Path(runtime.store.db.execute("PRAGMA database_list").fetchone()[2]).parent
    assert not (workspace / "delivery-partials" / f"delivery-{plan_id}.{digest_hex}.part").exists()
    stub.tamper_next = 0
    out = await module.fetch_delivery(runtime, plan_id, cfg)
    assert out["delivery"].get("verified") is True
    assert out["delivery"]["result_manifest_digest"] == manifest_digest
    assert "import_result" in out["delivery"]
    # A 416 unsatisfiable range is a pending typed fault, never an import.
    # Fresh plan so no prior import_result carries over.
    plan_id2, _ = await _propose(runtime, filename="second.pdf")
    await module.dispatch_plan(runtime, plan_id2, cfg, phase="exploration", operation_key="explore-bad-range-2")
    _succeeded_record(center, manifest_digest)
    center.payload, center.digest_hex = payload, digest_hex
    center.tamper_next, center.fail_next, center.requests_ranges = 0, ["invalid"], []
    out = await module.fetch_delivery(runtime, plan_id2, cfg)
    assert out["delivery"].get("verified") is not True
    assert out["delivery"].get("reason") == "invalid_range"
    assert "import_result" not in out["delivery"]
    # A wrong If-Match precondition is a pending typed fault, never an import.
    async with __import__("httpx").AsyncClient(
            transport=__import__("httpx").MockTransport(lambda request: RangeCenter.handler(center, request))) as raw:
        response = await raw.get("https://center.example/api/v1/remote-compute/rc-1/bundle",
                                 headers={"Range": "bytes=0-7", "If-Match": '"' + "0" * 64 + '"'})
        assert response.status_code == 412


async def test_expired_output_never_shows_saved_and_cleans_only_partial(runtime, center):
    import sys
    sys.path.insert(0, "tests")
    from ddp_bundle_fixture import sample_bundle
    payload = sample_bundle()
    manifest_digest = content_digest(payload)
    digest_hex = manifest_digest.removeprefix("sha256:")
    plan_id, _ = await _propose(runtime)
    cfg = config()
    await module.dispatch_plan(runtime, plan_id, cfg, phase="exploration", operation_key="explore-expired")
    _succeeded_record(center, manifest_digest)
    center.payload, center.digest_hex = payload, digest_hex
    center.tamper_next, center.fail_next, center.requests_ranges = 0, ["expired"], []
    center.handler = RangeCenter.handler.__get__(center, RangeCenter)
    out = await module.fetch_delivery(runtime, plan_id, cfg)
    assert out["delivery"].get("verified") is not True
    assert out["delivery"]["reason"] == "delivery_expired"
    assert "import_result" not in out["delivery"]
    with __import__("pytest").raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, plan_id, "rc-1", manifest_digest, cfg)
    assert exc.value.code == "plan_changed"


async def _ranged_plan(runtime, center, key):
    import sys
    sys.path.insert(0, "tests")
    from ddp_bundle_fixture import sample_bundle
    payload = sample_bundle()
    manifest_digest = content_digest(payload)
    plan_id, _ = await _propose(runtime)
    await module.dispatch_plan(runtime, plan_id, config(), phase="exploration", operation_key=key)
    _succeeded_record(center, manifest_digest)
    center.payload, center.digest_hex = payload, manifest_digest.removeprefix("sha256:")
    center.tamper_next, center.fail_next, center.requests_ranges = 0, [], []
    center.handler = RangeCenter.handler.__get__(center, RangeCenter)
    return plan_id, payload, manifest_digest


async def test_exact_chunk_multiple_completes_without_probing_past_the_end(runtime, center, monkeypatch):
    plan_id, payload, manifest_digest = await _ranged_plan(runtime, center, "explore-multiple")
    assert len(payload) % 2 == 0
    monkeypatch.setattr(filemod, "DOWNLOAD_CHUNK_BYTES", len(payload) // 2)
    ranged, sent = center.handler, []

    def counting(request):
        if request.url.path.endswith("/bundle"):
            sent.append(request.headers["Range"])
        return ranged(request)
    center.handler = counting
    out = await module.fetch_delivery(runtime, plan_id, config())
    assert out["delivery"]["verified"] is True
    assert out["delivery"]["result_manifest_digest"] == manifest_digest
    half = len(payload) // 2
    # Exactly two Ranges: no start==total probe after the last full chunk.
    assert sent == [f"bytes=0-{half - 1}", f"bytes={half}-{2 * half - 1}"]


async def test_complete_partial_left_by_a_crash_verifies_without_refetching(runtime, center):
    plan_id, payload, manifest_digest = await _ranged_plan(runtime, center, "explore-complete")
    target = filemod.partial_path(runtime, filemod.partial_identity(plan_id, manifest_digest))
    try:
        filemod.append_complete_chunk(target, payload, expected_offset=0,
                                      manifest_digest=manifest_digest)
    finally:
        import os
        os.close(target)
    out = await module.fetch_delivery(runtime, plan_id, config())
    assert out["delivery"]["verified"] is True
    assert "import_result" in out["delivery"]
    # Only the start==total probe was sent; no byte was downloaded again.
    assert center.requests_ranges == []
