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

from ddp_core.application.plans import content_digest, reject
from ddp_core.bundle import MAX_ARCHIVE, read_bundle

MAX_FILE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


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
