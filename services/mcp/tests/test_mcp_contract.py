"""语料级 MCP 的契约用例（铁律 6：五个小而正交的工具）。

从 `ddp_core` 的知识层用例里拆出来 —— 它们验的是 MCP 服务的对外形状，
不是纯逻辑。合仓前两者挤在同一个文件里，因为当时 mcp_server 没有自己的测试目录。
"""
from contextlib import asynccontextmanager

import pytest

from ddp_paths import CONTRACTS

@pytest.mark.asyncio
async def test_corpus_mcp_contract_exposes_five_tools_and_deprecated_compatibility_tool():
    from ddp_mcp import server

    tools = await server.mcp.list_tools()
    assert [tool.name for tool in tools] == [
        "search", "ask", "get_evidence", "read_wiki", "graph_neighbors", "ask_document"]
    contract_path = CONTRACTS / "mcp" / "mcp-tools.md"
    assert contract_path.exists(), f"契约文件不在 {contract_path}"
    contracts = contract_path.read_text(encoding="utf-8")
    assert all(f"### `{name}(" in contracts for name in (
        "search", "ask", "get_evidence", "read_wiki", "graph_neighbors"))


@pytest.mark.asyncio
async def test_corpus_mcp_missing_database_is_an_explicit_error(monkeypatch):
    from ddp_mcp import corpus

    monkeypatch.setattr(corpus, "DATABASE_URL", "")
    monkeypatch.setattr(corpus, "_sessions", None)
    monkeypatch.setattr(corpus, "_embedding", lambda _: _async_value((None, "embedding_unavailable")))
    with pytest.raises(RuntimeError, match="CORPUS_DATABASE_URL"):
        await corpus.search_impl("evidence")


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_get_evidence_returns_native_image_content(monkeypatch):
    from ddp_mcp import corpus

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
    from ddp_mcp import corpus

    monkeypatch.setattr(corpus, "_minio", None)
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    assert await corpus._crop_bytes(None) == (None, None)
    assert await corpus._crop_bytes("crops/job/a.png") == (None, "crop_store_unavailable")

    class Boom:
        def get_object(self, *_):
            raise RuntimeError("minio down")

    monkeypatch.setattr(corpus, "_minio_client", lambda: Boom())
    assert await corpus._crop_bytes("crops/job/a.png") == (None, "crop_read_failed")
