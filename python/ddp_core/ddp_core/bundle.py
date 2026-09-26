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
# Opt-in payloads. Default bundles carry exactly FILES; these members are only
# present when the exporter explicitly opts in. Old readers reject them (unknown
# member or unknown required feature) instead of silently dropping them.
OPTIONAL_FILES = {
    "wiki.json": "wiki",
    "vectors.json": "vectors",
}
ALL_FILES = {**FILES, **OPTIONAL_FILES}
# Features a new reader understands. Unknown required features still reject;
# optional members listed here must be present when required.
KNOWN_FEATURES = frozenset({"wiki", "vectors"})
WIKI_SCHEMA = "ddp-bundle-wiki/1"
VECTORS_SCHEMA = "ddp-bundle-vectors/1"
MAX_WIKI_PAGES = 50
MAX_WIKI_PARAGRAPHS = 100
MAX_WIKI_RELATIONS = 50
MAX_WIKI_DEPENDENCIES = 10000
MAX_WIKI_PREDICATE = 255
MAX_VECTORS = 100000
MAX_EMBEDDING_DIM = 4096
MAX_PREPROCESSING_FIELD = 128
MAX_FORMAT_FIELD = 32
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
    wiki: dict | None = None
    vectors: dict | None = None

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

def _source_identity(value):
    _object(
        value,
        (
            "origin_node_id",
            "authority_node_id",
            "resource_id",
            "source_version_id",
            "source_digest",
            "parse_revision",
        ),
    )
    for key in ("origin_node_id", "authority_node_id"):
        if not isinstance(value[key], str) or not NODE_ID.fullmatch(value[key]):
            fail("invalid source dependency identity")
    for key in ("resource_id", "source_version_id"):
        if not _string(value[key]):
            fail("invalid source dependency identity")
    if not _digest(value["source_digest"]):
        fail("invalid source dependency digest")
    if value["parse_revision"] is not None and not _string(value["parse_revision"]):
        fail("invalid source dependency revision")


def _validate_locator(loc):
    _object(loc, ("kind", "physical_page_index", "seq"),
            ("bbox", "page_size", "printed_page_label"))
    if loc["kind"] not in ("page_block", "table_cell", "paragraph"):
        fail("invalid evidence locator kind")
    if any(type(loc[k]) is not int or loc[k] < 0 for k in ("physical_page_index", "seq")):
        fail("invalid evidence page or sequence")
    if loc.get("printed_page_label") is not None and not isinstance(loc["printed_page_label"], str):
        fail("invalid printed page label")
    size = loc.get("page_size")
    if size is not None:
        _object(size, ("width", "height"), ("rotation",))
        if (any(not _number(size[k]) or size[k] <= 0 for k in ("width", "height"))
                or type(size.get("rotation", 0)) is not int
                or size.get("rotation", 0) not in (0, 90, 180, 270)):
            fail("invalid page size")
    if loc.get("bbox") is not None:
        bbox = loc["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(_number(v) for v in bbox):
            fail("invalid bbox")
        if size is None:
            fail("bbox requires page size")
        if not (0 <= bbox[0] <= bbox[2] <= size["width"]
                and 0 <= bbox[1] <= bbox[3] <= size["height"]):
            fail("bbox outside page")


def _validate_wiki(payload, source, evidence_digests):
    # Imported Wiki is a **draft**: human paragraphs are preserved verbatim and
    # generated claims keep their original evidence bindings, but nothing here
    # auto-publishes imported private knowledge. Consumers must create a new
    # local revision (with a fresh dependency manifest) before publishing.
    # Cross-source relations/dependencies are never relabeled as the current
    # bundle's evidence: external bindings keep their original source identity
    # and are marked needs_review/unavailable until the source is readable.
    _object(payload, ("schema", "state", "title", "pages", "reason"), ("relations", "dependency_manifest"))
    if payload["schema"] != WIKI_SCHEMA:
        fail("unsupported wiki schema", "bundle_schema_unsupported")
    if payload["state"] not in ("draft", "empty"):
        fail("unknown wiki state")
    if payload["state"] == "empty":
        if payload["pages"] != [] or payload["title"] is not None:
            fail("empty wiki carries no pages")
        if not _string(payload["reason"]):
            fail("empty wiki requires a reason")
        if payload.get("relations", []) != [] or payload.get("dependency_manifest", []) != []:
            fail("empty wiki carries no relations or dependencies")
        return
    if payload["title"] is not None and not _string(payload["title"]):
        fail("invalid wiki title")
    if payload["state"] == "draft" and payload["reason"] is not None:
        fail("draft wiki carries no reason")
    pages = payload["pages"]
    if not isinstance(pages, list) or not pages or len(pages) > MAX_WIKI_PAGES:
        fail("invalid wiki pages")
    page_keys = set()
    for page in pages:
        _object(page, ("page_key", "title", "generated_sections", "human_paragraphs"))
        if not _string(page["page_key"]) or len(page["page_key"]) > 64:
            fail("invalid wiki page key")
        if page["page_key"] in page_keys:
            fail("duplicate wiki page key")
        page_keys.add(page["page_key"])
        if not _string(page["title"]) or len(page["title"]) > 255:
            fail("invalid wiki page title")
        sections = page["generated_sections"]
        if not isinstance(sections, list) or len(sections) > MAX_WIKI_PAGES:
            fail("invalid wiki sections")
        for section in sections:
            _object(section, ("heading", "claims"))
            if section["heading"] is not None and (
                not isinstance(section["heading"], str) or len(section["heading"]) > 512
            ):
                fail("invalid wiki heading")
            claims = section["claims"]
            if not isinstance(claims, list) or len(claims) > MAX_WIKI_PARAGRAPHS:
                fail("invalid wiki claims")
            seen_claims = set()
            for claim in claims:
                _object(
                    claim,
                    ("claim_id", "text", "evidence_ids", "unsupported"),
                    ("conflict_group",),
                )
                if not _string(claim["claim_id"]) or len(claim["claim_id"]) > 64:
                    fail("invalid wiki claim id")
                if claim["claim_id"] in seen_claims:
                    fail("duplicate wiki claim id")
                seen_claims.add(claim["claim_id"])
                if (
                    not isinstance(claim["text"], str)
                    or not claim["text"]
                    or len(claim["text"]) > 10000
                ):
                    fail("invalid wiki claim text")
                if not isinstance(claim["evidence_ids"], list) or any(
                    not _string(eid) for eid in claim["evidence_ids"]
                ):
                    fail("invalid wiki claim evidence")
                if not isinstance(claim["unsupported"], bool):
                    fail("invalid wiki claim support flag")
                if claim.get("conflict_group") is not None and not _string(claim["conflict_group"]):
                    fail("invalid wiki claim conflict group")
        paragraphs = page["human_paragraphs"]
        if not isinstance(paragraphs, list) or len(paragraphs) > MAX_WIKI_PARAGRAPHS:
            fail("invalid wiki human paragraphs")
        seen_paragraphs = set()
        for paragraph in paragraphs:
            _object(paragraph, ("id", "text"))
            if (
                not _string(paragraph["id"])
                or len(paragraph["id"]) > 64
                or not isinstance(paragraph["text"], str)
                or not paragraph["text"]
                or len(paragraph["text"]) > 10000
            ):
                fail("invalid wiki human paragraph")
            if paragraph["id"] in seen_paragraphs:
                fail("duplicate wiki human paragraph")
            seen_paragraphs.add(paragraph["id"])
    relations = payload.get("relations", [])
    if not isinstance(relations, list) or len(relations) > MAX_WIKI_RELATIONS:
        fail("invalid wiki relations")
    for relation in relations:
        _object(relation, ("subject_page", "object_page", "predicate", "evidence_ids", "unsupported"))
        for key in ("subject_page", "object_page"):
            if not _string(relation[key]) or len(relation[key]) > 64:
                fail("invalid wiki relation endpoint")
        if relation["subject_page"] == relation["object_page"]:
            fail("wiki relation endpoints must differ")
        if relation["subject_page"] not in page_keys or relation["object_page"] not in page_keys:
            fail("wiki relation references unknown page")
        if (
            not isinstance(relation["predicate"], str)
            or not relation["predicate"].strip()
            or len(relation["predicate"]) > MAX_WIKI_PREDICATE
        ):
            fail("invalid wiki relation predicate")
        if not isinstance(relation["evidence_ids"], list) or any(
            not _string(eid) for eid in relation["evidence_ids"]
        ):
            fail("invalid wiki relation evidence")
        if not isinstance(relation["unsupported"], bool):
            fail("invalid wiki relation support flag")
    manifest = payload.get("dependency_manifest", [])
    if not isinstance(manifest, list) or len(manifest) > MAX_WIKI_DEPENDENCIES:
        fail("invalid wiki dependency manifest")
    seen_deps = set()
    bindings = {}
    for dep in manifest:
        _object(dep, ("page_key", "evidence_id", "excerpt_digest", "source", "review"),
                ("source_evidence_id", "locator", "policy_revision"))
        if not _string(dep["page_key"]) or len(dep["page_key"]) > 64:
            fail("invalid wiki dependency page")
        if dep["page_key"] not in page_keys:
            fail("wiki dependency references unknown page")
        if not _string(dep["evidence_id"]):
            fail("invalid wiki dependency evidence")
        if not _digest(dep["excerpt_digest"]):
            fail("invalid wiki dependency digest")
        if dep["review"] not in ("available", "needs_review", "unavailable"):
            fail("unknown wiki dependency review state")
        if not isinstance(dep["source"], dict):
            fail("invalid wiki dependency source")
        _source_identity(dep["source"])
        for key in ("source_evidence_id", "policy_revision"):
            if key in dep and (not _string(dep[key]) or len(dep[key]) > 128):
                fail("invalid external wiki source binding")
        if "locator" in dep:
            _validate_locator(dep["locator"])
        binding = json_bytes({key: value for key, value in dep.items() if key not in ("page_key", "review")})
        if dep["evidence_id"] in bindings and bindings[dep["evidence_id"]] != binding:
            fail("ambiguous wiki evidence reference")
        bindings[dep["evidence_id"]] = binding
        if (dep["page_key"], dep["evidence_id"]) in seen_deps:
            fail("duplicate wiki dependency binding")
        seen_deps.add((dep["page_key"], dep["evidence_id"]))
        local = all(dep["source"][key] == source[key] for key in IDENTITY)
        if local:
            if any(key in dep for key in ("source_evidence_id", "locator", "policy_revision")):
                fail("bundled wiki source binding must use its evidence record")
            if dep["evidence_id"] not in evidence_digests:
                fail("wiki dependency references unknown evidence")
            if evidence_digests[dep["evidence_id"]] != dep["excerpt_digest"]:
                fail("wiki dependency digest mismatch", "bundle_digest_mismatch")
            if dep["review"] != "available":
                fail("local wiki dependency must be available")
        elif dep["review"] == "available":
            fail("external wiki dependency needs review")
        elif dep["evidence_id"] in evidence_digests:
            fail("external wiki reference collides with bundled evidence")
    # Claims and relations may cite bundled evidence or an external binding
    # declared above; anything else is a forged cross-source reference.
    declared_external = {
        dep["evidence_id"] for dep in manifest if not all(dep["source"][key] == source[key] for key in IDENTITY)
    }
    cited = set()
    for page in pages:
        for section in page["generated_sections"]:
            for claim in section["claims"]:
                for eid in claim["evidence_ids"]:
                    if eid not in evidence_digests and eid not in declared_external:
                        fail("wiki claim references unknown evidence")
                    cited.add(eid)
    for relation in relations:
        for eid in relation["evidence_ids"]:
            if eid not in evidence_digests and eid not in declared_external:
                fail("wiki relation references unknown evidence")
            cited.add(eid)
    bound = {dep["evidence_id"] for dep in manifest}
    if any(eid not in bound for eid in cited if eid in evidence_digests):
        fail("wiki citation lacks a dependency binding")


def _validate_vectors(payload, source, evidence_ids):
    # Vector payloads are **never** trusted blindly: the consumer records the
    # model/version/dimension/preprocessing/chunking/format/source-evidence
    # identity and rebuilds when anything is incompatible, rather than just
    # flagging rebuild_required. Observed metadata travels verbatim; nothing
    # here invents a tokenizer name or a model version from a model name.
    _object(payload, ("schema", "state", "model", "dimension", "preprocessing", "chunking",
                      "format", "source", "vectors", "reason"))
    if payload["schema"] != VECTORS_SCHEMA:
        fail("unsupported vectors schema", "bundle_schema_unsupported")
    if payload["state"] not in ("present", "missing"):
        fail("unknown vectors state")
    if payload["state"] == "missing":
        if payload["vectors"] != []:
            fail("missing vectors carry no rows")
        if not _string(payload["reason"]):
            fail("missing vectors require a reason")
        for key in ("model", "dimension", "preprocessing", "chunking", "format", "source"):
            if payload[key] is not None:
                fail("missing vectors carry no model identity")
        return
    if payload["reason"] is not None:
        fail("present vectors carry no reason")
    model = payload["model"]
    _object(model, ("name", "version", "dimension"))
    if (
        not _string(model["name"])
        or len(model["name"]) > 256
        or not _string(model["version"])
        or len(model["version"]) > 128
        or model["version"] == model["name"]
        or type(model["dimension"]) is not int
        or not 1 <= model["dimension"] <= MAX_EMBEDDING_DIM
    ):
        fail("invalid vector model identity")
    if payload["dimension"] != model["dimension"]:
        fail("vector dimension mismatch")
    preprocessing = payload["preprocessing"]
    _object(preprocessing, ("tokenizer", "normalization"))
    if (
        not _string(preprocessing["tokenizer"])
        or len(preprocessing["tokenizer"]) > MAX_PREPROCESSING_FIELD
        or not _string(preprocessing["normalization"])
        or len(preprocessing["normalization"]) > MAX_PREPROCESSING_FIELD
    ):
        fail("invalid vector preprocessing identity")
    chunking = payload["chunking"]
    _object(chunking, ("max_chars", "tokenizer", "chunker"))
    if (
        type(chunking["max_chars"]) is not int
        or not 1 <= chunking["max_chars"] <= 100000
        or not _string(chunking["tokenizer"])
        or len(chunking["tokenizer"]) > MAX_PREPROCESSING_FIELD
        or not _string(chunking["chunker"])
        or len(chunking["chunker"]) > MAX_PREPROCESSING_FIELD
    ):
        fail("invalid vector chunking identity")
    vector_format = payload["format"]
    _object(vector_format, ("dtype", "encoding"))
    if (
        not _string(vector_format["dtype"])
        or len(vector_format["dtype"]) > MAX_FORMAT_FIELD
        or not _string(vector_format["encoding"])
        or len(vector_format["encoding"]) > MAX_FORMAT_FIELD
    ):
        fail("invalid vector format identity")
    vector_source = payload["source"]
    _object(vector_source, ("source_digest", "parse_revision", "evidence_count"))
    if vector_source["source_digest"] != source["source_digest"]:
        fail("vector source digest mismatch", "bundle_digest_mismatch")
    if vector_source["parse_revision"] != source["parse_revision"] or source["parse_revision"] is None:
        fail("vector source revision mismatch")
    rows = payload["vectors"]
    if not isinstance(rows, list) or len(rows) > MAX_VECTORS:
        fail("invalid vector rows")
    if type(vector_source["evidence_count"]) is not int or vector_source["evidence_count"] != len(rows):
        fail("vector evidence count mismatch")
    seen = set()
    for row in rows:
        _object(row, ("evidence_id", "excerpt_digest", "embedding"))
        if not _string(row["evidence_id"]) or row["evidence_id"] in seen:
            fail("invalid or duplicate vector evidence")
        seen.add(row["evidence_id"])
        if row["evidence_id"] not in evidence_ids:
            fail("vector references unknown evidence")
        if not _digest(row["excerpt_digest"]):
            fail("invalid vector excerpt digest")
        # The excerpt digest must match the actual bundled evidence bytes, not
        # just look like a digest: a forged row with a valid shape but stale
        # content is rejected here, before any consumer can reuse it.
        if row["excerpt_digest"] != evidence_ids[row["evidence_id"]]:
            fail("vector excerpt digest mismatch", "bundle_digest_mismatch")
        vector = row["embedding"]
        if (
            not isinstance(vector, list)
            or len(vector) != model["dimension"]
            or not all(_number(v) for v in vector)
        ):
            fail("invalid vector embedding")


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
        _validate_locator(loc)
        if source["mime"] != "application/pdf" or loc["kind"] not in ("page_block", "table_cell"):
            fail("unsupported non-PDF locator", "bundle_schema_unsupported")
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
    if manifest["schema"] != SCHEMA:
        fail("unsupported bundle schema or required capability", "bundle_schema_unsupported")
    features = manifest["required_features"]
    if (
        not isinstance(features, list)
        or any(not isinstance(f, str) for f in features)
        or len(set(features)) != len(features)
        or any(f not in KNOWN_FEATURES for f in features)
    ):
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
        if name not in ALL_FILES or entry["role"] != ALL_FILES[name] or name in listed:
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
    # A required feature must be satisfied by its member file, and an opt-in
    # member must declare its feature: either direction missing means the
    # reader cannot interpret the archive, so reject instead of dropping bytes.
    for feature, member in (("wiki", "wiki.json"), ("vectors", "vectors.json")):
        if (feature in features) != (member in listed):
            fail("required feature member mismatch", "bundle_schema_unsupported")
    expected = set(FILES) - ({"source.bin"} if source["original"] == "missing" else set())
    if listed - set(OPTIONAL_FILES) != expected or set(files) != listed:
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
    evidence_digests = {
        record["evidence"]["evidence_id"]: record["evidence"]["excerpt_digest"]
        for record in evidence
    }
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
    wiki = None
    if "wiki.json" in files:
        wiki = parse_json(files["wiki.json"])
        _validate_wiki(wiki, source, evidence_digests)
    vectors = None
    if "vectors.json" in files:
        vectors = parse_json(files["vectors.json"])
        _validate_vectors(vectors, source, evidence_digests)
    return VerifiedBundle(manifest, files, evidence, layout, wiki, vectors)


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
            if "manifest.json" not in names or names - set(ALL_FILES) - {"manifest.json"}:
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


def build_bundle(source: dict, files: dict[str, bytes], *, required_features=None) -> bytes:
    features = sorted(required_features or [])
    if any(f not in KNOWN_FEATURES for f in features) or len(set(features)) != len(features):
        fail("unsupported bundle schema or required capability", "bundle_schema_unsupported")
    manifest = {
        "schema": SCHEMA,
        "required_features": features,
        "source": source,
        "files": [
            {"path": name, "size": len(data), "digest": digest(data), "role": ALL_FILES.get(name)}
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
