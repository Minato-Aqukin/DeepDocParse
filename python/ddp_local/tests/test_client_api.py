"""Client cursor consistency and real loopback reconnect against persisted local state."""

import json
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
PDF = ROOT / "tests/fixtures/sample.pdf"


@contextmanager
def server(workspace):
    process = subprocess.Popen(
        [sys.executable, "-m", "ddp_local", "--workspace", str(workspace), "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    token_path = None
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "runtime bootstrap was not published"
        bootstrap = json.loads(process.stdout.readline())
        assert "token" not in bootstrap
        token_path = Path(bootstrap["token_file"])
        assert token_path.stat().st_mode & 0o777 == 0o600
        secret = json.loads(token_path.read_text())
        assert secret["token"] not in json.dumps(bootstrap)
        with httpx.Client(
            base_url=bootstrap["url"], trust_env=False, timeout=5,
            headers={"Authorization": "Bearer " + secret["token"]},
        ) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert client.get("/api/v1/client/handshake").status_code == 200
                    break
                except httpx.ConnectError:
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.03)
            yield client, bootstrap
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if token_path:
            assert not token_path.exists(), "clean exit must remove session credentials"


def test_real_http_reconnect_snapshot_ack_cross_workspace_and_receipt(tmp_path):
    workspace = tmp_path / "one"
    with server(workspace) as (client, bootstrap):
        handshake = client.get("/api/v1/client/handshake").json()
        assert handshake["protocol_version"] == "ddp-client/1"
        assert handshake["identity"] == bootstrap["identity"]
        assert handshake["profile"] == bootstrap["profile"]
        assert "client.receipt" in handshake["capabilities"]
        assert client.get("/api/v1/client/handshake", headers={"Authorization": ""}).status_code == 401
        initial = client.get("/api/v1/client/snapshot").json()
        assert initial["sequence"] == 1 and initial["state"]["tasks"] == []
        assert client.get("/api/v1/client/receipts/unknown").status_code == 404
        assert client.get("/api/v1/client/snapshot").json() == initial
        admitted = client.post(
            "/api/v1/resources/upload", content=PDF.read_bytes(),
            headers={"X-Filename": "sample.pdf", "Idempotency-Key": "upload/reconnect"},
        )
        assert admitted.status_code == 202, admitted.text
        task_id = admitted.json()["id"]
        deadline = time.monotonic() + 15
        while True:
            receipt = client.get("/api/v1/client/receipts/upload%2Freconnect").json()
            if receipt["status"] == "succeeded":
                break
            assert receipt["status"] in {"queued", "running"}, receipt
            assert time.monotonic() < deadline
            time.sleep(0.03)
        batch = client.get("/api/v1/client/events", params={"after": initial["cursor"]}).json()["events"]
        assert len(batch) == 1
        event = batch[0]
        assert event["previous_sequence"] == initial["sequence"] < event["sequence"]
        assert event["state"]["tasks"][0]["id"] == task_id
        assert event["state"]["tasks"][0]["status"] == "succeeded"
        assert event["state"]["resources"][0]["state"] == "ready"
        assert "lease_until" not in event["state"]["tasks"][0]
        ack = client.get("/api/v1/client/events", params={"after": event["cursor"]}).json()["events"][0]
        assert ack["sequence"] == ack["previous_sequence"] == event["sequence"]
        assert ack["state"] == event["state"]
        assert client.get("/api/v1/client/events", params={"after": "invalid"}).status_code == 410
    with server(workspace) as (client, reopened):
        assert reopened["identity"] == bootstrap["identity"]
        assert reopened["profile"] == bootstrap["profile"]
        snapshot = client.get("/api/v1/client/snapshot").json()
        assert snapshot["sequence"] > event["sequence"]
        assert snapshot["state"] == event["state"]
        resumed = client.get("/api/v1/client/events", params={"after": event["cursor"]}).json()["events"][0]
        assert resumed["previous_sequence"] == event["sequence"]
        assert resumed["cursor"] == snapshot["cursor"]
        receipt = client.get("/api/v1/client/receipts/upload%2Freconnect").json()
        assert receipt["id"] == task_id and receipt["status"] == "succeeded"
    with server(tmp_path / "two") as (client, other):
        assert other["identity"] != bootstrap["identity"]
        assert client.get("/api/v1/client/events", params={"after": event["cursor"]}).status_code == 410
        assert client.get("/api/v1/client/receipts/upload%2Freconnect").status_code == 404


def test_snapshot_resources_tasks_and_cursor_use_one_wal_read_transaction(tmp_path, monkeypatch):
    reader, writer = LocalRuntime(tmp_path / "workspace"), LocalRuntime(tmp_path / "workspace")
    try:
        original = reader.store.versions

        def upload_between_snapshot_reads():
            writer.upload_file(str(PDF), operation_key="concurrent")
            return original()

        monkeypatch.setattr(reader.store, "versions", upload_between_snapshot_reads)
        snapshot = reader.store.client_snapshot(reader.capabilities())
        assert snapshot["sequence"] == 0
        assert snapshot["state"]["resources"] == snapshot["state"]["tasks"] == []
        monkeypatch.setattr(reader.store, "versions", original)
        later = reader.store.client_snapshot(reader.capabilities())
        assert later["sequence"] == 1
        assert len(later["state"]["resources"]) == len(later["state"]["tasks"]) == 1
    finally:
        reader.close()
        writer.close()


def test_pruned_history_requires_snapshot_and_preserves_highwater(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        initial = runtime.store.client_snapshot(runtime.capabilities())
        task = runtime.upload_file(str(PDF), operation_key="retention")
        runtime.store.cancel(task["id"])
        current = runtime.store.client_snapshot(runtime.capabilities())
        with runtime.store.tx():
            runtime.store.db.execute("DELETE FROM events")
        with pytest.raises(ApplicationError) as expired:
            runtime.store.client_snapshot(runtime.capabilities(), after=initial["cursor"])
        assert expired.value.code == "cursor_expired"
        renewed = runtime.store.client_snapshot(runtime.capabilities())
        assert renewed["sequence"] == current["sequence"]
        ack = runtime.store.client_snapshot(runtime.capabilities(), after=renewed["cursor"])
        assert ack["sequence"] == ack["previous_sequence"]
        for bad in (f"local.{runtime.store.workspace_id}.999", f"local.{runtime.store.workspace_id}.01"):
            with pytest.raises(ApplicationError) as rejected:
                runtime.store.client_snapshot(runtime.capabilities(), after=bad)
            assert rejected.value.code == "cursor_expired"
    finally:
        runtime.close()
