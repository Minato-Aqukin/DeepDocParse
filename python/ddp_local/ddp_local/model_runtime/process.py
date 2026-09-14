"""One owned llama-server process; no PATH lookup, shell commands, or remote fallback."""

import asyncio
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

import httpx

from ddp_core.application.ports import ApplicationError
from ddp_local.providers import ModelSelection
from ddp_local.model_runtime.install import settled_io


def safe_member(name):
    path = PurePosixPath(name)
    if not name or "\\" in name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise ApplicationError("runtime_archive_unsafe", "runtime archive contains an unsafe path")
    return path


def extract_runtime(archive, destination, artifact):
    """Materialize reviewed tar contents without ever creating an archive symlink."""
    maximum = artifact.get("unpacked_bytes", 1024**3)
    with tarfile.open(archive, mode="r:gz") as source:
        members, total = {}, 0
        for item in source:
            # Bound metadata while parsing, not after getmembers() has already
            # materialized an arbitrarily large member list in memory.
            if len(members) >= 4096:
                raise ApplicationError("runtime_archive_unsafe", "runtime has too many members")
            path = safe_member(item.name)
            canonical = str(path)
            if canonical in members:
                raise ApplicationError("runtime_archive_unsafe", "runtime member is duplicated")
            if not (item.isdir() or item.isreg() or item.issym() or item.islnk()):
                raise ApplicationError("runtime_archive_unsafe", "runtime contains a special device")
            members[canonical] = item
            total += max(0, item.size)
            if total > maximum:
                raise ApplicationError("runtime_archive_unsafe", "runtime exceeds its expanded budget")
        for path, item in members.items():
            target = destination / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
                continue
            resolved, visited = item, {path}
            while resolved.issym() or resolved.islnk():
                link = safe_member(resolved.linkname)
                resolved_path = str(safe_member(str(PurePosixPath(resolved.name).parent / link))) if resolved.issym() else str(link)
                if resolved_path in visited or resolved_path not in members:
                    raise ApplicationError("runtime_archive_unsafe", "runtime link is cyclic or unresolved")
                visited.add(resolved_path)
                resolved = members[resolved_path]
            if not resolved.isreg():
                raise ApplicationError("runtime_archive_unsafe", "runtime links must resolve to regular files")
            if item is not resolved:
                total += resolved.size
                if total > maximum:
                    raise ApplicationError("runtime_archive_unsafe", "materialized links exceed the byte budget")
            stream = source.extractfile(resolved)
            if stream is None:
                raise ApplicationError("runtime_archive_unsafe", "runtime file has no body")
            with stream, open(target, "xb") as output:
                copied = 0
                while chunk := stream.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > resolved.size:
                        raise ApplicationError("runtime_archive_unsafe", "runtime file exceeds declared length")
                    output.write(chunk)
                if copied != resolved.size:
                    raise ApplicationError("runtime_archive_unsafe", "runtime file is truncated")
            target.chmod(0o700 if resolved.mode & 0o111 else 0o600)
    executable = destination / safe_member(artifact["executable"])
    if not executable.is_file() or executable.is_symlink():
        raise ApplicationError("runtime_archive_unsafe", "reviewed llama-server entry point is missing")
    executable.chmod(0o700)
    return executable


class ModelProcess:
    def __init__(self, installer, *, event=None):
        self.installer = installer
        self.event = event
        self.process = None
        self.pidfd = None
        self.model_fd = None
        self.workdir = None
        self.selection = None
        self.model_id = None
        self.last_error = None
        self.started_at = None
        self._lock = asyncio.Lock()

    def status(self):
        alive = self.process is not None and self.process.poll() is None
        if not alive and self.selection is not None and self.last_error is None:
            self.last_error = "model_process_exited"
            self._emit()
        return {"status": "ready" if alive and self.selection else "starting" if alive else
                "failed" if self.last_error else "stopped", "model_id": self.model_id,
                "pid": self.process.pid if alive else None,
                "endpoint": self.selection.endpoint if alive and self.selection else None,
                "error": self.last_error, "started_at": self.started_at}

    def _emit(self):
        if self.event:
            self.event(self.status())

    async def start(self, identifier, *, threads=None, timeout=120):
        async with self._lock:
            if self.process is not None and self.process.poll() is None:
                if self.model_id == identifier and self.selection:
                    return self.selection
                raise ApplicationError("model_process_busy", "stop the owned model before changing it")
            model = self.installer.artifact(identifier)
            try:
                backend = self.installer.artifact(model.get("runtime_id"))
            except ApplicationError as exc:
                raise ApplicationError("runtime_unavailable", "the reviewed CPU runtime is not installed") from exc
            if (model.get("kind") != "model" or backend.get("kind") != "runtime" or
                    backend.get("backend") != model.get("backend") or
                    model.get("architecture") not in backend.get("architectures", []) or
                    backend.get("platform") != f"{sys.platform}-{platform.machine()}"):
                raise ApplicationError("model_backend_incompatible", "model and installed backend are incompatible")
            await settled_io(self.installer.verify, identifier)
            await settled_io(self.installer.verify, backend["id"])
            self.stop_sync()
            self.model_id, self.last_error = identifier, None
            self.workdir = Path(f"/proc/self/fd/{self.installer.fd}") / (".runtime-" + uuid.uuid4().hex)
            self.workdir.mkdir(mode=0o700)
            try:
                archive_fd = self.installer._open(self.installer.name(backend), os.O_RDONLY)
                try:
                    await settled_io(self.installer._verify_fd, backend, archive_fd)
                    executable = await settled_io(
                        extract_runtime, f"/proc/self/fd/{archive_fd}", self.workdir, backend
                    )
                finally:
                    os.close(archive_fd)
                self.model_fd = self.installer._open(self.installer.name(model), os.O_RDONLY)
                # Verify the exact descriptor inherited by the model, rather than
                # trusting a filename that could have been replaced after lookup.
                await settled_io(self.installer._verify_fd, model, self.model_fd)
                # Pick a random loopback port. Bind is rechecked through authenticated
                # readiness; another listener cannot pass the random model alias/key.
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                api_key = secrets.token_urlsafe(48)
                key_file = self.workdir / "api-key.txt"
                key_file.write_text(api_key)
                key_file.chmod(0o600)
                alias = "ddp-" + secrets.token_hex(16)
                command = [
                    str(executable), "--model", f"/proc/self/fd/{self.model_fd}",
                    "--host", "127.0.0.1", "--port", str(port), "--alias", alias,
                    "--ctx-size", str(model.get("context_tokens", 8192)),
                    "--threads", str(max(1, min(threads or max(1, (os.cpu_count() or 2) // 2), 32))),
                    "--n-gpu-layers", "0", "--parallel", "1", "--api-key-file", str(key_file),
                    "--offline", "--no-webui",
                    "--chat-template-kwargs", '{"enable_thinking":false}', "--reasoning-budget", "0",
                ]
                environment = {
                    "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "LD_LIBRARY_PATH": os.pathsep.join(str(self.workdir / safe_member(d)) for d in backend.get("library_dirs", [])),
                }
                log = self.workdir / "model.log"
                with open(log, "wb") as output:
                    self.process = subprocess.Popen(
                        [sys.executable, str(Path(__file__).with_name("worker.py")), str(os.getpid()), *command],
                        pass_fds=(self.model_fd, self.installer.fd), stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, env=environment,
                        start_new_session=True,
                    )
                self.pidfd = os.pidfd_open(self.process.pid) if hasattr(os, "pidfd_open") else None
                self.started_at = time.time()
                self._emit()
                endpoint = f"http://127.0.0.1:{port}/v1"
                deadline = time.monotonic() + timeout
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
                    while True:
                        if self.process.poll() is not None:
                            message = log.read_bytes()[-16384:].lower()
                            memory_failure = any(marker in message for marker in (
                                b"out of memory", b"failed to allocate", b"cannot allocate memory",
                            ))
                            code = "out_of_memory" if memory_failure else "model_start_failed"
                            raise ApplicationError(code, "owned model exited before readiness")
                        if time.monotonic() >= deadline:
                            raise ApplicationError("model_start_timeout", "owned model did not become ready within budget")
                        try:
                            response = await client.get(endpoint + "/models", headers={"Authorization": "Bearer " + api_key})
                            if response.status_code == 200 and len(response.content) < 65536:
                                names = {entry.get("id") for entry in response.json().get("data", [])}
                                health = await client.get(f"http://127.0.0.1:{port}/health", headers={"Authorization": "Bearer " + api_key})
                                if alias in names and health.status_code == 200:
                                    self.selection = ModelSelection(endpoint, alias, "local", api_key, {
                                        "model_id": model["id"], "model_revision": model["version"],
                                        "model_sha256": model["sha256"], "runtime_id": backend["id"],
                                        "runtime_revision": backend["version"], "runtime_sha256": backend["sha256"],
                                        "device": "cpu", "context_tokens": model.get("context_tokens", 8192),
                                    })
                                    self._emit()
                                    return self.selection
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            pass
                        await asyncio.sleep(0.1)
            except BaseException as exc:
                self.last_error = getattr(exc, "code", "model_start_failed")
                self.stop_sync()
                self._emit()
                raise

    def stop_sync(self):
        process = self.process
        if process is not None and process.poll() is None:
            try:
                if self.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                    signal.pidfd_send_signal(self.pidfd, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass  # It may exit after poll(); owned descriptors still need cleanup.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if self.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                        signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for fd in (self.pidfd, self.model_fd):
            if fd is not None:
                os.close(fd)
        self.pidfd = self.model_fd = None
        self.process = self.selection = None
        if self.workdir is not None:
            shutil.rmtree(self.workdir)
            self.workdir = None

    async def stop(self):
        async with self._lock:
            await settled_io(self.stop_sync)
            self._emit()
            return self.status()
