"""DDP-Bundle v1 validation, without database, network or filesystem extraction.

No archive member becomes usable until the entire archive and its evidence graph
are validated. These same functions serve the centre and the offline runtime.
"""

import hashlib
import io
import json
import math
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

SCHEMA = "ddp-bundle/1"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_ENTRIES = 32
MAX_RATIO = 200
FILES = {
    "source.bin": "original",
    "layout.json": "layout",
    "evidence.json": "evidence",
    "provenance.json": "provenance",
}
IDENTITY = (
    "origin_node_id",
    "authority_node_id",
    "resource_id",
    "source_version_id",
    "source_digest",
    "parse_revision",
)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
NODE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")


class BundleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fail(message: str, code: str = "bundle_invalid") -> None:
    raise BundleError(code, message)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            fail("duplicate JSON key")
        out[key] = value
    return out


def parse_json(data: bytes):
    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            fail("non-finite JSON number")
        return value

    try:
        value = json.loads(
            data,
            object_pairs_hook=_pairs,
            parse_float=finite_float,
            parse_constant=lambda _: fail("non-finite JSON number"),
        )
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 64 or count > 1000000:
                fail("JSON structure exceeds limit", "bundle_too_large")
            if isinstance(item, str) and re.search("[\ud800-\udfff]", item):
                fail("invalid Unicode scalar")
            if isinstance(item, dict):
                pending.extend((part, depth + 1) for pair in item.items() for part in pair)
            elif isinstance(item, list):
                pending.extend((part, depth + 1) for part in item)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("bundle_invalid", "invalid JSON") from exc


def _object(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        fail("unsupported or missing schema fields", "bundle_schema_unsupported")


def _string(value):
    return isinstance(value, str) and 0 < len(value) <= 512


def _digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or len(name) > 255
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or path.is_absolute()
        or any(p in (".", "..", "") for p in name.split("/"))
        or str(path) != name
    ):
        fail("unsafe archive path", "bundle_unsafe_path")


@dataclass(frozen=True)
class VerifiedBundle:
    manifest: dict
    files: dict[str, bytes]
    evidence: list[dict]
    layout: dict

    @property
    def source(self) -> dict:
        return self.manifest["source"]


def _validate_source(source):
    _object(
        source,
        (
            *IDENTITY,
            "filename",
            "mime",
            "original",
            "missing_reason",
            "uploader_ref",
            "policy_revision",
        ),
    )
    for key in ("origin_node_id", "authority_node_id"):
        if not isinstance(source[key], str) or not NODE_ID.fullmatch(source[key]):
            fail("invalid node identity")
    for key in ("resource_id", "source_version_id", "filename", "mime", "policy_revision"):
        if not _string(source[key]):
            fail("invalid source metadata")
    if len(source["filename"]) > 255 or len(source["mime"]) > 128:
        fail("source metadata exceeds storage limits")
    if not _digest(source["source_digest"]):
        fail("invalid source digest")
    if source["parse_revision"] is not None and not _string(source["parse_revision"]):
        fail("invalid parse revision")
    if source["uploader_ref"] is not None and not _string(source["uploader_ref"]):
        fail("invalid uploader reference")
    if source["original"] not in ("present", "missing"):
        fail("unknown original availability")
    if source["original"] == "missing" and not _string(source["missing_reason"]):
        fail("missing original requires a reason")
    if source["original"] == "present" and source["missing_reason"] is not None:
        fail("present original cannot have missing reason")


def _validate_evidence(records, source):
    if not isinstance(records, list) or len(records) > 100000:
        fail("invalid evidence list")
    identities = {}
    for item in records:
        _object(item, ("evidence", "excerpt"))
        e = item["evidence"]
        _object(
            e,
            (
                "schema",
                "evidence_id",
                *IDENTITY,
                "excerpt_digest",
                "locator",
                "source_type",
                "policy_revision",
            ),
            ("derived_from", "uploader_ref", "retrieval_receipt_ref", "block_type"),
        )
        if e["schema"] != "ddp-evidence/1#FederatedEvidence" or not _string(e["evidence_id"]):
            fail("unsupported evidence schema", "bundle_schema_unsupported")
        if any(e[k] != source[k] for k in IDENTITY) or source["parse_revision"] is None:
            fail("evidence does not belong to source version")
        if (
            not isinstance(item["excerpt"], str)
            or digest(item["excerpt"].encode()) != e["excerpt_digest"]
        ):
            fail("excerpt digest mismatch", "bundle_digest_mismatch")
        if e["policy_revision"] != source["policy_revision"]:
            fail("evidence policy revision mismatch")
        if e["evidence_id"] in identities:
            fail("duplicate evidence identity")
        loc = e["locator"]
        _object(
            loc, ("kind", "physical_page_index", "seq"), ("bbox", "page_size", "printed_page_label")
        )
        if source["mime"] != "application/pdf" or loc["kind"] not in ("page_block", "table_cell"):
            fail("unsupported non-PDF locator", "bundle_schema_unsupported")
        if any(type(loc[k]) is not int or loc[k] < 0 for k in ("physical_page_index", "seq")):
            fail("invalid evidence page or sequence")
        if loc.get("bbox") is not None:
            bbox = loc["bbox"]
            size = loc.get("page_size")
            if not isinstance(bbox, list) or len(bbox) != 4 or not all(_number(v) for v in bbox):
                fail("invalid bbox")
            _object(size, ("width", "height"), ("rotation",))
            if any(not _number(size[k]) or size[k] <= 0 for k in ("width", "height")) or size.get(
                "rotation", 0
            ) not in (0, 90, 180, 270):
                fail("invalid page size")
            if not (
                0 <= bbox[0] <= bbox[2] <= size["width"]
                and 0 <= bbox[1] <= bbox[3] <= size["height"]
            ):
                fail("bbox outside page")
        if e["source_type"] == "source":
            if e.get("derived_from") is not None:
                fail("source cannot be derived")
        elif e["source_type"] == "generated":
            if not _string(e.get("derived_from")):
                fail("generated evidence needs a source")
        else:
            fail("unsupported evidence source type")
        identities[e["evidence_id"]] = e
    resolved = set()
    for e in identities.values():
        seen = {e["evidence_id"]}
        current = e
        while current.get("derived_from") and current["evidence_id"] not in resolved:
            parent = current["derived_from"]
            if parent not in identities or parent in seen:
                fail("unresolved or cyclic evidence provenance")
            seen.add(parent)
            current = identities[parent]
        resolved.update(seen)


def validate_parts(manifest: dict, files: dict[str, bytes]) -> VerifiedBundle:
    _object(manifest, ("schema", "required_features", "source", "files"))
    if manifest["schema"] != SCHEMA or manifest["required_features"] != []:
        fail("unsupported bundle schema or required capability", "bundle_schema_unsupported")
    source = manifest["source"]
    _validate_source(source)
    entries = manifest["files"]
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        fail("invalid file list")
    listed = set()
    total = 0
    for entry in entries:
        _object(entry, ("path", "size", "digest", "role"))
        name = entry["path"]
        if not isinstance(name, str):
            fail("invalid path")
        safe_member(name)
        if name not in FILES or entry["role"] != FILES[name] or name in listed:
            fail("unknown or duplicate bundle member")
        listed.add(name)
        if type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE:
            fail("member exceeds limit", "bundle_too_large")
        total += entry["size"]
        if total > MAX_EXPANDED:
            fail("expanded bundle exceeds limit", "bundle_too_large")
        if name not in files or len(files[name]) != entry["size"]:
            fail("missing member or incorrect size")
        if not _digest(entry["digest"]) or digest(files[name]) != entry["digest"]:
            fail("file digest mismatch", "bundle_digest_mismatch")
    expected = set(FILES) - ({"source.bin"} if source["original"] == "missing" else set())
    if listed != expected or set(files) != listed:
        fail("required member missing or undeclared member")
    if source["original"] == "present" and digest(files["source.bin"]) != source["source_digest"]:
        fail("source digest mismatch", "bundle_digest_mismatch")
    layout = parse_json(files["layout.json"])
    _object(layout, ("schema", "state", "layout", "reason"))
    if layout["schema"] != "ddp-bundle-layout/1":
        fail("unsupported layout schema", "bundle_schema_unsupported")
    if layout["state"] == "missing":
        if layout["layout"] is not None or not _string(layout["reason"]):
            fail("missing layout requires reason")
    elif layout["state"] == "present":
        value = layout["layout"]
        if (
            source["parse_revision"] is None
            or not isinstance(value, dict)
            or value.get("layout_version") != "ddp-layout/1"
            or not isinstance(value.get("pdf_info"), list)
        ):
            fail("invalid fixed parse layout")
    else:
        fail("unknown layout state")
    evidence = parse_json(files["evidence.json"])
    _validate_evidence(evidence, source)
    if layout["state"] == "present":
        pages = {}
        for page in layout["layout"]["pdf_info"]:
            if (
                not isinstance(page, dict)
                or type(page.get("page_idx")) is not int
                or page["page_idx"] < 0
                or page["page_idx"] in pages
            ):
                fail("invalid or duplicate layout page")
            pages[page["page_idx"]] = page
        for record in evidence:
            locator = record["evidence"]["locator"]
            if locator["physical_page_index"] not in pages:
                fail("evidence page missing from fixed layout")
            page = pages[locator["physical_page_index"]]
            expected = page.get("page_size")
            if isinstance(expected, list) and len(expected) == 2:
                expected = {"width": expected[0], "height": expected[1]}
            actual = locator.get("page_size")
            if (
                actual
                and isinstance(expected, dict)
                and any(actual.get(axis) != expected.get(axis) for axis in ("width", "height"))
            ):
                fail("evidence page size differs from fixed layout")
    if not isinstance(parse_json(files["provenance.json"]), list):
        fail("invalid provenance")
    return VerifiedBundle(manifest, files, evidence, layout)


def read_bundle(file: BinaryIO) -> VerifiedBundle:
    file.seek(0, io.SEEK_END)
    if file.tell() > MAX_ARCHIVE:
        fail("archive exceeds limit", "bundle_too_large")
    file.seek(0)
    try:
        with zipfile.ZipFile(file) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES:
                fail("too many archive entries", "bundle_too_large")
            names = set()
            total = 0
            for info in infos:
                safe_member(info.orig_filename)
                if info.filename != info.orig_filename or info.filename in names:
                    fail("duplicate archive path")
                names.add(info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir() or mode not in (0, stat.S_IFREG):
                    fail("links and special files prohibited", "bundle_unsafe_path")
                if info.flag_bits & 1 or info.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    fail("unsupported archive encoding")
                maximum = MAX_MANIFEST if info.filename == "manifest.json" else MAX_FILE
                total += info.file_size
                if (
                    info.file_size > maximum
                    or total > MAX_EXPANDED
                    or info.file_size > max(1, info.compress_size) * MAX_RATIO
                ):
                    fail("archive expansion limit exceeded", "bundle_too_large")
            if "manifest.json" not in names or names - set(FILES) - {"manifest.json"}:
                fail("unknown or missing archive members")
            contents = {}
            for info in infos:
                # Never trust advertised lengths alone; enforce the bound on actual output.
                with archive.open(info) as member:
                    contents[info.filename] = member.read(min(MAX_FILE, info.file_size) + 1)
                if len(contents[info.filename]) != info.file_size:
                    fail("incorrect expanded size")
            manifest = parse_json(contents.pop("manifest.json"))
            return validate_parts(manifest, contents)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError) as exc:
        raise BundleError("bundle_invalid", "invalid ZIP archive") from exc


def build_bundle(source: dict, files: dict[str, bytes]) -> bytes:
    manifest = {
        "schema": SCHEMA,
        "required_features": [],
        "source": source,
        "files": [
            {"path": name, "size": len(data), "digest": digest(data), "role": FILES.get(name)}
            for name, data in sorted(files.items())
        ],
    }
    validate_parts(manifest, files)
    output = io.BytesIO()
    # Stored entries avoid rejecting our own highly repetitive layout/text as a zip bomb.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json_bytes(manifest))
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    value = output.getvalue()
    if len(value) > MAX_ARCHIVE:
        fail("archive exceeds limit", "bundle_too_large")
    return value
