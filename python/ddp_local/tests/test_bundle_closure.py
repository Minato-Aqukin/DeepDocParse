"""Local Bundle consumption closure: real draft visibility, honest vectors, atomic import."""

import json
import io

import pytest
from ddp_bundle_fixture import sample_parts
from ddp_core.bundle import build_bundle, digest, json_bytes, read_bundle
from ddp_local.runtime import LocalRuntime


def _wiki(source, evidence):
    first = evidence[0]["evidence"]
    return {
        "schema": "ddp-bundle-wiki/1",
        "state": "draft",
        "title": "Imported Draft",
        "pages": [
            {
                "page_key": "page-a",
                "title": "Page A",
                "generated_sections": [
                    {"heading": "Facts",
                     "claims": [{"claim_id": "claim-1", "text": "A fixed source statement.",
                                 "evidence_ids": [first["evidence_id"]], "unsupported": False}]}
                ],
                "human_paragraphs": [{"id": "human-1", "text": "  Verbatim human note  "}],
            }
        ],
        "reason": None,
        "relations": [],
        "dependency_manifest": [
            {"page_key": "page-a", "evidence_id": first["evidence_id"],
             "excerpt_digest": first["excerpt_digest"],
             "source": {key: source[key] for key in (
                 "origin_node_id", "authority_node_id", "resource_id",
                 "source_version_id", "source_digest", "parse_revision")},
             "review": "available"}
        ],
    }


def _vectors_missing():
    return {
        "schema": "ddp-bundle-vectors/1",
        "state": "missing",
        "model": None,
        "dimension": None,
        "preprocessing": None,
        "chunking": None,
        "format": None,
        "source": None,
        "vectors": [],
        "reason": "embedding_unavailable: no local embedding provider on this CPU profile",
    }


def _bundle_with_optionals():
    source, files = sample_parts()
    evidence = __import__("json").loads(files["evidence.json"])
    files = {**files,
             "wiki.json": json_bytes(_wiki(source, evidence)),
             "vectors.json": json_bytes(_vectors_missing())}
    return build_bundle(source, files, required_features=["vectors", "wiki"]), source, evidence


def test_import_materializes_real_private_draft_and_keeps_verbatim_human_text(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        data, source, evidence = _bundle_with_optionals()
        created = runtime.import_bundle(io.BytesIO(data), operation_key="import-wiki")
        assert created["status"] == "succeeded"
        version = runtime.store.version(created["version_id"])
        assert version["state"] == "ready"
        # Original evidence is really queryable through FTS.
        assert runtime.search("fixed")["hits"]
        # The Wiki draft is a real private draft visible through list/read.
        items = runtime.wikis.list()["items"]
        assert items
        current = runtime.wikis.get(items[0]["wiki"]["id"])
        paragraphs = current["revision"]["pages"][0]["human_paragraphs"]
        assert paragraphs[0]["text"] == "  Verbatim human note  "
        # Nothing auto-published it as reviewed/original content.
        assert current["revision"]["semantic_review"] == "needs_review"
        assert current["revision"]["source_type"] == "generated"
        # Optional private content requires an explicit export choice.
        second = read_bundle(io.BytesIO(runtime.export_bundle(created["version_id"])))
        assert second.source == read_bundle(io.BytesIO(data)).source
        assert second.files["layout.json"] == read_bundle(io.BytesIO(data)).files["layout.json"]
        assert second.manifest["required_features"] == []
        included = read_bundle(io.BytesIO(runtime.export_bundle(
            created["version_id"], include_wiki=True, include_vectors=True)))
        assert set(included.manifest["required_features"]) == {"vectors", "wiki"}
        before = runtime.search("fixed")["hits"]
        again = runtime.import_bundle(io.BytesIO(data), operation_key="import-wiki")
        assert again["id"] == created["id"]
        assert runtime.search("fixed")["hits"] == before
        assert runtime.wikis.list()["items"] == items
    finally:
        runtime.close()


def test_vectors_without_provider_are_honestly_unavailable_but_fts_stays_searchable(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        data, _, _ = _bundle_with_optionals()
        created = runtime.import_bundle(io.BytesIO(data), operation_key="import-vec")
        version = runtime.store.version(created["version_id"])
        assert version["state"] == "ready"
        events = runtime.store.events()
        assert any(e["kind"] == "vectors.rejected" for e in events)
        assert not any(e["kind"] == "vectors.reused" for e in events)
        assert runtime.search("fixed")["hits"]
        exported = read_bundle(io.BytesIO(
            runtime.export_bundle(created["version_id"], include_vectors=True)))
        assert exported.vectors["state"] == "missing"
        assert "embedding_unavailable" in exported.vectors["reason"]
    finally:
        runtime.close()

def test_failed_wiki_materialization_leaves_no_ready_version(tmp_path, monkeypatch):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        data, _, _ = _bundle_with_optionals()
        real = runtime._materialize_wiki_draft

        def _boom(version_id, wiki, import_task_id):
            raise RuntimeError("simulated wiki crash")

        monkeypatch.setattr(runtime, "_materialize_wiki_draft", _boom)
        with pytest.raises(RuntimeError, match="simulated wiki crash"):
            runtime.import_bundle(io.BytesIO(data), operation_key="import-crash")
        assert runtime.store.versions() == []
        assert runtime.store.tasks() == []
        monkeypatch.setattr(runtime, "_materialize_wiki_draft", real)
        created = runtime.import_bundle(io.BytesIO(data), operation_key="import-crash")
        assert created["status"] == "succeeded"
        assert runtime.wikis.list()["items"]
    finally:
        runtime.close()


def test_incompatible_vectors_are_rejected_not_reused(tmp_path, monkeypatch):
    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        source, files = sample_parts()
        evidence = __import__("json").loads(files["evidence.json"])
        first = evidence[0]["evidence"]
        vectors = {
            "schema": "ddp-bundle-vectors/1",
            "state": "present",
            "model": {"name": "other-embed", "version": "other-1", "dimension": 4},
            "dimension": 4,
            "preprocessing": {"tokenizer": "other-tok", "normalization": "other-norm"},
            "chunking": {"max_chars": 800, "tokenizer": "other-tok", "chunker": "other-chunk"},
            "format": {"dtype": "float32", "encoding": "json-list"},
            "source": {"source_digest": source["source_digest"],
                       "parse_revision": source["parse_revision"], "evidence_count": 1},
            "vectors": [{"evidence_id": first["evidence_id"],
                         "excerpt_digest": first["excerpt_digest"],
                         "embedding": [0.1, 0.2, 0.3, 0.4]}],
            "reason": None,
        }
        data = build_bundle(source, {**files, "vectors.json": json_bytes(vectors)},
                            required_features=["vectors"])
        monkeypatch.setattr(runtime, "_local_vector_identity", lambda: {
            "name": "local-embed", "version": "local-1", "dimension": 4,
            "tokenizer": "local-tok", "normalization": "local-norm",
            "max_chars": 800, "chunk_tokenizer": "local-tok", "chunker": "local-chunk",
            "dtype": "float32", "encoding": "json-list"})
        created = runtime.import_bundle(io.BytesIO(data), operation_key="import-incompat")
        assert runtime.store.version(created["version_id"])["state"] == "ready"
        assert any(e["kind"] == "vectors.rejected" for e in runtime.store.events())
        assert runtime.search("fixed")["hits"]
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_foreign_draft_refs_survive_import_human_edit_and_export(tmp_path):
    source, files = sample_parts()
    evidence = json.loads(files["evidence.json"])
    wiki = _wiki(source, evidence)
    ref = "source:+remote-binding"
    external = {
        "page_key": "page-a", "evidence_id": ref,
        "source_evidence_id": evidence[0]["evidence"]["evidence_id"],
        "source": {**wiki["dependency_manifest"][0]["source"],
                   "origin_node_id": "other-node", "source_version_id": "other-version"},
        "excerpt_digest": evidence[0]["evidence"]["excerpt_digest"],
        "locator": evidence[0]["evidence"]["locator"],
        "policy_revision": "other-policy-7", "review": "needs_review",
    }
    wiki["pages"][0]["generated_sections"][0]["claims"][0]["evidence_ids"].append(ref)
    wiki["pages"].append({"page_key": "page-b", "title": "Related page",
                          "generated_sections": [], "human_paragraphs": []})
    wiki["dependency_manifest"].append(external)
    wiki["relations"].append({"subject_page": "page-a", "object_page": "page-b",
                              "predicate": "requires", "evidence_ids": [ref], "unsupported": False})
    data = build_bundle(source, {**files, "wiki.json": json_bytes(wiki)}, required_features=["wiki"])
    runtime = LocalRuntime(tmp_path)
    try:
        task = runtime.import_bundle(io.BytesIO(data), operation_key="foreign-import")
        item = runtime.wikis.list()["items"][0]
        initial = runtime.wikis.get(item["wiki"]["id"])["revision"]
        # A foreign reference has no local version that could change: not stale.
        assert initial["stale"] is False and initial["stale_reasons"] == {}
        assert ref in initial["pages"][0]["generated_sections"][0]["sentences"][0]["evidence_ids"]
        assert initial["relations"][0]["evidence_ids"] == [ref]
        frozen = next(d for d in initial["dependency_manifest"] if d["evidence_id"] == ref)
        assert frozen["local_version_id"] is None
        assert frozen["source_evidence_id"] == evidence[0]["evidence"]["evidence_id"]
        await runtime.edit_wiki(item["wiki"]["id"], "page-a",
                                {"base_revision_id": initial["id"],
                                 "paragraphs": [{"id": "human-1", "text": "Updated human note"}]},
                                operation_key="edit-import")
        exported = read_bundle(io.BytesIO(runtime.export_bundle(task["version_id"], include_wiki=True)))
        assert exported.wiki["pages"][0]["human_paragraphs"][0]["text"] == "Updated human note"
        assert exported.wiki["relations"] == wiki["relations"]
        assert external in exported.wiki["dependency_manifest"]
        assert runtime.wikis.get(item["wiki"]["id"], initial["id"])["revision"] == initial
    finally:
        runtime.close()


def test_human_only_draft_import_needs_no_invented_evidence(tmp_path):
    source, files = sample_parts()
    wiki = _wiki(source, json.loads(files["evidence.json"]))
    wiki["pages"][0]["generated_sections"] = []
    wiki["dependency_manifest"] = []
    files["evidence.json"] = json_bytes([])
    files["wiki.json"] = json_bytes(wiki)
    runtime = LocalRuntime(tmp_path)
    try:
        task = runtime.import_bundle(io.BytesIO(build_bundle(source, files, required_features=["wiki"])),
                                     operation_key="human-only")
        item = runtime.wikis.list()["items"][0]
        draft = runtime.wikis.get(item["wiki"]["id"])["revision"]
        assert draft["dependency_manifest"] == []
        exported = read_bundle(io.BytesIO(runtime.export_bundle(task["version_id"], include_wiki=True)))
        assert exported.wiki["pages"] == wiki["pages"]
        assert exported.evidence == []
    finally:
        runtime.close()
