"""Bundle optional-payload boundary: wiki drafts stay private drafts, vectors need real identity.

Covers the local Bundle closure: full-format validation of the new optional
members (relations/dependency_manifest, preprocessing/format/source, excerpt
digest cross-checks, boundedness/uniqueness/binding/unknown-feature rejection)
plus the local import/export closure (verbatim human paragraphs, real private
draft visibility, no auto-publish, honest dense-unavailable, FTS still
searchable, atomic failure, replay idempotency, layout round-trip).
"""

import io

import pytest
from ddp_bundle_fixture import sample_parts
from ddp_core.bundle import (
    MAX_EMBEDDING_DIM,
    BundleError,
    build_bundle,
    digest,
    json_bytes,
    read_bundle,
)


def _parts_with_optionals(**kwargs):
    source, files = sample_parts(**kwargs)
    evidence = [
        {"evidence": {**record["evidence"]}, "excerpt": record["excerpt"]}
        for record in __import__("json").loads(files["evidence.json"])
    ]
    return source, files, evidence


def _wiki_payload(source, evidence, **overrides):
    first = evidence[0]["evidence"]
    pages = [
        {
            "page_key": "page-a",
            "title": "Page A",
            "generated_sections": [
                {
                    "heading": "Facts",
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "text": "A fixed source statement.",
                            "evidence_ids": [first["evidence_id"]],
                            "unsupported": False,
                        }
                    ],
                }
            ],
            "human_paragraphs": [{"id": "human-1", "text": "  Verbatim human note  "}],
        }
    ]
    payload = {
        "schema": "ddp-bundle-wiki/1",
        "state": "draft",
        "title": "Draft",
        "pages": pages,
        "reason": None,
        "relations": [],
        "dependency_manifest": [
            {
                "page_key": "page-a",
                "evidence_id": first["evidence_id"],
                "excerpt_digest": first["excerpt_digest"],
                "source": {key: source[key] for key in (
                    "origin_node_id", "authority_node_id", "resource_id",
                    "source_version_id", "source_digest", "parse_revision")},
                "review": "available",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _vectors_payload(bundle_source, evidence, **overrides):
    first = evidence[0]["evidence"]
    payload = {
        "schema": "ddp-bundle-vectors/1",
        "state": "present",
        "model": {"name": "fixture-embed", "version": "embed-2026-09-21", "dimension": 4},
        "dimension": 4,
        "preprocessing": {"tokenizer": "observed-fixture-tok", "normalization": "observed-nfc"},
        "chunking": {"max_chars": 800, "tokenizer": "observed-fixture-tok", "chunker": "ddp-chunk/2"},
        "format": {"dtype": "float32", "encoding": "json-list"},
        "source": {"source_digest": bundle_source["source_digest"],
                   "parse_revision": bundle_source["parse_revision"], "evidence_count": 1},
        "vectors": [{"evidence_id": first["evidence_id"],
                     "excerpt_digest": first["excerpt_digest"],
                     "embedding": [0.1, 0.2, 0.3, 0.4]}],
        "reason": None,
    }
    payload.update(overrides)
    return payload


def _build(source, files, wiki=None, vectors=None):
    if wiki is not None:
        files = {**files, "wiki.json": json_bytes(wiki)}
    if vectors is not None:
        files = {**files, "vectors.json": json_bytes(vectors)}
    features = sorted(
        (["wiki"] if wiki is not None else []) + (["vectors"] if vectors is not None else [])
    )
    return build_bundle(source, files, required_features=features)


def test_full_optional_format_roundtrips():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    vectors = _vectors_payload(source, evidence)
    verified = read_bundle(io.BytesIO(_build(source, files, wiki, vectors)))
    assert verified.wiki["dependency_manifest"][0]["review"] == "available"
    assert verified.vectors["vectors"][0]["excerpt_digest"] == evidence[0]["evidence"]["excerpt_digest"]


def test_vector_excerpt_digest_must_match_actual_evidence():
    source, files, evidence = _parts_with_optionals()
    vectors = _vectors_payload(source, evidence)
    vectors["vectors"][0]["excerpt_digest"] = digest(b"something else")
    with pytest.raises(BundleError) as error:
        _build(source, files, None, vectors)
    assert error.value.code == "bundle_digest_mismatch"


def test_model_name_may_not_pose_as_version():
    source, files, evidence = _parts_with_optionals()
    vectors = _vectors_payload(source, evidence)
    vectors["model"] = {"name": "fixture-embed", "version": "fixture-embed", "dimension": 4}
    with pytest.raises(BundleError, match="model identity"):
        _build(source, files, None, vectors)


def test_external_wiki_binding_keeps_original_source_and_needs_review():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    external_id = "external-evidence"
    wiki["pages"][0]["generated_sections"][0]["claims"].append(
        {"claim_id": "claim-x", "text": "An external statement.",
         "evidence_ids": [external_id], "unsupported": True})
    wiki["dependency_manifest"].append(
        {"page_key": "page-a", "evidence_id": external_id,
         "excerpt_digest": digest(b"external bytes"),
         "source": {**{key: source[key] for key in (
             "origin_node_id", "authority_node_id", "resource_id",
             "source_version_id", "source_digest", "parse_revision")},
                    "resource_id": "c" * 32, "source_version_id": "d" * 32},
         "review": "needs_review"})
    verified = read_bundle(io.BytesIO(_build(source, files, wiki, None)))
    external = [d for d in verified.wiki["dependency_manifest"] if d["evidence_id"] == external_id][0]
    assert external["review"] == "needs_review"
    assert external["source"]["resource_id"] == "c" * 32


def test_external_binding_claiming_available_is_rejected():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    wiki["dependency_manifest"][0] = {
        **wiki["dependency_manifest"][0],
        "source": {**wiki["dependency_manifest"][0]["source"], "resource_id": "c" * 32},
        "review": "available",
    }
    with pytest.raises(BundleError, match="external wiki dependency needs review"):
        _build(source, files, wiki, None)


def test_forged_cross_source_citation_is_rejected():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    wiki["pages"][0]["generated_sections"][0]["claims"][0]["evidence_ids"] = ["forged-id"]
    with pytest.raises(BundleError, match="unknown evidence"):
        _build(source, files, wiki, None)


def test_citation_without_dependency_binding_is_rejected():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence, dependency_manifest=[])
    with pytest.raises(BundleError, match="dependency binding"):
        _build(source, files, wiki, None)


@pytest.mark.parametrize("field", ["relations", "dependency_manifest", "preprocessing", "format", "source"])
def test_unknown_optional_shape_is_rejected(field):
    source, files, evidence = _parts_with_optionals()
    if field in ("relations", "dependency_manifest"):
        wiki = _wiki_payload(source, evidence, **{field: [{"bogus": 1}]})
        with pytest.raises(BundleError):
            _build(source, files, wiki, None)
    else:
        vectors = _vectors_payload(source, evidence, **{field: {"bogus": 1}})
        with pytest.raises(BundleError):
            _build(source, files, None, vectors)


def test_duplicate_wiki_identities_rejected():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    wiki["pages"].append({**wiki["pages"][0]})
    with pytest.raises(BundleError, match="duplicate wiki page key"):
        _build(source, files, wiki, None)


def test_duplicate_vector_rows_rejected():
    source, files, evidence = _parts_with_optionals()
    vectors = _vectors_payload(source, evidence)
    vectors["vectors"].append(dict(vectors["vectors"][0]))
    vectors["source"]["evidence_count"] = 2
    with pytest.raises(BundleError, match="duplicate vector evidence"):
        _build(source, files, None, vectors)


def test_vector_count_must_match_source_identity():
    source, files, evidence = _parts_with_optionals()
    vectors = _vectors_payload(source, evidence)
    vectors["source"]["evidence_count"] = 2
    with pytest.raises(BundleError, match="evidence count"):
        _build(source, files, None, vectors)


def test_unknown_required_feature_rejected_and_member_mismatch_rejected():
    source, files, evidence = _parts_with_optionals()
    wiki = _wiki_payload(source, evidence)
    with pytest.raises(BundleError) as error:
        build_bundle(source, {**files, "wiki.json": json_bytes(wiki)},
                     required_features=["run-shell"])
    assert error.value.code == "bundle_schema_unsupported"
    with pytest.raises(BundleError) as error:
        build_bundle(source, {**files, "wiki.json": json_bytes(wiki)}, required_features=[])
    assert error.value.code == "bundle_schema_unsupported"


def test_oversized_vector_dimension_rejected():
    source, files, evidence = _parts_with_optionals()
    vectors = _vectors_payload(source, evidence)
    vectors["model"]["dimension"] = MAX_EMBEDDING_DIM + 1
    vectors["dimension"] = MAX_EMBEDDING_DIM + 1
    vectors["vectors"][0]["embedding"] = [0.0] * (MAX_EMBEDDING_DIM + 1)
    with pytest.raises(BundleError, match="model identity"):
        _build(source, files, None, vectors)
