"""Local file-compute adapter: approved local bytes -> center parse -> verified return.

This is the owned local `remote_compute` module. It never invents a plan,
endpoint, credential, path or URL: every remote call goes through the existing
approved-plan dispatch path (`federation_dispatch`) with the reviewed center
endpoint, and every local byte comes from the trusted blob snapshot resolver.

Flow (mirrors the center `/api/v1/remote-compute` lifecycle):

1. `prepare_file_compute` snapshots one selected local file (fixed stat
   before/after, bounded read) and proposes a `corpus.parse` file plan with a
   pinned input manifest (digest + size). No byte leaves the machine here.
2. The caller approves exploration/execution through the existing consent
   ledger and two-phase native dialog; dispatch uploads the fixed snapshot as
   a `temporary_compute` input bound to the waiting center record.
3. The center verifies the full digest before enqueueing its existing real
   parse queue; metadata pre-checks never impersonate `content_verified`.
4. `fetch_and_import` downloads the fixed manifest output in bounded resume
   chunks, rehashes every byte, refuses hash mismatch without importing, and
   atomically imports the verified Bundle through `runtime.import_bundle`.
5. `confirm` acks only the verified manifest digest; lost/duplicate acks
   replay with the same key. TTL/cancel/failure/ack cleanup is owned by the
   center with reference-safe GC; the local ledger keeps the mirror visible.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat

from ddp_core.application.plans import content_digest, reject
from ddp_core.bundle import MAX_ARCHIVE, read_bundle

MAX_FILE_BYTES = 32 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PARTIAL_NAME_PATTERN = re.compile(r"delivery-[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\.[a-f0-9]{64}\.part")
PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MANIFEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}\Z")


def snapshot_bytes(read_file, size_hint=None) -> tuple[bytes, str, int]:
    """Read one fixed snapshot through the caller's trusted file reader.

    `read_file` must return the exact bytes of a file the host already
    snapshotted (fixed stat, bounded size, `%PDF-` checked by the host). This
    adapter only rehashes and bounds what it receives; it never opens a path.
    """
    data = read_file()
    if not isinstance(data, (bytes, bytearray)):
        reject("input_changed", "file snapshot must be concrete bytes")
    data = bytes(data)
    if size_hint is not None and len(data) != size_hint:
        reject("input_changed", "file changed during snapshot; retry explicitly")
    if not 1 <= len(data) <= MAX_FILE_BYTES:
        reject("input_too_large", "file exceeds the file-compute input budget")
    digest = hashlib.sha256(data).hexdigest()
    return data, "sha256:" + digest, len(data)


def file_plan_request(*, center, filename, digest, size_bytes, retention,
                      valid_seconds):
    """Typed `corpus.parse` file-plan request for the fixed template.

    The renderer/host names only the paired center, a filename label, and the
    pinned digest/size. The template (not the caller) fixes the operation,
    edges, budget and retention; the ledger rechecks the snapshot on approval
    and dispatch but releases no original byte until the execution phase.
    """
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        reject("invalid_plan", "a plain filename is required")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        reject("input_changed", "fixed input digest is required")
    if not isinstance(size_bytes, int) or size_bytes < 1:
        reject("input_changed", "fixed input size is required")
    if retention not in ("temporary", "task_pinned"):
        reject("policy_denied", "file compute keeps only temporary retention")
    return {
        "center": dict(center),
        "operation": "corpus.parse",
        "filename": filename,
        "inputs": [{"digest": digest, "size_bytes": size_bytes}],
        "retention": retention,
        "valid_seconds": valid_seconds,
    }


def verify_output_bytes(data: bytes, manifest_digest: str) -> bytes:
    """Rehash downloaded output bytes; mismatch refuses import atomically."""
    if not isinstance(data, (bytes, bytearray)):
        reject("result_manifest_mismatch", "delivery body must be concrete bytes")
    data = bytes(data)
    if not data or len(data) > MAX_ARCHIVE:
        reject("result_unavailable", "delivery body is missing or over budget")
    actual = content_digest(data)
    if actual != manifest_digest:
        reject("result_manifest_mismatch",
               "downloaded bytes differ from the fixed manifest")
    return data


def import_verified_bundle(runtime, data: bytes, *, operation_key: str):
    """Atomically import a verified Bundle into the local workspace.

    The caller already rehashed `data` against the fixed manifest digest.
    `read_bundle` validates paths/symlinks/sizes/schema before anything is
    stored; `runtime.import_bundle` allocates fresh local keys and never
    reuses the remote identity as a local key.
    """
    verified = read_bundle(io.BytesIO(data))
    if verified.source["original"] != "present":
        reject("source_missing", "remote bundle carries no original bytes")
    return runtime.import_bundle(io.BytesIO(data), operation_key=operation_key)


def partial_identity(plan_id, manifest_digest):
    """Workspace-owned partial name; renderer can never supply a native path."""
    if not isinstance(plan_id, str) or PLAN_ID_PATTERN.fullmatch(plan_id) is None:
        reject("invalid_plan", "plan id must be a bounded identifier")
    if not isinstance(manifest_digest, str) or MANIFEST_PATTERN.fullmatch(manifest_digest) is None:
        reject("invalid_plan", "manifest digest must be a fixed sha256 value")
    return "delivery-" + plan_id + "." + manifest_digest.removeprefix("sha256:") + ".part"


def partial_path(runtime, name):
    """Pinned workspace-owned partial path; no caller path authority."""
    if not isinstance(name, str) or PARTIAL_NAME_PATTERN.fullmatch(name) is None:
        reject("unsafe_path", "delivery partial name is not a fixed transfer identity")
    directory = workspace_directory(runtime) / "delivery-partials"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            reject("unsafe_path", "delivery partial directory must be private")
        target = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    finally:
        os.close(fd)
    info = os.fstat(target)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        os.close(target)
        reject("unsafe_path", "delivery partial must be an owned regular file with one link")
    return target


def partial_size(target):
    return os.fstat(target).st_size


def truncate_partial(target, size):
    if type(size) is not int or size < 0:
        reject("invalid_plan", "partial offset must be a non-negative integer")
    os.ftruncate(target, size)
    os.fsync(target)


def append_complete_chunk(target, chunk, *, expected_offset, manifest_digest):
    """Persist one validated chunk; only durable complete 1MiB prefixes survive."""
    if not isinstance(chunk, (bytes, bytearray)) or not chunk:
        reject("result_manifest_mismatch", "delivery chunk must be concrete bytes")
    if type(expected_offset) is not int or expected_offset < 0:
        reject("invalid_plan", "partial offset must be a non-negative integer")
    os.lseek(target, 0, os.SEEK_END)
    actual = os.fstat(target).st_size
    # Crash divergence fails closed: a longer file means an unwritten tail was
    # never fsynced as complete; truncate back to the durable offset and let
    # the next Range re-fetch the missing tail instead of keeping a hole.
    if actual != expected_offset:
        os.ftruncate(target, min(actual, expected_offset))
        os.fsync(target)
        actual = os.fstat(target).st_size
        if actual != expected_offset:
            reject("result_manifest_mismatch", "partial offset diverged after a crash; retry the missing range")
    view = memoryview(bytes(chunk))
    while view:
        view = view[os.write(target, view):]
    os.fsync(target)
    return os.fstat(target).st_size


def discard_partial(runtime, name):
    """Remove only this transfer's partial; never touch imported versions."""
    if not isinstance(name, str) or PARTIAL_NAME_PATTERN.fullmatch(name) is None:
        reject("unsafe_path", "delivery partial name is not a fixed transfer identity")
    directory = workspace_directory(runtime) / "delivery-partials"
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        else:
            os.fsync(fd)
    finally:
        os.close(fd)


def workspace_directory(runtime):
    row = runtime.store.db.execute("PRAGMA database_list").fetchone()
    found = row[2] if row is not None and len(row) > 2 else ""
    if not found:
        reject("not_found", "workspace database has no filesystem path")
    from pathlib import Path as _Path
    return _Path(found).parent
