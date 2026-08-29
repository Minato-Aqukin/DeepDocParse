import pytest
from contextlib import asynccontextmanager

from ddp_core.knowledge import (
    EntityMention, edge_result, merge_mentions, neighbor_ids, normalize_entity_name,
    wiki_sentence,
)


def test_entity_normalization_and_explicit_alias_merge_are_auditable():
    assert normalize_entity_name(" Qwen3-VL / 8B ") == "qwen3vl8b"
    groups = merge_mentions([
        EntityMention("DeepDocParse", "system", ("DDP",)),
        EntityMention("DDP", "system"),
    ])
    assert len(groups) == 1
    assert groups[0].merged_by == "alias" and "DDP" in groups[0].aliases


def test_similar_but_unconfirmed_names_are_not_silently_merged():
    groups = merge_mentions([
        EntityMention("Model-1", "model"), EntityMention("Model-2", "model")])
    assert len(groups) == 2


def test_low_confidence_model_alias_is_visible_and_splittable():
    group = merge_mentions([
        EntityMention("DeepDocParse", "system", ("DDP",), "model", 0.48),
    ])[0]
    assert group.merged_by == "model"
    assert group.merge_confidence == 0.48
    assert group.entity_merge_uncertain is True


def test_negative_relation_returns_not_found_instead_of_inventing_edge():
    result = edge_result(subject_id="a", predicate="", object_id=None,
                         evidence_ids=[], confidence=0.9, provider={"model": "fixture"})
    assert result == {"status": "not_found", "edge": None}


def test_edge_and_wiki_without_evidence_are_type_level_unsupported():
    edge = edge_result(subject_id="a", predicate="uses", object_id="b",
                       evidence_ids=[], confidence=2, provider={"model": "fixture"})["edge"]
    sentence = wiki_sentence(text="生成句。", evidence_ids=[])
    assert edge["unsupported"] is True and edge["confidence"] == 1.0
    assert sentence["unsupported"] is True and sentence["evidence_ids"] == []


def test_neighbor_depth_is_bounded_and_deterministic():
    edges = [("a", "b"), ("b", "c"), ("c", "d")]
    assert neighbor_ids("a", edges, 1) == {"a", "b"}
    assert neighbor_ids("a", edges, 2) == {"a", "b", "c"}
    with pytest.raises(ValueError):
        neighbor_ids("a", edges, 4)


@pytest.mark.asyncio
async def test_corpus_mcp_contract_exposes_five_tools_and_deprecated_compatibility_tool():
    import server

    tools = await server.mcp.list_tools()
    assert [tool.name for tool in tools] == [
        "search", "ask", "get_evidence", "read_wiki", "graph_neighbors", "ask_document"]
    contracts = (server_path := __import__("pathlib").Path(
        __file__).resolve().parent.parent / "docs" / "mcp-tools.md").read_text(encoding="utf-8")
    assert server_path.exists()
    assert all(f"### `{name}(" in contracts for name in (
        "search", "ask", "get_evidence", "read_wiki", "graph_neighbors"))


@pytest.mark.asyncio
async def test_corpus_mcp_missing_database_is_an_explicit_error(monkeypatch):
    import corpus

    monkeypatch.setattr(corpus, "DATABASE_URL", "")
    monkeypatch.setattr(corpus, "_sessions", None)
    monkeypatch.setattr(corpus, "_embedding", lambda _: _async_value((None, "embedding_unavailable")))
    with pytest.raises(RuntimeError, match="CORPUS_DATABASE_URL"):
        await corpus.search_impl("evidence")


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_get_evidence_returns_native_image_content(monkeypatch):
    import corpus

    evidence = type("EvidenceRow", (), {"crop_key": "crops/e.png"})()

    class Session:
        async def get(self, model, value):
            return evidence

    @asynccontextmanager
    async def fake_session():
        yield Session()

    async def payload(session, row):
        return {"evidence_id": "e1", "bbox": [1, 2, 3, 4], "crop_url": "/crop/e1"}

    monkeypatch.setattr(corpus, "corpus_session", fake_session)
    monkeypatch.setattr(corpus, "_evidence_payload", payload)
    monkeypatch.setattr(corpus, "_crop_bytes", lambda _: _async_value((b"png", None)))
    result = await corpus.get_evidence_impl("e1")
    assert result.structured_content["bbox"] == [1, 2, 3, 4]
    assert result.structured_content["crop_degraded"] is None
    assert [item.type for item in result.content] == ["text", "image"]
    assert result.content[1].mimeType == "image/png"

    # 取不到图不许静默退化成"这条证据本来就没图"：外部 agent 会据此以为无法核对
    monkeypatch.setattr(corpus, "_crop_bytes",
                        lambda _: _async_value((None, "crop_store_unavailable")))
    degraded = await corpus.get_evidence_impl("e1")
    assert degraded.structured_content["crop_degraded"] == "crop_store_unavailable"
    assert [item.type for item in degraded.content] == ["text"]


@pytest.mark.asyncio
async def test_missing_crop_store_is_reported_instead_of_looking_like_no_crop(monkeypatch):
    """`_crop_bytes` 自己就要把两种情况分开（不变式 2）。"""
    import corpus

    monkeypatch.setattr(corpus, "_minio", None)
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    assert await corpus._crop_bytes(None) == (None, None)
    assert await corpus._crop_bytes("crops/job/a.png") == (None, "crop_store_unavailable")

    class Boom:
        def get_object(self, *_):
            raise RuntimeError("minio down")

    monkeypatch.setattr(corpus, "_minio_client", lambda: Boom())
    assert await corpus._crop_bytes("crops/job/a.png") == (None, "crop_read_failed")
