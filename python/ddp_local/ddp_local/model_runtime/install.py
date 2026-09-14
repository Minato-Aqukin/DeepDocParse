"""Resumable, explicitly requested artifacts; incomplete downloads are never usable."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import json_bytes
from ddp_local.model_runtime.catalog import catalog, manifest_digest, validate_catalog


async def settled_io(function, *args):
    """Keep descriptors and temporary paths alive until a worker actually stops."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its thread. Closing/reusing an fd or
        # deleting its output here would race the still-running verifier/extractor.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class ModelInstaller:
    def __init__(self, directory, *, definitions=None, progress=None):
        self.definitions = definitions if definitions is not None else catalog()
        validate_catalog(self.definitions)
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(self.fd)
            raise ApplicationError("unsafe_path", "model directory must be owned by this user with mode 0700")
        self.progress = progress

    def close(self):
        os.close(self.fd)

    def artifact(self, identifier):
        for item in self.definitions["artifacts"]:
            if item["id"] == identifier:
                return item
        raise ApplicationError("model_not_found", "artifact is not in the reviewed catalog")

    def name(self, artifact):
        return artifact["id"] + "." + artifact["sha256"]

    def path(self, artifact):
        return f"/proc/self/fd/{self.fd}/{self.name(artifact)}"

    def _open(self, name, flags, mode=0o600):
        # Inspect before truncation: a crafted hard link must not truncate an
        # unrelated file even when the caller owns both paths.
        fd = os.open(name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            os.close(fd)
            raise ApplicationError("unsafe_path", "model artifact must be an owned regular file with one link")
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd

    def _read_json(self, name):
        try:
            fd = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            return None

    def _write_json(self, name, data):
        temp = ".state-" + uuid.uuid4().hex
        fd = self._open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json_bytes(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self, artifact):
        fd = self._open(self.name(artifact) + ".lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApplicationError("model_install_busy", "artifact is already being changed") from exc
            yield
        finally:
            os.close(fd)

    def status(self, identifier):
        artifact = self.artifact(identifier)
        name = self.name(artifact)
        info = {"manifest": artifact, "status": "not_installed", "downloaded_bytes": 0}
        state = self._read_json(name + ".state.json")
        try:
            fd = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            try:
                partial = self._open(name + ".part", os.O_RDONLY)
                info.update(status="partial", downloaded_bytes=os.fstat(partial).st_size)
                os.close(partial)
            except FileNotFoundError:
                pass
            if state and state.get("status") == "failed":
                info.update(status="failed", error=state.get("error"))
            return info
        try:
            valid = bool(state and state.get("status") == "installed" and
                         state.get("manifest_digest") == manifest_digest(artifact) and
                         state.get("file_signature") == signature(os.fstat(fd)))
            info.update(status="installed" if valid else "verification_required",
                        downloaded_bytes=os.fstat(fd).st_size)
            return info
        finally:
            os.close(fd)

    def _verify_fd(self, artifact, fd):
        before = os.fstat(fd)
        if before.st_size != artifact["bytes"]:
            raise ApplicationError("model_size_mismatch", "artifact size differs from its manifest")
        os.lseek(fd, 0, os.SEEK_SET)
        hasher, header = hashlib.sha256(), b""
        while chunk := os.read(fd, 1024 * 1024):
            if not header:
                header = chunk[:8]
            hasher.update(chunk)
        if signature(before) != signature(os.fstat(fd)):
            raise ApplicationError("model_input_changed", "artifact changed during verification")
        if hasher.hexdigest() != artifact["sha256"]:
            raise ApplicationError("model_digest_mismatch", "artifact SHA-256 differs from its manifest")
        if artifact.get("format") == "gguf-v3" and header != b"GGUF\x03\x00\x00\x00":
            raise ApplicationError("model_format_unsupported", "this profile requires GGUF version 3")
        return signature(before)

    def verify(self, identifier):
        artifact = self.artifact(identifier)
        with self.lock(artifact):
            try:
                fd = self._open(self.name(artifact), os.O_RDONLY)
            except FileNotFoundError as exc:
                raise ApplicationError("model_not_installed", "install the reviewed artifact before verification or execution") from exc
            try:
                info = self._verify_fd(artifact, fd)
                self._write_json(self.name(artifact) + ".state.json", {
                    "status": "installed", "manifest_digest": manifest_digest(artifact),
                    "file_signature": info, "verified_at": time.time(),
                })
            finally:
                os.close(fd)
        return self.status(identifier)

    def _publish(self, artifact, fd):
        self._verify_fd(artifact, fd)
        os.fsync(fd)
        name = self.name(artifact)
        os.replace(name + ".part", name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self._write_json(name + ".state.json", {
            "status": "installed", "manifest_digest": manifest_digest(artifact),
            "file_signature": signature(os.fstat(fd)), "verified_at": time.time(),
        })

    def import_file(self, identifier, filename):
        artifact = self.artifact(identifier)
        source = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise ApplicationError("unsafe_path", "selected artifact must be a regular file")
            before = signature(os.fstat(source))
            with self.lock(artifact):
                target = self._open(self.name(artifact) + ".part", os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                try:
                    total = 0
                    while chunk := os.read(source, 1024 * 1024):
                        total += len(chunk)
                        if total > artifact["bytes"]:
                            raise ApplicationError("model_size_mismatch", "selected file exceeds the manifest size")
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(target, view):]
                    if before != signature(os.fstat(source)):
                        raise ApplicationError("model_input_changed", "selected artifact changed during import")
                    self._publish(artifact, target)
                finally:
                    os.close(target)
        finally:
            os.close(source)
        return self.status(identifier)

    def _progress(self, artifact, total, *, error=None):
        data = {"status": "failed" if error else "downloading", "downloaded_bytes": total,
                "manifest_digest": manifest_digest(artifact), "error": error}
        self._write_json(self.name(artifact) + ".state.json", data)
        if self.progress:
            self.progress({"artifact_id": artifact["id"], "total_bytes": artifact["bytes"], **data})

    async def download(self, identifier, *, client=None):
        artifact = self.artifact(identifier)
        if client is None:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=60) as owned:
                return await self.download(identifier, client=owned)
        with self.lock(artifact):
            if self.status(identifier)["status"] == "installed":
                return self.status(identifier)
            fd = self._open(self.name(artifact) + ".part", os.O_RDWR | os.O_CREAT)
            total, last_progress = os.fstat(fd).st_size, 0
            try:
                if total > artifact["bytes"]:
                    raise ApplicationError("model_size_mismatch", "partial artifact exceeds its size budget")
                if total == artifact["bytes"]:
                    await settled_io(self._publish, artifact, fd)
                    return self.status(identifier)
                url = artifact["url"]
                for _ in range(6):
                    headers = {"Range": f"bytes={total}-"} if total else {}
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("location", ""))
                            parsed = urlsplit(url)
                            host = parsed.hostname or ""
                            origin = urlsplit(artifact["url"]).hostname
                            permitted = host == origin or host in {
                                "huggingface.co", "cdn-lfs.huggingface.co", "release-assets.githubusercontent.com",
                                "objects.githubusercontent.com",
                            } or host.endswith((".hf.co", ".huggingface.co"))
                            if parsed.scheme != "https" or not permitted or parsed.username or parsed.password:
                                raise ApplicationError("model_download_redirect", "download redirect is not HTTPS")
                            continue
                        if total and response.status_code == 200:
                            os.ftruncate(fd, 0)
                            total = 0  # Range ignored: restart, never append a second full file.
                        elif total:
                            position = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                            if (response.status_code != 206 or not position or
                                    int(position[1]) != total or int(position[3]) != artifact["bytes"] or
                                    not total <= int(position[2]) < artifact["bytes"]):
                                raise ApplicationError("model_download_range", "server did not resume the requested byte range")
                        elif not total and response.status_code != 200:
                            raise ApplicationError("model_download_failed", "publisher did not return the requested artifact")
                        os.lseek(fd, total, os.SEEK_SET)
                        self._progress(artifact, total)
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > artifact["bytes"]:
                                raise ApplicationError("model_size_mismatch", "publisher response exceeds the artifact budget")
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(fd, view):]
                            if time.monotonic() - last_progress >= 1:
                                os.fsync(fd)
                                self._progress(artifact, total)
                                last_progress = time.monotonic()
                        await settled_io(self._publish, artifact, fd)
                        return self.status(identifier)
                raise ApplicationError("model_download_redirect", "too many publisher redirects")
            except (httpx.HTTPError, ApplicationError) as exc:
                self._progress(artifact, total, error=getattr(exc, "code", "model_download_failed"))
                if isinstance(exc, ApplicationError):
                    raise
                raise ApplicationError("model_download_failed", "download interrupted; explicit retry resumes the partial file") from exc
            finally:
                os.close(fd)
