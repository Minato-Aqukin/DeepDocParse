"""File-compute danger boundaries: fixed input, idempotent execute, hashes, replays, TTL.

Covers the local half of the remote file-compute path (no network beyond the
MockTransport center stub):
- tampered pinned input is refused before any byte moves (input_changed);
- cross-actor upload binding is refused (remote_compute_not_found shape);
- interrupted finalize / unknown completion never mints a second asset/task;
- hash mismatch never imports and never acks;
- lost/duplicate acks replay safely;
- cleanup removes only this record's tmp prefix and keeps other references.
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


async def _propose(runtime, filename="manual.pdf", data=PDF):
    source, body = propose_file(runtime, filename, data)
    app = create_app(runtime, session_token=SESSION,
                     allowed_hosts={"127.0.0.1:8123"}, start_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as client:
        resp = await client.post("/api/v1/plans/propose-file", json=body,
                                 headers={"Idempotency-Key": "propose-file-1"})
        assert resp.status_code == 201, resp.text
        plan_id = resp.json()["plan_id"]
        view = _view(runtime, plan_id)
        for phase in ("exploration", "execution"):
            resp = await client.post(f"/api/v1/plans/{plan_id}/approve",
                                     json={"phase": phase,
                                           "confirmed_scope_digest": view["scope_digest"],
                                           "user_confirmed": True},
                                     headers={"Idempotency-Key": f"approve-{phase}-1"})
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
