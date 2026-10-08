"""One owned llama-server process; no PATH lookup, shell commands, or remote fallback."""

import asyncio
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import httpx

from ddp_core.application.ports import ApplicationError
from ddp_local.providers import ModelSelection
from ddp_local.model_runtime.install import check_memory_floor, settled_io


def _remove_tree_at(parent_fd, name):
    """Recursively delete one pinned directory without following symlinks.

    Works entirely from dir-fds (openat/unlinkat/rmdir): the pinned models
    descriptor anchors `name`, an O_NOFOLLOW dir-fd anchors its children, so
    a symlink swapped in mid-sweep cannot redirect deletion elsewhere. An
    unreadable child is left in place and rmdir then surfaces the failure
    rather than leaving a silent half-sweep.
    """
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        try:
            entries = os.listdir(child)
        except OSError:
            entries = []
        for entry in entries:
            try:
                info = os.lstat(entry, dir_fd=child)
            except OSError:
                continue
            import stat as _stat

            if _stat.S_ISDIR(info.st_mode) and not _stat.S_ISLNK(info.st_mode):
                _remove_tree_at(child, entry)
            else:
                try:
                    os.unlink(entry, dir_fd=child)
                except OSError:
                    pass
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(child)


def sweep_orphan_runtimes(installer):
    """Remove crashed `.runtime-*` workdirs left by a killed predecessor.

    Only exact `.runtime-<32 hex>` directory names are touched, addressed
    through the pinned models dir-fd with lstat (never following symlinks).
    Runs once per ModelProcess construction so a crash/kill-9 between mkdir
    and stop_sync cannot leak extracted runtimes, api-key.txt or model.log.
    Returns the number of removed entries.
    """
    import errno as _errno

    removed = 0
    try:
        names = os.listdir(installer.fd)
    except OSError:
        return 0
    for name in names:
        if not re.fullmatch(r"\.runtime-[0-9a-f]{32}", name):
            continue
        try:
            info = os.lstat(name, dir_fd=installer.fd)
        except OSError:
            continue
        import stat as _stat

        if not _stat.S_ISDIR(info.st_mode) or _stat.S_ISLNK(info.st_mode):
            continue
        try:
            _remove_tree_at(installer.fd, name)
        except OSError as exc:
            if exc.errno in {_errno.ELOOP, _errno.ENOTDIR}:
                continue
            raise
        removed += 1
    return removed


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


def parse_libc_version(text):
    """Parse a dotted libc version into comparable integers; None when unknown."""
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.(\d+))?\s*", text or "")
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.groups() if part is not None)
    except ValueError:
        return None


def host_libc_version():
    """Host C library as (name, version); (None, None) when undetectable."""
    try:
        release = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, ValueError, OSError):
        release = None
    if isinstance(release, str):
        name, _, version = release.partition(" ")
        parsed = parse_libc_version(version)
        if name.strip().lower() == "glibc" and parsed is not None:
            return "glibc", parsed
    try:
        name, version = platform.libc_ver()
    except (OSError, ValueError):
        return None, None
    if not isinstance(name, str) or not isinstance(version, str):
        return None, None
    parsed = parse_libc_version(version)
    if name.strip().lower() != "glibc" or parsed is None:
        return None, None
    return "glibc", parsed


def check_backend_abi(backend):
    """Refuse a runtime whose declared C-library floor the host cannot meet.

    A 2026-10 real-host incident: the catalog's llama.cpp b10809 binaries need
    GLIBC_2.34, but an Ubuntu 20.04 host (glibc 2.31) passed the platform-only
    check and died as a generic model_start_failed. This runs before any
    verification, launch, or extraction so the failure stays explicit.
    """
    floor = backend.get("min_libc_version")
    if not floor:
        return
    if not isinstance(floor, str) or parse_libc_version(floor) is None:
        raise ApplicationError("model_manifest_invalid", "runtime C-library floor is not a version")
    required = parse_libc_version(floor)
    name, found = host_libc_version()
    if name != "glibc" or found is None:
        raise ApplicationError(
            "runtime_host_incompatible",
            f"runtime {backend.get('id')} needs glibc {floor}, "
            "but no glibc was detected on this host (for example musl); it cannot start here",
        )
    if found < required:
        have = ".".join(str(part) for part in found)
        raise ApplicationError(
            "runtime_host_incompatible",
            f"runtime {backend.get('id')} needs glibc {floor}, but this host provides glibc {have}",
        )


def tail_bytes(path, limit=16384):
    """Return the last `limit` bytes of a log without loading the whole file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, max(0, size - limit), os.SEEK_SET)
        pieces = []
        while chunk := os.read(fd, 65536):
            pieces.append(chunk)
        return b"".join(pieces)[-limit:]
    finally:
        os.close(fd)


LOG_CAP_BYTES = 8 * 1024 * 1024
LOG_READ_LIMIT = 2 * 1024 * 1024
LOG_DROPPED_MARKER = b"[ddp] earlier log bytes were dropped by the bounded writer\n"


class BoundedLogPump:
    """Stream a child pipe into model.log with a hard size cap.

    The pump owns one writer thread that appends stdout/stderr chunks to
    model.log. Once LOG_CAP_BYTES are stored it rotates the full file to
    model.log.prev, writes a dropped-bytes marker, and keeps the newest
    output. Existing tests drive write() directly; start()/join() bracket the
    child lifetime so no writer thread survives stop or a failed start.
    """

    def __init__(self, path, *, cap=LOG_CAP_BYTES):
        self.path = Path(path)
        self.cap = cap
        self.total = 0
        self.dropped = 0
        self._stop = threading.Event()
        self._thread = None

    def _rotate(self):
        previous = self.path.with_name(self.path.name + ".prev")
        try:
            if previous.exists():
                previous.unlink()
        except OSError:
            pass
        os.replace(self.path, previous)
        self.total = 0

    def write(self, chunk):
        """Append one child-output chunk; rotate when the cap is reached."""
        if not chunk:
            return self.total
        view = bytes(chunk)
        while view:
            room = self.cap - self.total
            if room <= 0:
                self._rotate()
                with open(self.path, "ab") as output:
                    output.write(LOG_DROPPED_MARKER)
                self.total = len(LOG_DROPPED_MARKER)
                self.dropped += 1
                room = self.cap - self.total
            piece, view = view[:room], view[room:]
            with open(self.path, "ab") as output:
                output.write(piece)
            self.total += len(piece)
        return self.total

    def start(self, stream):
        """Drain a binary pipe on a daemon thread until EOF or stop."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._stop.clear()

        def _drain():
            try:
                fd = stream.fileno()
            except (OSError, ValueError, AttributeError):
                fd = None
            try:
                while not self._stop.is_set():
                    if fd is None:
                        try:
                            piece = stream.read(65536)
                        except (OSError, ValueError):
                            break
                    else:
                        try:
                            piece = os.read(fd, 65536)
                        except OSError:
                            break
                    if not piece:
                        break
                    try:
                        self.write(piece)
                    except OSError:
                        break
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        self._thread = threading.Thread(target=_drain, name="ddp-model-log", daemon=True)
        self._thread.start()
        return self._thread

    def join(self, timeout=5):
        """Stop the writer thread; safe to call twice or before start."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return self.dropped


_BACKEND_LINE_START = "using device "


def _parse_gpu_devices(text):
    """Map Vulkan ids to full device names from llama.cpp startup lines.

    Device names may nest parentheses (e.g. `Fixture physical device
    (vendor)`), so the name runs to the balanced close paren rather than the
    first `)`. Only lines with a trailing `(pci-id) - <free> free` block
    count as device reports.
    """
    devices = {}
    for line in text.splitlines():
        marker = line.find(_BACKEND_LINE_START)
        if marker < 0:
            continue
        rest = line[marker + len(_BACKEND_LINE_START):]
        match = re.fullmatch(r"(Vulkan\d+) \((.*)", rest)
        if not match:
            continue
        vulkan, after_open = match.groups()
        depth, chars = 1, []
        for char in after_open:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            chars.append(char)
        else:
            continue
        tail = after_open[len("".join(chars)) + 1:]
        if not re.fullmatch(r" \([^()]*\) - .+", tail):
            continue
        devices[vulkan] = "".join(chars)
    return devices


def backend_evidence(log, backend):
    """Inspect startup output without loading an unbounded log into memory."""
    if backend["device"] == "cpu":
        return {"device": "cpu", "offloaded_layers": 0, "gpu_devices": []}
    text = tail_bytes(log, LOG_READ_LIMIT).decode("utf-8", errors="replace")
    devices = _parse_gpu_devices(text)
    buffers = {name: float(size) for name, size in
               re.findall(r"(Vulkan\d+) model buffer size\s*=\s*([0-9.]+) MiB", text)}
    offloads = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", text)
    if any(re.search(r"\b(llvmpipe|lavapipe|swiftshader|software|cpu)\b", name, re.I) for name in devices.values()):
        raise ApplicationError("gpu_device_unsupported", "selected Vulkan device is a software renderer; no CPU fallback was used")
    count, total = tuple(map(int, offloads[-1])) if offloads else (0, 0)
    if not devices or not 0 < count <= total or not any(buffers.get(name, 0) > 0 for name in devices):
        raise ApplicationError("gpu_offload_unverified", "physical GPU offload was not observed; select CPU explicitly to use it")
    return {"device": "gpu", "gpu_api": "vulkan", "gpu_devices": list(devices.values()),
            "offloaded_layers": count, "total_layers": total,
            "gpu_model_buffer_mib": sum(buffers.get(name, 0) for name in devices)}


class _ExtractAbandoned(Exception):
    """Internal signal: the extractor thread was abandoned after a stop.

    The worker thread cannot be cancelled, so a stop during extraction
    detaches it; the background reaper owns the archive descriptor and the
    workdir until the thread lands. Never escapes ModelProcess.
    """


class ModelProcess:
    def __init__(self, installer, *, event=None):
        self.installer = installer
        self.event = event
        self.process = None
        self.pidfd = None
        self.model_fd = None
        self.workdir = None
        self.log_pump = None
        self.selection = None
        self.model_id = None
        self.runtime_id = None
        self.last_error = None
        self.started_at = None
        self._lock = asyncio.Lock()
        self._proc_lock = threading.Lock()
        self._starting = False
        self._stop_requested = asyncio.Event()
        # Background joins for start workers abandoned after a stop; tasks
        # remove themselves on completion so the set cannot grow.
        self._reap_tasks = set()
        sweep_orphan_runtimes(installer)

    def status(self):
        alive = self.process is not None and self.process.poll() is None
        if not alive and self.selection is not None and self.last_error is None:
            self.last_error = "model_process_exited"
            self._emit()
        return {"status": "ready" if alive and self.selection else "starting" if alive else
                "failed" if self.last_error else "stopped", "model_id": self.model_id, "runtime_id": self.runtime_id,
                "pid": self.process.pid if alive else None,
                "endpoint": self.selection.endpoint if alive and self.selection else None,
                "backend": self.selection.provenance if alive and self.selection else None,
                "error": self.last_error, "started_at": self.started_at}

    def _emit(self):
        if self.event:
            self.event(self.status())

    async def _cancellable_extract(self, archive_fd, workdir, backend):
        """Await extraction, but abandon it promptly when stop is requested.

        The extractor runs on a worker thread that cannot be cancelled, so
        racing stop against completion lets stop() return while the thread
        is still running. On abandon a background reaper takes the archive
        descriptor and the workdir and cleans them up when the thread
        lands; the caller must detach both and report cancellation.
        """
        worker = asyncio.create_task(
            settled_io(extract_runtime, f"/proc/self/fd/{archive_fd}", workdir, backend)
        )
        stop_wait = asyncio.create_task(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                {worker, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            stop_wait.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await stop_wait
            except asyncio.CancelledError:
                pass
            raise
        if worker in done:
            stop_wait.cancel()
            try:
                await stop_wait
            except asyncio.CancelledError:
                pass
            return await worker
        reap = asyncio.create_task(
            self._reap_abandoned_extract(worker, archive_fd, workdir)
        )
        self._reap_tasks.add(reap)
        reap.add_done_callback(self._reap_tasks.discard)
        raise _ExtractAbandoned()

    async def _reap_abandoned_extract(self, worker, archive_fd, workdir):
        """Join an abandoned extractor, then release its descriptor and workdir.

        Runs detached after a stop: the worker still references the archive
        descriptor through its /proc path and writes into the workdir, so
        neither may be closed or removed until the thread lands.
        """
        try:
            await worker
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            try:
                os.close(archive_fd)
            except OSError:
                pass
            shutil.rmtree(workdir, ignore_errors=True)

    async def start(self, identifier, *, runtime_id=None, threads=None, timeout=120):
        async with self._lock:
            model = self.installer.artifact(identifier)
            wanted = runtime_id or model.get("runtime_id")
            if wanted not in model.get("runtime_ids", [model.get("runtime_id")]):
                raise ApplicationError("model_backend_incompatible", "this runtime is not a reviewed choice for the model")
            if self._starting or (self.process is not None and self.process.poll() is None):
                if (self.model_id == identifier and self.runtime_id == wanted
                        and self.selection is not None and not self._starting):
                    return self.selection
                raise ApplicationError("model_process_busy", "stop the owned model before changing it")
            try:
                backend = self.installer.artifact(wanted)
            except ApplicationError as exc:
                raise ApplicationError("runtime_unavailable", "the selected reviewed runtime is not installed") from exc
            if (model.get("kind") != "model" or backend.get("kind") != "runtime" or
                    backend.get("backend") != model.get("backend") or
                    model.get("architecture") not in backend.get("architectures", []) or
                    backend.get("platform") != f"{sys.platform}-{platform.machine()}"):
                raise ApplicationError("model_backend_incompatible", "model and installed backend are incompatible")
            check_backend_abi(backend)
            check_memory_floor(model)
            self._starting = True
            self._stop_requested.clear()
            plan = (identifier, wanted, dict(model), dict(backend), threads, timeout)
        # Everything below runs WITHOUT the start lock so stop() can interrupt
        # a slow verify/extract/readiness wait. The _starting reservation keeps
        # a second start() returning model_process_busy; the poll loop snapshots
        # the child under the brief proc mutex so a concurrent stop clears it.
        try:
            await settled_io(self.installer.verify, identifier)
            await settled_io(self.installer.verify, plan[3]["id"])
            if self._stop_requested.is_set():
                raise ApplicationError("model_start_cancelled", "model start was cancelled")
            await settled_io(self.stop_sync)
            workdir = Path(f"/proc/self/fd/{self.installer.fd}") / (".runtime-" + uuid.uuid4().hex)
            workdir.mkdir(mode=0o700)
            model_fd = None
            pump = None
            child_launched = False
            workdir_live = True
            try:
                archive_fd = self.installer._open(self.installer.name(plan[3]), os.O_RDONLY)
                detached = False
                try:
                    await settled_io(self.installer._verify_fd, plan[3], archive_fd)
                    try:
                        executable = await self._cancellable_extract(archive_fd, workdir, plan[3])
                    except _ExtractAbandoned:
                        # The extractor thread still runs; it owns the archive
                        # descriptor and the workdir until the reaper cleans
                        # them up, so detach both from the shared cleanup below.
                        detached = True
                        workdir_live = False
                        raise ApplicationError("model_start_cancelled", "model start was cancelled")
                finally:
                    if not detached:
                        os.close(archive_fd)
                if self._stop_requested.is_set():
                    raise ApplicationError("model_start_cancelled", "model start was cancelled")
                model_fd = self.installer._open(self.installer.name(plan[2]), os.O_RDONLY)
                try:
                    # Verify the exact descriptor inherited by the model, rather than
                    # trusting a filename that could have been replaced after lookup.
                    await settled_io(self.installer._verify_fd, plan[2], model_fd)
                except BaseException:
                    os.close(model_fd)
                    model_fd = None
                    raise
                # Pick a random loopback port. Bind is rechecked through authenticated
                # readiness; another listener cannot pass the random model alias/key.
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                api_key = secrets.token_urlsafe(48)
                key_file = workdir / "api-key.txt"
                key_file.write_text(api_key)
                key_file.chmod(0o600)
                alias = "ddp-" + secrets.token_hex(16)
                command = [
                    str(executable), "--model", f"/proc/self/fd/{model_fd}",
                    "--host", "127.0.0.1", "--port", str(port), "--alias", alias,
                    "--ctx-size", str(plan[2].get("context_tokens", 8192)),
                    "--threads", str(max(1, min(plan[4] or max(1, (os.cpu_count() or 2) // 2), 32))),
                    "--n-gpu-layers", str(plan[3].get("default_gpu_layers", 0)) if plan[3]["device"] == "gpu" else "0",
                    "--parallel", "1", "--api-key-file", str(key_file),
                    "--offline", "--no-webui", "--fit", "off",
                    "--verbosity", str(plan[3].get("default_log_verbosity", 3)),
                    "--chat-template-kwargs", '{"enable_thinking":false}', "--reasoning-budget", "0",
                ]
                environment = {
                    "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "LD_LIBRARY_PATH": os.pathsep.join(str(workdir / safe_member(d)) for d in plan[3].get("library_dirs", [])),
                }
                if plan[3]["device"] == "gpu":
                    environment["DISABLE_LSFGVK"] = "1"
                log = workdir / "model.log"
                log.touch(mode=0o600, exist_ok=True)
                try:
                    os.chmod(log, 0o600)
                except OSError:
                    pass
                pump = BoundedLogPump(log)
                try:
                    child = subprocess.Popen(
                        [sys.executable, str(Path(__file__).with_name("worker.py")), str(os.getpid()), *command],
                        pass_fds=(model_fd, self.installer.fd), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment,
                        start_new_session=True,
                    )
                except BaseException:
                    pump.join()
                    raise
                child_launched = True
                pump.start(child.stdout)
                pidfd = os.pidfd_open(child.pid) if hasattr(os, "pidfd_open") else None
                with self._proc_lock:
                    self.process = child
                    self.pidfd = pidfd
                    self.model_fd = model_fd
                    self.workdir = workdir
                    self.log_pump = pump
                    self.model_id, self.runtime_id = plan[0], plan[1]
                    self.selection, self.last_error = None, None
                    self.started_at = time.time()
                model_fd = None
                workdir = None
                workdir_live = False
                pump = None
                self._emit()
                endpoint = f"http://127.0.0.1:{port}/v1"
                deadline = time.monotonic() + plan[5]
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
                    while True:
                        if self._stop_requested.is_set():
                            raise ApplicationError("model_start_cancelled", "model start was cancelled")
                        with self._proc_lock:
                            live = self.process
                            current_log = self.workdir / "model.log" if self.workdir is not None else log
                        if live is None:
                            raise ApplicationError("model_start_cancelled", "model start was cancelled")
                        if live.poll() is not None:
                            message = tail_bytes(current_log)[-16384:].lower()
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
                                    observed = backend_evidence(current_log, plan[3])
                                    selection = ModelSelection(endpoint, alias, "local", api_key, {
                                        "model_id": plan[2]["id"], "model_revision": plan[2]["version"],
                                        "model_sha256": plan[2]["sha256"], "runtime_id": plan[3]["id"],
                                        "runtime_revision": plan[3]["version"], "runtime_sha256": plan[3]["sha256"],
                                        **observed, "context_tokens": plan[2].get("context_tokens", 8192),
                                    })
                                    async with self._lock:
                                        if self._stop_requested.is_set():
                                            raise ApplicationError("model_start_cancelled", "model start was cancelled")
                                        self.selection = selection
                                        self._emit()
                                        return selection
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            pass
                        await asyncio.sleep(0.1)
            except BaseException:
                if child_launched:
                    # The child is published under the proc mutex; let the shared
                    # cleanup below reap it so the pump thread always joins.
                    raise
                if pump is not None:
                    pump.join()
                if model_fd is not None:
                    os.close(model_fd)
                if workdir is not None and workdir_live:
                    shutil.rmtree(workdir, ignore_errors=True)
                raise
        except BaseException as exc:
            if getattr(exc, "code", None) == "model_start_cancelled":
                # A stop-requested start is not a failure: report stopped.
                self.last_error = None
            else:
                self.last_error = getattr(exc, "code", "model_start_failed")
            await settled_io(self.stop_sync)
            self._emit()
            raise
        finally:
            async with self._lock:
                self._starting = False

    def stop_sync(self):
        with self._proc_lock:
            process, pidfd, model_fd, workdir, pump = (
                self.process, self.pidfd, self.model_fd, self.workdir, self.log_pump,
            )
            self.process = self.selection = None
            self.pidfd = self.model_fd = None
            self.workdir = self.log_pump = None
        if process is not None and process.poll() is None:
            try:
                if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass  # It may exit after poll(); owned descriptors still need cleanup.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        if pump is not None:
            pump.join()
        for fd in (pidfd, model_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    async def stop(self):
        # Never wait on the start lock: set the cancel event, terminate the
        # known child under the brief proc mutex, and return. A start blocked
        # in verify/extract/poll observes _stop_requested (or a cleared
        # process handle) and raises model_start_cancelled promptly.
        self._stop_requested.set()
        await settled_io(self.stop_sync)
        self._emit()
        return self.status()
