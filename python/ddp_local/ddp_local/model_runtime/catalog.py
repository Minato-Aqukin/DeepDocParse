"""Pinned publisher artifacts; request bodies cannot supply manifests or download URLs."""

import hashlib
import json
import re
from importlib.resources import files
from urllib.parse import urlsplit

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import json_bytes


def catalog():
    value = json.loads(files(__package__).joinpath("catalog.json").read_text())
    validate_catalog(value)
    return value


def validate_catalog(value):
    if value.get("schema") != "ddp-model-catalog/1" or not isinstance(value.get("revision"), str):
        raise ApplicationError("model_manifest_unsupported", "unsupported model catalog")
    seen = set()
    for artifact in value.get("artifacts", []):
        identifier = artifact.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", identifier) or identifier in seen:
            raise ApplicationError("model_manifest_invalid", "invalid or duplicate artifact identity")
        seen.add(identifier)
        filename = artifact.get("filename", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", filename):
            raise ApplicationError("model_manifest_invalid", "artifact filename must be plain")
        if not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", "")):
            raise ApplicationError("model_manifest_invalid", "artifact needs a fixed SHA-256")
        if type(artifact.get("bytes")) is not int or not 0 < artifact["bytes"] <= 16 * 1024**3:
            raise ApplicationError("model_manifest_invalid", "artifact size is outside the budget")
        for field in ("url", "license_url"):
            url = urlsplit(artifact.get(field, ""))
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                raise ApplicationError("model_manifest_invalid", "publisher and license URLs must use HTTPS")
        if artifact.get("kind") not in {"model", "runtime"} or not artifact.get("version"):
            raise ApplicationError("model_manifest_invalid", "artifact kind and version are required")
        if artifact.get("format") != ("gguf-v3" if artifact["kind"] == "model" else "tar-gz"):
            raise ApplicationError("model_format_unsupported", "artifact format is not supported by this catalog")
        if artifact.get("license") not in {"Apache-2.0", "MIT"}:
            raise ApplicationError("model_license_unsupported", "artifact license requires review")
        if artifact.get("backend") != "llama.cpp" or artifact.get("device") != "cpu":
            raise ApplicationError("model_backend_unsupported", "only the declared CPU backend is available")


def manifest_digest(artifact):
    return hashlib.sha256(json_bytes(artifact)).hexdigest()
