"""The two additive `serve` increments the WSL bridge depends on."""

import json
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def start_serve(workspace, *arguments):
    return subprocess.Popen(
        [sys.executable, "-m", "ddp_local", "--workspace", str(workspace), "serve", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def bootstrap_from(process):
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready, "runtime bootstrap was not published"
    return json.loads(process.stdout.readline())


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def await_handshake(bootstrap):
    with httpx.Client(
        base_url=bootstrap["url"], trust_env=False, timeout=5,
        headers={"Authorization": "Bearer " + bootstrap["token"]},
    ) as client:
        deadline = time.monotonic() + 10
        while True:
            try:
                response = client.get("/api/v1/client/handshake")
            except httpx.ConnectError:
                assert time.monotonic() < deadline
                time.sleep(0.03)
                continue
            break
        assert response.status_code == 200
        assert response.json()["protocol_version"] == "ddp-client/1"


def test_dash_token_file_prints_the_whole_bootstrap_and_writes_nothing(tmp_path):
    workspace = tmp_path / "dashed"
    process = start_serve(workspace, "--token-file", "-")
    try:
        bootstrap = bootstrap_from(process)
        assert bootstrap["pid"] == process.pid
        assert bootstrap["url"].startswith("http://127.0.0.1:")
        assert TOKEN.fullmatch(bootstrap["token"])
        assert "token_file" not in bootstrap
        assert not (tmp_path / "-").exists()
        assert not (Path.cwd() / "-").exists()
        assert not list(tmp_path.rglob("session-*.json"))
        assert not list(workspace.glob("session-*.json"))
        await_handshake(bootstrap)
    finally:
        stop(process)


def test_sigterm_during_startup_never_leaves_the_session_file(tmp_path):
    """SIGTERM may land between token-file creation and uvicorn's handlers.

    The signal is sent the moment the bootstrap line appears; that line is
    printed before uvicorn starts, so this is the window the early handler must
    cover. The handoff is still a race (a late signal takes uvicorn's graceful
    path), hence the retries -- but the assertion is unconditional: whatever
    the timing, no session credential may be left behind.
    """
    for attempt in range(6):
        workspace = tmp_path / f"sigterm-{attempt}"
        process = start_serve(workspace)
        try:
            bootstrap_from(process)
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            stop(process)
        leftovers = sorted(workspace.rglob("session-*.json"))
        assert not leftovers, f"attempt {attempt} left {leftovers}"


def test_default_token_file_keeps_the_secret_off_stdout_and_removes_it(tmp_path):
    workspace = tmp_path / "persisted"
    process = start_serve(workspace)
    token_path = None
    try:
        bootstrap = bootstrap_from(process)
        assert bootstrap["pid"] == process.pid
        assert bootstrap["url"].startswith("http://127.0.0.1:")
        assert "token" not in bootstrap
        token_path = Path(bootstrap["token_file"])
        assert token_path.stat().st_mode & 0o777 == 0o600
        token = json.loads(token_path.read_text())["token"]
        assert TOKEN.fullmatch(token)
        await_handshake({**bootstrap, "token": token})
    finally:
        stop(process)
    assert token_path is not None
    assert not token_path.exists()
