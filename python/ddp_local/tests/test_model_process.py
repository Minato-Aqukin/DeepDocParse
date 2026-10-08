"""Supervisor protocol fixtures. These tests do not run inference or satisfy T16/T51."""

import asyncio
import hashlib
import io
import json
import os
import platform
import select
import signal
import subprocess
import sys
import tarfile
import threading
import time

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.model_runtime.install import ModelInstaller
from ddp_local.model_runtime.process import (
    LOG_CAP_BYTES,
    LOG_DROPPED_MARKER,
    BoundedLogPump,
    ModelProcess,
    extract_runtime,
    sweep_orphan_runtimes,
    tail_bytes,
)


SERVER = b'''#!/usr/bin/python3
import http.server,json,sys
args=sys.argv[1:]
def value(name): return args[args.index(name)+1]
key=open(value('--api-key-file')).read()
alias=value('--alias')
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.headers.get('Authorization') != 'Bearer '+key:
   self.send_response(401);self.end_headers();return
  body=json.dumps({'data':[{'id':alias}]} if self.path=='/v1/models' else {'status':'ok'}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
http.server.HTTPServer((value('--host'),int(value('--port'))),Handler).serve_forever()
'''


def archive_bytes(body, *, filename="runtime/server"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        entry = tarfile.TarInfo(filename)
        entry.size, entry.mode = len(body), 0o700
        archive.addfile(entry, io.BytesIO(body))
    return stream.getvalue()


@pytest.fixture
def installed(tmp_path):
    model = b"GGUF\x03\0\0\0fixture-only"
    backend = archive_bytes(SERVER)
    common = {"version": "protocol-test", "license": "MIT", "license_url": "https://fixture.example/license",
              "backend": "llama.cpp", "device": "cpu"}
    definitions = {
        "schema": "ddp-model-catalog/1", "revision": "protocol-test",
        "artifacts": [
            {**common, "id": "fixture-model", "kind": "model", "format": "gguf-v3", "filename": "fixture.gguf",
             "url": "https://fixture.example/model", "bytes": len(model), "sha256": hashlib.sha256(model).hexdigest(),
             "runtime_id": "fixture-runtime", "architecture": "test"},
            {**common, "id": "fixture-runtime", "kind": "runtime", "format": "tar-gz", "filename": "runtime.tar.gz",
             "url": "https://fixture.example/runtime", "bytes": len(backend), "sha256": hashlib.sha256(backend).hexdigest(),
             "architectures": ["test"], "platform": f"{sys.platform}-{platform.machine()}",
             "executable": "runtime/server", "library_dirs": [], "unpacked_bytes": 65536},
        ],
    }
    installer = ModelInstaller(tmp_path / "models", definitions=definitions)
    for identifier, payload in (("fixture-model", model), ("fixture-runtime", backend)):
        path = tmp_path / identifier
        path.write_bytes(payload)
        installer.import_file(identifier, path)
    yield installer
    installer.close()


async def test_owned_model_handshake_and_stop_leave_unowned_process_running(installed):
    owned = ModelProcess(installed)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        selection = await owned.start("fixture-model", timeout=5)
        assert owned.status()["status"] == "ready" and selection.location == "local"
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(selection.endpoint + "/models")).status_code == 401
            good = await client.get(selection.endpoint + "/models", headers={"Authorization": "Bearer " + selection.api_key})
            assert good.json()["data"][0]["id"] == selection.model
        process, directory = owned.process, owned.workdir
        assert (await owned.start("fixture-model", timeout=5)) is selection
        assert (await owned.stop())["status"] == "stopped"
        assert process.poll() is not None and not directory.exists()
        assert unrelated.poll() is None, "never signal a process this supervisor did not start"
    finally:
        await owned.stop()
        unrelated.terminate()
        unrelated.wait(timeout=5)


async def test_runtime_compatibility_and_missing_model_fail_before_process_launch(installed):
    owned = ModelProcess(installed)
    backend = installed.artifact("fixture-runtime")
    backend["platform"] = "unsupported-system"
    with pytest.raises(ApplicationError) as incompatible:
        await owned.start("fixture-model")
    assert incompatible.value.code == "model_backend_incompatible" and owned.process is None
    backend["platform"] = f"{sys.platform}-{platform.machine()}"
    os.unlink(installed.path(installed.artifact("fixture-model")))
    with pytest.raises(ApplicationError) as missing:
        await owned.start("fixture-model")
    assert missing.value.code == "model_not_installed" and owned.process is None


async def test_glibc_floor_below_host_refuses_before_launch(installed, monkeypatch):
    owned = ModelProcess(installed)
    backend = installed.artifact("fixture-runtime")
    backend["min_libc_version"] = "2.34"
    monkeypatch.setattr("ddp_local.model_runtime.process.host_libc_version", lambda: ("glibc", (2, 31)))
    with pytest.raises(ApplicationError) as old:
        await owned.start("fixture-model")
    assert old.value.code == "runtime_host_incompatible" and owned.process is None
    assert "2.34" in str(old.value) and "2.31" in str(old.value)


async def test_glibc_floor_met_starts_and_musl_fails_closed(installed, monkeypatch):
    owned = ModelProcess(installed)
    backend = installed.artifact("fixture-runtime")
    backend["min_libc_version"] = "2.34"
    monkeypatch.setattr("ddp_local.model_runtime.process.host_libc_version", lambda: ("glibc", (2, 35)))
    try:
        selection = await owned.start("fixture-model", timeout=5)
        assert selection.location == "local"
    finally:
        await owned.stop()
    monkeypatch.setattr("ddp_local.model_runtime.process.host_libc_version", lambda: (None, None))
    with pytest.raises(ApplicationError) as musl:
        await owned.start("fixture-model")
    assert musl.value.code == "runtime_host_incompatible" and owned.process is None
    assert "musl" in str(musl.value).lower() or "no glibc" in str(musl.value).lower()


def install_gpu_fixture(installed, tmp_path, startup_log):
    """Supervisor protocol only; these messages are not hardware validation."""
    body = SERVER.replace(b"import http.server,json,sys\n",
                          b"import http.server,json,sys\n" + f"print({startup_log!r}, flush=True)\n".encode())
    payload = archive_bytes(body)
    backend = {**installed.artifact("fixture-runtime"), "id": "fixture-vulkan", "device": "gpu",
               "gpu_api": "vulkan", "default_gpu_layers": 99, "bytes": len(payload),
               "sha256": hashlib.sha256(payload).hexdigest()}
    installed.definitions["artifacts"].append(backend)
    installed.artifact("fixture-model")["runtime_ids"] = ["fixture-runtime", "fixture-vulkan"]
    source = tmp_path / "vulkan.tar.gz"
    source.write_bytes(payload)
    installed.import_file("fixture-vulkan", source)


@pytest.mark.parametrize(("startup_log", "error"), [
    ("CPU endpoint is healthy, but nothing was offloaded", "gpu_offload_unverified"),
    ("llama_prepare_model_devices: using device Vulkan0 (llvmpipe LLVM software) (0000:00:00.0) - 100 MiB free\n"
     "load_tensors: offloaded 2/2 layers to GPU\n"
     "load_tensors: Vulkan0 model buffer size = 1.00 MiB\n", "gpu_device_unsupported"),
])
async def test_http_health_cannot_disguise_cpu_or_software_as_physical_gpu(installed, tmp_path, startup_log, error):
    install_gpu_fixture(installed, tmp_path, startup_log)
    owned = ModelProcess(installed)
    try:
        with pytest.raises(ApplicationError) as failure:
            await owned.start("fixture-model", runtime_id="fixture-vulkan", timeout=5)
        assert failure.value.code == error
        assert owned.process is None and owned.selection is None
        assert owned.status()["status"] == "failed" and owned.status()["error"] == error
        # Recovery is an explicit new CPU selection, not a hidden retry.
        recovered = await owned.start("fixture-model", runtime_id="fixture-runtime", timeout=5)
        assert recovered.provenance["device"] == "cpu"
    finally:
        await owned.stop()


async def test_selected_profile_requires_observed_offload_and_cannot_silently_switch_a_live_model(installed, tmp_path):
    install_gpu_fixture(installed, tmp_path,
        "llama_prepare_model_devices: using device Vulkan0 (Fixture physical device (vendor)) (0000:00:00.0) - 100 MiB free\n"
        "load_tensors: offloaded 2/2 layers to GPU\n"
        "load_tensors: Vulkan0 model buffer size = 1.00 MiB\n")
    owned = ModelProcess(installed)
    try:
        selected = await owned.start("fixture-model", runtime_id="fixture-vulkan", timeout=5)
        assert selected.provenance["device"] == "gpu" and selected.provenance["offloaded_layers"] == 2
        with pytest.raises(ApplicationError) as busy:
            await owned.start("fixture-model", runtime_id="fixture-runtime")
        assert busy.value.code == "model_process_busy"
        assert owned.selection is selected and owned.process.poll() is None
    finally:
        await owned.stop()


@pytest.mark.parametrize("filename", ["../outside", "/tmp/outside", "safe/../../outside"])
def test_runtime_archive_cannot_escape_output(tmp_path, filename):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(archive_bytes(b"bad", filename=filename))
    target = tmp_path / "unpack"
    target.mkdir()
    with pytest.raises(ApplicationError) as unsafe:
        extract_runtime(archive, target, {"executable": "runtime/server", "unpacked_bytes": 1024})
    assert unsafe.value.code == "runtime_archive_unsafe"
    assert not list(target.iterdir())


def test_internal_runtime_library_links_are_copied_not_created(tmp_path):
    archive = tmp_path / "links.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        for name, body in (("runtime/server", b"binary"), ("runtime/library.so.1", b"library")):
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            package.addfile(entry, io.BytesIO(body))
        link = tarfile.TarInfo("runtime/library.so")
        link.type, link.linkname = tarfile.SYMTYPE, "library.so.1"
        package.addfile(link)
    target = tmp_path / "unpack"
    target.mkdir()
    extract_runtime(archive, target, {"executable": "runtime/server", "unpacked_bytes": 1024})
    link = target / "runtime/library.so"
    assert link.is_file() and not link.is_symlink() and link.read_bytes() == b"library"


def test_model_process_dies_when_its_runtime_owner_is_killed(installed, tmp_path):
    definitions = tmp_path / "catalog.json"
    definitions.write_text(json.dumps(installed.definitions))
    program = """
import asyncio,json,sys
from ddp_local.model_runtime.install import ModelInstaller
from ddp_local.model_runtime.process import ModelProcess
installer=ModelInstaller(sys.argv[1],definitions=json.load(open(sys.argv[2])))
owned=ModelProcess(installer)
async def main():
 await owned.start('fixture-model',timeout=5)
 print(owned.process.pid,flush=True)
 await asyncio.sleep(60)
asyncio.run(main())
"""
    owner = subprocess.Popen([sys.executable, "-c", program, str(installed.directory), str(definitions)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pidfd = None
    try:
        ready, _, _ = select.select([owner.stdout], [], [], 10)
        assert ready, "owned model did not publish its process ID"
        line = owner.stdout.readline()
        assert line.strip().isdigit(), owner.stderr.read()
        pid = int(line)
        pidfd = os.pidfd_open(pid)
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            assert time.monotonic() < deadline, "model survived its owning runtime"
            time.sleep(0.02)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if pidfd is not None:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(pidfd)


async def test_owned_runtime_oom_is_visible_and_explicit_restart_can_recover(installed, tmp_path):
    backend = installed.artifact("fixture-runtime")
    original = dict(backend)
    original_archive = (installed.directory / installed.name(backend)).read_bytes()
    failure = archive_bytes(b"#!/bin/sh\necho 'Cannot allocate memory' >&2\nexit 1\n")
    backend.update(bytes=len(failure), sha256=hashlib.sha256(failure).hexdigest())
    source = tmp_path / "oom-runtime.tar.gz"
    source.write_bytes(failure)
    installed.import_file(backend["id"], source)
    owned = ModelProcess(installed)
    try:
        with pytest.raises(ApplicationError) as oom:
            await owned.start("fixture-model", timeout=5)
        assert oom.value.code == "out_of_memory"
        assert owned.status()["status"] == "failed" and owned.status()["error"] == "out_of_memory"
        assert owned.process is None and owned.workdir is None
        backend.clear()
        backend.update(original)
        source.write_bytes(original_archive)
        installed.import_file(backend["id"], source)
        assert (await owned.start("fixture-model", timeout=5)).location == "local"
        assert owned.status()["status"] == "ready" and owned.status()["error"] is None
    finally:
        await owned.stop()


def test_runtime_archive_bounds_member_metadata_before_materialization(tmp_path):
    archive = tmp_path / "too-many.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        for index in range(4097):
            package.addfile(tarfile.TarInfo(f"runtime/member-{index}"), io.BytesIO())
    target = tmp_path / "unpack"
    target.mkdir()
    with pytest.raises(ApplicationError) as unsafe:
        extract_runtime(archive, target, {"executable": "runtime/server", "unpacked_bytes": 1024})
    assert unsafe.value.code == "runtime_archive_unsafe"
    assert not list(target.iterdir())


def test_orphan_sweep_removes_only_exact_runtime_dirs(installed):
    models = installed.directory
    exact = models / (".runtime-" + "ab" * 16)
    exact.mkdir(mode=0o700)
    (exact / "api-key.txt").write_text("secret")
    (exact / "model.log").write_bytes(b"stale")
    (models / ".runtime-evil").mkdir()
    (models / ".runtime-short").mkdir()
    (models / (".runtime-" + "zz" * 16)).mkdir()
    regular = models / (".runtime-" + "cd" * 16)
    regular.write_bytes(b"keep me")
    target = models / "sweep-target"
    target.mkdir()
    link = models / (".runtime-" + "ef" * 16)
    link.symlink_to(target, target_is_directory=True)
    try:
        assert sweep_orphan_runtimes(installed) == 1
        assert not exact.exists()
        assert (models / ".runtime-evil").is_dir()
        assert (models / ".runtime-short").is_dir()
        assert (models / (".runtime-" + "zz" * 16)).is_dir()
        assert regular.read_bytes() == b"keep me"
        assert link.is_symlink() and target.is_dir()
    finally:
        for leftover in models.iterdir():
            if leftover.is_symlink() or leftover.is_file():
                leftover.unlink()
            else:
                import shutil
                shutil.rmtree(leftover, ignore_errors=True)
        for identifier in ("fixture-model", "fixture-runtime"):
            source = models.parent / identifier
            installed.import_file(identifier, source)


def test_tail_bytes_returns_last_bytes_without_loading_all(tmp_path):
    log = tmp_path / "model.log"
    with open(log, "wb") as output:
        output.truncate(5 * 1024 * 1024)
        output.seek(0)
        output.write(b"A" * 1024)
        output.seek(5 * 1024 * 1024 - 7)
        output.write(b"TAILEND")
    assert tail_bytes(log, 7) == b"TAILEND"
    assert tail_bytes(log, 16384)[-7:] == b"TAILEND"


def test_bounded_pump_caps_file_and_rotates_with_marker(tmp_path):
    log = tmp_path / "model.log"
    cap = len(LOG_DROPPED_MARKER) + 96
    pump = BoundedLogPump(log, cap=cap)
    pump.write(b"x" * 96)
    pump.write(b"y" * 96)
    assert pump.dropped == 1
    previous = tmp_path / "model.log.prev"
    assert previous.exists() and previous.stat().st_size == cap
    body = log.read_bytes()
    assert len(body) <= cap and body.startswith(LOG_DROPPED_MARKER)
    assert body.endswith(b"y" * 32)


def test_bounded_pump_thread_joins_on_stop(tmp_path):
    log = tmp_path / "model.log"
    pump = BoundedLogPump(log, cap=LOG_CAP_BYTES)
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb")
    thread = pump.start(stream)

    def close_on_stop():
        if pump._stop.wait(timeout=5):
            time.sleep(0.2)
            try:
                os.close(writer)
            except OSError:
                pass

    closer = threading.Thread(target=close_on_stop, daemon=True)
    closer.start()
    try:
        os.write(writer, b"hello")
        deadline = time.monotonic() + 5
        while log.read_bytes() != b"hello":
            assert time.monotonic() < deadline, "pump did not drain the pipe"
            time.sleep(0.01)
        assert thread.is_alive()
        pump.join(timeout=5)
        assert not thread.is_alive()
        assert pump._thread is None
        assert log.read_bytes() == b"hello"
    finally:
        try:
            os.close(writer)
        except OSError:
            pass
        pump._stop.set()
        pump.join(timeout=5)
        closer.join(timeout=5)
    assert not [t for t in threading.enumerate() if t.name == "ddp-model-log" and t.is_alive()]


async def test_failed_start_reads_bounded_tail_and_drops_pump_thread(installed, tmp_path, monkeypatch):
    backend = installed.artifact("fixture-runtime")
    original = dict(backend)
    original_archive = (installed.directory / installed.name(backend)).read_bytes()
    failing = archive_bytes(b"#!/bin/sh\necho boom >&2\nexit 1\n")
    backend.update(bytes=len(failing), sha256=hashlib.sha256(failing).hexdigest())
    source = tmp_path / "failing-runtime.tar.gz"
    source.write_bytes(failing)
    installed.import_file(backend["id"], source)
    calls = []
    real_tail = tail_bytes

    def spy(path, limit=16384):
        calls.append((str(path), limit))
        return real_tail(path, limit)

    joins = []
    joined_threads = []
    real_join = BoundedLogPump.join

    def join_spy(self, timeout=5):
        joins.append(self)
        joined_threads.append(self._thread)
        return real_join(self, timeout=timeout)

    monkeypatch.setattr("ddp_local.model_runtime.process.tail_bytes", spy)
    monkeypatch.setattr(BoundedLogPump, "join", join_spy)
    owned = ModelProcess(installed)
    try:
        with pytest.raises(ApplicationError) as failure:
            await owned.start("fixture-model", timeout=5)
        assert failure.value.code == "model_start_failed"
        assert calls, "failure path must bound the log read through tail_bytes"
        assert all(name.endswith("model.log") for name, _ in calls)
        assert calls[0][1] <= 16384
        assert joins, "failed start must join the log pump"
        joined_threads[0].join(timeout=5)
        assert not joined_threads[0].is_alive()
        assert owned.process is None and owned.workdir is None
        assert owned.log_pump is None
        assert not [t for t in threading.enumerate() if t.name == "ddp-model-log" and t.is_alive()]
    finally:
        backend.clear()
        backend.update(original)
        source.write_bytes(original_archive)
        installed.import_file(backend["id"], source)
        await owned.stop()
        assert owned.log_pump is None
        assert not [t for t in threading.enumerate() if t.name == "ddp-model-log" and t.is_alive()]


async def test_stop_joins_pump_and_clears_thread_after_successful_start(installed, monkeypatch):
    joins = []
    real_join = BoundedLogPump.join

    def join_spy(self, timeout=5):
        joins.append(self)
        return real_join(self, timeout=timeout)

    monkeypatch.setattr(BoundedLogPump, "join", join_spy)
    owned = ModelProcess(installed)
    try:
        assert (await owned.start("fixture-model", timeout=5)).location == "local"
        assert owned.log_pump is not None
        thread = owned.log_pump._thread
        assert thread is not None and thread.is_alive()
        pump = owned.log_pump
        await owned.stop()
        assert pump in joins
        assert not thread.is_alive()
        assert owned.log_pump is None
        assert not [t for t in threading.enumerate() if t.name == "ddp-model-log" and t.is_alive()]
    finally:
        await owned.stop()
        assert owned.log_pump is None
        assert not [t for t in threading.enumerate() if t.name == "ddp-model-log" and t.is_alive()]


async def test_stop_interrupts_a_start_waiting_on_readiness(installed, monkeypatch):
    owned = ModelProcess(installed)
    entered = threading.Event()
    release = threading.Event()
    real_extract = extract_runtime

    def slow_extract(archive, destination, artifact):
        entered.set()
        assert release.wait(15)
        return real_extract(archive, destination, artifact)

    monkeypatch.setattr("ddp_local.model_runtime.process.extract_runtime", slow_extract)
    task = asyncio.create_task(owned.start("fixture-model", timeout=60))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        before = time.monotonic()
        result = await owned.stop()
        with pytest.raises(ApplicationError) as cancelled:
            await asyncio.wait_for(task, timeout=15)
        assert cancelled.value.code == "model_start_cancelled"
        assert time.monotonic() - before < 30
        assert owned.process is None and owned.workdir is None and owned.log_pump is None
        assert result["status"] == "stopped"
        assert owned.status()["status"] == "stopped"
    finally:
        release.set()
        try:
            await asyncio.wait_for(task, timeout=15)
        except ApplicationError:
            pass
        await owned.stop()


async def test_second_start_while_starting_reports_busy(installed, monkeypatch):
    owned = ModelProcess(installed)
    entered = threading.Event()
    release = threading.Event()
    real_extract = extract_runtime

    def slow_extract(archive, destination, artifact):
        entered.set()
        assert release.wait(15)
        return real_extract(archive, destination, artifact)

    monkeypatch.setattr("ddp_local.model_runtime.process.extract_runtime", slow_extract)
    task = asyncio.create_task(owned.start("fixture-model", timeout=60))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(ApplicationError) as busy:
            await owned.start("fixture-model", timeout=5)
        assert busy.value.code == "model_process_busy"
    finally:
        release.set()
        try:
            await asyncio.wait_for(task, timeout=15)
        except ApplicationError:
            pass
        await owned.stop()


async def test_low_memory_refuses_start(installed, monkeypatch):
    installed.artifact("fixture-model")["minimum_memory_bytes"] = 100 * 1024**3
    monkeypatch.setattr("ddp_local.model_runtime.install.host_free_bytes", lambda: 1024)
    owned = ModelProcess(installed)
    try:
        with pytest.raises(ApplicationError) as refused:
            await owned.start("fixture-model", timeout=5)
        assert refused.value.code == "out_of_memory"
        assert owned.process is None
    finally:
        del installed.artifact("fixture-model")["minimum_memory_bytes"]
        await owned.stop()


async def test_ample_memory_starts_without_warning(installed, monkeypatch):
    monkeypatch.setattr(
        "ddp_local.model_runtime.install.host_free_bytes", lambda: 256 * 1024**3)
    owned = ModelProcess(installed)
    try:
        assert (await owned.start("fixture-model", timeout=5)).location == "local"
    finally:
        await owned.stop()
