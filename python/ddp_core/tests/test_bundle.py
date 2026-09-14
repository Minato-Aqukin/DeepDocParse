"""T11/T12 real ZIP decoding; malicious fixtures must fail before publication."""

import io
import json
import stat
import zipfile

import pytest
from ddp_bundle_fixture import sample_bundle, sample_parts
from ddp_core.bundle import (
    BundleError,
    build_bundle,
    digest,
    json_bytes,
    read_bundle,
    validate_parts,
)


def rewrite_archive(data, *, alter=None, extra=None):
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    if alter:
        alter(entries)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def test_t11_portable_bundle_roundtrip():
    original = sample_bundle()
    verified = read_bundle(io.BytesIO(original))
    restored = read_bundle(io.BytesIO(build_bundle(verified.source, verified.files)))
    assert restored.source == verified.source
    assert restored.files == verified.files
    assert restored.evidence == verified.evidence
    assert restored.evidence[0]["evidence"]["evidence_id"] == "stable-source-evidence"


def test_t11_original_missing_is_explicit_and_does_not_invent_bytes():
    verified = read_bundle(io.BytesIO(sample_bundle(missing=True)))
    assert verified.source["original"] == "missing"
    assert "source.bin" not in verified.files


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        "C:/Windows/file",
        "C:\\file",
        "a/../../out",
        "a\\..\\out",
        "a//out",
        "./out",
    ],
)
def test_t12_path_traversal(path):
    value = rewrite_archive(sample_bundle(), extra=(path, b"attack"))
    with pytest.raises(BundleError, match="unsafe archive path"):
        read_bundle(io.BytesIO(value))


def test_t12_symlink_cannot_enter_available_area():
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    value = rewrite_archive(sample_bundle(), extra=(symlink, b"/etc/passwd"))
    with pytest.raises(BundleError, match="links"):
        read_bundle(io.BytesIO(value))


def test_t12_duplicate_member_is_rejected():
    with pytest.warns(UserWarning, match="Duplicate"):
        value = rewrite_archive(sample_bundle(), extra=("evidence.json", b"[]"))
    with pytest.raises(BundleError, match="duplicate archive"):
        read_bundle(io.BytesIO(value))


def test_t12_zip_bomb_has_advertised_and_actual_size_limits():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", b"x" * 200000)
    with pytest.raises(BundleError) as error:
        read_bundle(io.BytesIO(output.getvalue()))
    assert error.value.code == "bundle_too_large"


@pytest.mark.parametrize(
    "field,value", [("schema", "ddp-bundle/99"), ("required_features", ["run-shell"])]
)
def test_t12_unknown_required_schema(field, value):
    def alter(entries):
        manifest = json.loads(entries["manifest.json"])
        manifest[field] = value
        entries["manifest.json"] = json_bytes(manifest)

    with pytest.raises(BundleError) as error:
        read_bundle(io.BytesIO(rewrite_archive(sample_bundle(), alter=alter)))
    assert error.value.code == "bundle_schema_unsupported"


def test_t12_digest_and_required_members():
    for mutate in (
        lambda entries: entries.update({"source.bin": b"forged"}),
        lambda entries: entries.pop("evidence.json"),
        lambda entries: entries.update({"manifest.json": b'{"schema":1,"schema":2}'}),
    ):
        with pytest.raises(BundleError):
            read_bundle(io.BytesIO(rewrite_archive(sample_bundle(), alter=mutate)))


@pytest.mark.parametrize(
    "mutation", ["excerpt", "origin", "revision", "bbox", "generated", "cycle"]
)
def test_t12_forged_evidence_rejected_even_with_valid_file_digests(mutation):
    source, files = sample_parts()
    records = json.loads(files["evidence.json"])
    evidence = records[0]["evidence"]
    if mutation == "excerpt":
        records[0]["excerpt"] = "replacement text"
    elif mutation == "origin":
        evidence["origin_node_id"] = "some-other-node"
    elif mutation == "revision":
        evidence["parse_revision"] = "different-parse"
    elif mutation == "bbox":
        evidence["locator"]["bbox"] = [10, 20, 999999, 50]
    elif mutation == "generated":
        evidence["source_type"] = "generated"
    else:
        evidence["source_type"] = "generated"
        evidence["derived_from"] = evidence["evidence_id"]
    files["evidence.json"] = json_bytes(records)
    with pytest.raises(BundleError):
        build_bundle(source, files)


def test_declared_content_hash_cannot_authorize_missing_original():
    verified = read_bundle(io.BytesIO(sample_bundle()))
    manifest = verified.manifest
    manifest["source"]["source_digest"] = digest(b"an existing private document")
    with pytest.raises(BundleError, match="source digest mismatch"):
        validate_parts(manifest, verified.files)


@pytest.mark.parametrize("payload", [b'{"n":1e999}', b'{"n":"\\ud800"}', b'{"n":NaN}'])
def test_t12_invalid_json_scalars_are_rejected(payload):
    from ddp_core.bundle import parse_json

    with pytest.raises(BundleError):
        parse_json(payload)


def test_t12_locator_cannot_point_outside_fixed_layout():
    source, files = sample_parts()
    records = json.loads(files["evidence.json"])
    records[0]["evidence"]["locator"]["physical_page_index"] = 999
    files["evidence.json"] = json_bytes(records)
    with pytest.raises(BundleError, match="page missing"):
        build_bundle(source, files)
