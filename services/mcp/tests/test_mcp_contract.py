"""语料级 MCP 的契约与**身份边界**。

## 这个文件在搬迁后验什么

五个工具的取数已经移进 corpus-api（`routers/mcp_tools.py`，授权与 `/api/*`
共用一条链），本服务只剩一层 HTTP 适配。所以这里验的是那一层的三件事：

1. 工具清单与契约文档仍然对得上（铁律 6：签名不变）；
2. **凭据不对或没有 actor 头时，每个工具都拒绝**，而不是按匿名继续；
3. 拿到的身份**原样转发**给语料 API —— 包括 `X-DDP-User`
   （api_key 调用的真实主体在它里面，丢了它 ACL 会判错人）。

搬迁前这里有两条用例验"数据库没配好要报错"和"直接从 MinIO 取裁图"——
它们验的是**这个服务自己连库连桶**，而那正是本轮删掉的越权面，所以一并删了。
"""
import json

import pytest
import respx
from httpx import Response

from ddp_paths import CONTRACTS

from tests.conftest import CORPUS, IDENTITY, TOKEN, entry_client, mcp_client, passthrough_mcp

TOOLS = ("search", "ask", "get_evidence", "read_wiki", "graph_neighbors")

#: 每个工具的一次最小调用。**清单不许缩** —— 身份检查必须逐个工具都成立，
#: "抽查两个"正是让某个工具漏挂检查的方式。
CALLS = {
    "search": {"query": "证据"},
    "ask": {"question": "总收入是多少"},
    "get_evidence": {"evidence_id": "ev-1"},
    "read_wiki": {"entry_id_or_title": "北极星"},
    "graph_neighbors": {"entity_id_or_name": "北极星"},
    "ask_document": {"file_url": "http://files.example.com/a.pdf", "question": "多少"},
}


async def test_corpus_mcp_contract_exposes_five_tools_and_deprecated_compatibility_tool():
    from ddp_mcp import server

    tools = await server.mcp.list_tools()
    assert [tool.name for tool in tools] == [*TOOLS, "ask_document"]
    contract_path = CONTRACTS / "mcp" / "mcp-tools.md"
    assert contract_path.exists(), f"契约文件不在 {contract_path}"
    contracts = contract_path.read_text(encoding="utf-8")
    assert all(f"### `{name}(" in contracts for name in TOOLS)


# --------------------------------------------------------------- 没有身份就不回答

@pytest.mark.parametrize("tool", list(CALLS))
async def test_every_tool_fails_closed_without_service_credentials(mcp_env, tool):
    """直接打到本服务端口、不带服务凭据 —— 全部拒绝。

    这是承重墙：本服务信任 `X-DDP-*` 头的唯一理由是"只有入口能发出带服务凭据
    的请求"。少了这道检查，任何能连到这个端口的人都能自称 admin，而**下游会
    完全正常地接受它** —— 没有报错、没有异常日志，只是权限没了。
    """
    async with mcp_client(IDENTITY) as client:        # 有身份头、没有凭据
        result = await client.call_tool(tool, CALLS[tool], raise_on_error=False)
    assert result.is_error, f"{tool} 在没有服务凭据时竟然执行了"
    assert "服务凭据" in result.content[0].text


@pytest.mark.parametrize("tool", list(CALLS))
async def test_every_tool_fails_closed_without_actor_context(mcp_env, tool):
    """凭据对、但没有 actor 头：同样拒绝。

    这一半同样重要 —— 入口挂错中间件时，请求会带着服务凭据但没有身份。
    给它一个默认身份（viewer / 匿名）等于把配置错渲染成"这个人突然只能看公开的"。
    """
    async with mcp_client({"Authorization": f"Bearer {TOKEN}"}) as client:
        result = await client.call_tool(tool, CALLS[tool], raise_on_error=False)
    assert result.is_error, f"{tool} 在没有 actor 上下文时竟然执行了"
    assert "actor 上下文头" in result.content[0].text


@pytest.mark.parametrize("missing", ["X-DDP-Organization", "X-DDP-Actor",
                                     "X-DDP-Actor-Kind", "X-DDP-Role"])
async def test_partial_actor_context_is_rejected(mcp_env, missing):
    """**缺一个也不行。** 少了组织就没有组织边界（企业边界 8），
    少了角色就没法判能力 —— 任何一项缺失都不是"少一点信息"，而是没有身份。"""
    headers = {"Authorization": f"Bearer {TOKEN}", **IDENTITY}
    headers.pop(missing)
    async with mcp_client(headers) as client:
        result = await client.call_tool("search", {"query": "x"}, raise_on_error=False)
    assert result.is_error and missing in result.content[0].text


async def test_wrong_service_token_is_rejected(mcp_env):
    async with mcp_client({"Authorization": "Bearer not-the-token", **IDENTITY}) as client:
        result = await client.call_tool("search", {"query": "x"}, raise_on_error=False)
    assert result.is_error and "服务凭据" in result.content[0].text


async def test_empty_configured_token_never_means_open(monkeypatch, mcp_env):
    """`SERVICE_TOKEN` 没配时**拒绝服务**，而不是"谁都能进"。

    空串会让比对对一个空 Bearer 成立 —— 那是一道形同虚设的门，而且是静默的。
    """
    from ddp_mcp import corpus

    monkeypatch.setattr(corpus, "SERVICE_TOKEN", "")
    async with mcp_client({"Authorization": "Bearer ", **IDENTITY}) as client:
        result = await client.call_tool("search", {"query": "x"}, raise_on_error=False)
    assert result.is_error and "SERVICE_TOKEN" in result.content[0].text


async def test_no_http_context_means_no_identity(mcp_env):
    """压根没有 HTTP 上下文（比如 stdio 传输）时也必须拒绝。

    `get_http_headers()` 在这种情况下返回空 dict 而**不抛** —— 于是
    "拿不到头"会自然落到缺凭据那一支。这条用例钉的就是那个落点：
    它是失败闭合，而不是"本地跑就当它是 admin"。
    """
    from fastmcp.exceptions import ToolError

    from ddp_mcp import corpus

    with pytest.raises(ToolError, match="服务凭据"):
        await corpus.search_impl("证据")


# --------------------------------------------------------------- 身份原样转发

@respx.mock
async def test_identity_headers_are_forwarded_to_the_corpus_api(mcp_env):
    """入口给的每一项都要转过去，而**客户端的 Authorization 到此为止**。"""
    passthrough_mcp(respx)
    route = respx.post(f"{CORPUS}/internal/mcp/search").mock(
        return_value=Response(200, json={"results": [], "degraded": None}))

    async with entry_client() as client:
        await client.call_tool("search", {"query": "证据"})

    sent = route.calls.last.request.headers
    for name, value in IDENTITY.items():
        assert sent[name] == value, f"{name} 没有原样转发"
    # 服务凭据是本服务自己的那份（与入口换手一次），不是客户端那串
    assert sent["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(route.calls.last.request.content) == {"query": "证据", "limit": 10}


@respx.mock
async def test_corpus_errors_surface_instead_of_looking_like_empty_results(mcp_env):
    """语料侧 5xx 不许变成"语料里没有" —— 契约「错误与降级」那一节。"""
    passthrough_mcp(respx)
    respx.post(f"{CORPUS}/internal/mcp/search").mock(
        return_value=Response(503, json={"error": {"message": "db down", "type": "api_error",
                                                   "code": "internal_error"}}))
    async with entry_client() as client:
        result = await client.call_tool("search", {"query": "x"}, raise_on_error=False)
    assert result.is_error
    assert "503" in result.content[0].text and "db down" in result.content[0].text


@respx.mock
async def test_get_evidence_returns_native_image_content(mcp_env):
    """裁图必须以 MCP 原生 image content 回去（外部 agent 要自己核对像素）。"""
    passthrough_mcp(respx)
    respx.get(f"{CORPUS}/internal/mcp/evidence/ev-1").mock(return_value=Response(200, json={
        "status": "ok",
        "evidence": {"evidence_id": "ev-1", "bbox": [1, 2, 3, 4],
                     "crop_url": "/api/documents/doc-1/crops/job-1/0.png",
                     "crop_degraded": None},
        "crop": {"mime": "image/png", "data_base64": "cG5n"}}))

    async with entry_client() as client:
        result = await client.call_tool("get_evidence", {"evidence_id": "ev-1"})
    assert [item.type for item in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/png"
    assert result.content[1].data == "cG5n"
    payload = result.structured_content
    assert payload["bbox"] == [1, 2, 3, 4]
    # 相对裁图路径要补成对外可用的绝对地址（语料侧不知道自己挂在哪个域名下）
    assert payload["crop_url"] == "https://ddp.example.com/api/documents/doc-1/crops/job-1/0.png"


@respx.mock
async def test_unreadable_crop_is_not_reported_as_no_crop(mcp_env):
    """"取不到图"与"本来就没图"必须分开地透传（不变式 2）。"""
    passthrough_mcp(respx)
    respx.get(f"{CORPUS}/internal/mcp/evidence/ev-2").mock(return_value=Response(200, json={
        "status": "ok",
        "evidence": {"evidence_id": "ev-2", "crop_url": None,
                     "crop_degraded": "crop_store_unavailable"},
        "crop": None}))
    async with entry_client() as client:
        result = await client.call_tool("get_evidence", {"evidence_id": "ev-2"})
    assert [item.type for item in result.content] == ["text"]
    assert result.structured_content["crop_degraded"] == "crop_store_unavailable"


@respx.mock
async def test_missing_evidence_is_a_structured_not_found(mcp_env):
    passthrough_mcp(respx)
    respx.get(f"{CORPUS}/internal/mcp/evidence/nope").mock(
        return_value=Response(404, json={"error": {"message": "evidence not found",
                                                   "type": "invalid_request_error",
                                                   "code": "not_found"}}))
    async with entry_client() as client:
        result = await client.call_tool("get_evidence", {"evidence_id": "nope"},
                                        raise_on_error=False)
    assert result.is_error and result.structured_content == {"status": "not_found"}


@respx.mock
async def test_wiki_and_graph_map_not_found_without_inventing_content(mcp_env):
    passthrough_mcp(respx)
    not_found = Response(404, json={"error": {"message": "not found",
                                             "type": "invalid_request_error",
                                             "code": "not_found"}})
    respx.get(f"{CORPUS}/internal/mcp/wiki").mock(return_value=not_found)
    respx.get(f"{CORPUS}/internal/mcp/graph/neighbors").mock(return_value=not_found)

    async with entry_client() as client:
        wiki = await client.call_tool("read_wiki", {"entry_id_or_title": "无"})
        graph = await client.call_tool("graph_neighbors", {"entity_id_or_name": "无"})
    assert wiki.structured_content == {"status": "not_found"}
    assert graph.structured_content == {"status": "not_found"}


@respx.mock
async def test_graph_depth_is_checked_before_calling_the_corpus(mcp_env):
    passthrough_mcp(respx)
    route = respx.get(f"{CORPUS}/internal/mcp/graph/neighbors")
    async with entry_client() as client:
        result = await client.call_tool("graph_neighbors",
                                        {"entity_id_or_name": "北极星", "depth": 7})
    assert result.structured_content == {"status": "invalid_depth"}
    assert not route.called


@respx.mock
async def test_tool_arguments_reach_the_corpus_under_the_names_it_expects(mcp_env):
    """参数名是两侧的接缝：写错了表现是 422，而不是"结果不对"。"""
    passthrough_mcp(respx)
    wiki = respx.get(f"{CORPUS}/internal/mcp/wiki").mock(
        return_value=Response(200, json={"status": "ok", "entry": {}, "sections": []}))
    graph = respx.get(f"{CORPUS}/internal/mcp/graph/neighbors").mock(
        return_value=Response(200, json={"status": "ok", "center_id": "e1",
                                         "entities": [], "edges": []}))
    ask = respx.post(f"{CORPUS}/internal/mcp/ask").mock(
        return_value=Response(200, json={"assertions": [], "degraded": None}))

    async with entry_client() as client:
        await client.call_tool("read_wiki", {"entry_id_or_title": "北极星"})
        await client.call_tool("graph_neighbors", {"entity_id_or_name": "北极星", "depth": 2})
        await client.call_tool("ask", {"question": "多少"})

    assert dict(wiki.calls.last.request.url.params) == {"value": "北极星"}
    assert dict(graph.calls.last.request.url.params) == {"value": "北极星", "depth": "2"}
    assert json.loads(ask.calls.last.request.content) == {"question": "多少"}


@respx.mock
async def test_fixed_parse_scope_and_asset_aliases_survive_the_http_adapter(mcp_env):
    passthrough_mcp(respx)
    item = {"evidence_id": "ev-bob", "document_id": "same-bytes", "parse_revision": "parse-bob",
            "resource_id": "asset-bob", "source_version_id": "version-bob", "filename": "bob.pdf",
            "copies": [{"resource_id": "asset-bob", "source_version_id": "version-bob", "filename": "bob.pdf"}],
            "crop_url": "/api/documents/same-bytes/crops/parse-bob/0_digest.png"}
    payload = {"results": [item], "degraded": None, "scope": {"authorized_parse_revisions": 1}}
    respx.post(f"{CORPUS}/internal/mcp/search").mock(return_value=Response(200, json=payload))
    async with entry_client() as client:
        result = await client.call_tool("search", {"query": "Bob"})
    assert result.structured_content["scope"] == payload["scope"]
    actual = result.structured_content["results"][0]
    assert actual["parse_revision"] == "parse-bob" and actual["copies"] == item["copies"]
    assert actual["crop_url"] == "https://ddp.example.com" + item["crop_url"]
    assert payload["results"][0]["crop_url"].startswith("/"), "adapter must not mutate the upstream payload"


def test_thin_adapter_does_not_reintroduce_storage_or_database_dependencies():
    import ast
    import tomllib
    from pathlib import Path
    from ddp_mcp import corpus

    root = Path(__file__).resolve().parents[1]
    dependencies = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "ddp-core" in dependencies and not any("ddp-core[" in item for item in dependencies)
    assert not any(item.lower().startswith(("sqlalchemy", "asyncpg", "minio", "boto3")) for item in dependencies)
    tree = ast.parse(Path(corpus.__file__).read_text())
    imported = {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names)
    assert not imported.intersection({"sqlalchemy", "asyncpg", "minio", "boto3"})
