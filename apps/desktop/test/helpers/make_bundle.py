"""Build one minimal valid DDP-Bundle v1 for the WSL validator test."""

import hashlib
import json
import sys

from ddp_core.bundle import build_bundle

source = {
    "origin_node_id": "node-origin",
    "authority_node_id": "node-authority",
    "resource_id": "resource-one",
    "source_version_id": "version-one",
    "source_digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
    "parse_revision": None,
    "filename": "sample.pdf",
    "mime": "application/pdf",
    "original": "missing",
    "missing_reason": "fixture stores no original bytes",
    "uploader_ref": None,
    "policy_revision": "policy-one",
}
files = {
    "layout.json": json.dumps({
        "schema": "ddp-bundle-layout/1",
        "state": "missing",
        "layout": None,
        "reason": "fixture stores no layout",
    }, separators=(",", ":")).encode(),
    "evidence.json": b"[]",
    "provenance.json": b"[]",
}

sys.stdout.buffer.write(build_bundle(source, files))
