"""Immutable content-addressed files; every caller supplies bytes or an explicit file handle."""

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path

from ddp_core.application.ports import ApplicationError

MAX_INPUT = 32 * 1024 * 1024


class FileBlobStore:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    def _key(self, key):
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key):
            raise ApplicationError("unsafe_path", "blob keys must be SHA-256 identifiers")
        return key

    def path(self, key: str) -> str:
        # Linux profile: the pinned directory descriptor survives path/symlink replacement.
        return f"/proc/self/fd/{self.fd}/{self._key(key)}"

    def read(self, key: str, maximum: int) -> bytes:
        fd = os.open(self._key(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ApplicationError("unsafe_path", "blob is not a regular file")
            chunks, total, hasher = [], 0, hashlib.sha256()
            while data := os.read(fd, min(65536, maximum + 1 - total)):
                total += len(data)
                if total > maximum:
                    raise ApplicationError("input_too_large", "blob exceeds the read budget")
                chunks.append(data)
                hasher.update(data)
            if hasher.hexdigest() != key:
                raise ApplicationError("blob_corrupt", "immutable blob digest does not match")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def put_stream(self, stream, *, maximum=MAX_INPUT) -> tuple[str, int]:
        name = ".pending-" + uuid.uuid4().hex
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )
        hasher, size = hashlib.sha256(), 0
        try:
            while data := stream.read(65536):
                size += len(data)
                if size > maximum:
                    raise ApplicationError(
                        "input_too_large", "input exceeds configured byte budget"
                    )
                hasher.update(data)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            os.fsync(fd)
            key = hasher.hexdigest()
            try:
                os.link(name, key, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            except FileExistsError:
                self.read(key, maximum)  # Existing hash alone is not proof of intact bytes.
            os.fsync(self.fd)
            return key, size
        finally:
            os.close(fd)
            os.unlink(name, dir_fd=self.fd)

    def snapshot(self, filename: str) -> tuple[str, int]:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ApplicationError("unsafe_path", "selected input must be a regular file")
            with os.fdopen(os.dup(fd), "rb") as stream:
                key, size = self.put_stream(stream)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or size != before.st_size:
                raise ApplicationError(
                    "input_changed", "input changed during snapshot; retry explicitly"
                )
            return key, size
        finally:
            os.close(fd)

    def write(self, content: bytes) -> str:
        import io

        return self.put_stream(io.BytesIO(content), maximum=128 * 1024 * 1024)[0]
