"""Supervisor protocol fixtures. These tests do not run inference or satisfy T16/T51."""

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
import time

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.model_runtime.install import ModelInstaller
from ddp_local.model_runtime.process import ModelProcess, extract_runtime


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
