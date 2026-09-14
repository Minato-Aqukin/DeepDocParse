"""MCP 用例的装配：**跑真的 Streamable HTTP 传输**。

## 为什么不直接调工具函数

这些工具现在从 HTTP 请求头里取 actor 上下文（`fastmcp` 的 HTTP 上下文），
而"取不到就拒绝"正是本轮要钉的行为。直接 `await tool.fn(...)` 的话根本没有
请求，测的就不是生产里那条路 —— 而且会让"我们确实在读请求头"这件事无从验证。

所以这里把 `mcp.http_app()` 挂在 `httpx.ASGITransport` 上，用真的
`fastmcp.Client` 打进去：头是真头，上下文是真上下文，不需要起进程、不需要端口。

`app.router.lifespan_context` 必须自己进 —— ASGITransport 不跑 lifespan，
而 FastMCP 的会话管理器是在 lifespan 里初始化的（少了它是
"Task group is not initialized"，一个与鉴权毫无关系的报错）。

## 两个客户端

    entry_client   带服务凭据 + 完整 actor 头 —— 生产里入口转发过来的样子
    rogue_client   直接打到本服务端口的人（凭据/头缺一样或全缺）

**不要给 rogue_client 补上凭据来"让用例过"**：它存在的意义就是那条路必须不通。
"""
from contextlib import asynccontextmanager

import httpx
import pytest

CORPUS = "http://corpus.test"          # 语料 API（monkeypatch 进去的地址）
GW = "http://gw.test"                  # 模型网关（纯算力：VQA / embeddings）
TOKEN = "svc-token-for-tests"          # 服务凭据
PUBLIC = "https://ddp.example.com"     # 对外基址（裁图 URL 绝对化用它）

#: 入口下发的 actor 上下文。**用 api_key 身份**：真实 MCP 调用都是 key 调的，
#: 而 `X-DDP-User` 只在这种身份下才有内容 —— 用 user 身份写用例会让
#: "漏转 X-DDP-User" 这个 bug 测不出来。
IDENTITY = {
    "X-DDP-Organization": "org-test",
    "X-DDP-Actor": "key-7",
    "X-DDP-Actor-Kind": "api_key",
    "X-DDP-Role": "contributor",
    "X-DDP-User": "user-alice",
    "X-DDP-Api-Key": "key-7",
    "X-Request-Id": "req-1",
}


@pytest.fixture
def mcp_env(monkeypatch):
    """把两个上游地址与服务凭据指到测试替身上。"""
    from ddp_mcp import corpus, server

    monkeypatch.setattr(corpus, "SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(corpus, "CORPUS_URL", CORPUS)
    monkeypatch.setattr(corpus, "PUBLIC_BASE_URL", PUBLIC)
    monkeypatch.setattr(server, "SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(server, "GATEWAY", GW)
    return server


@asynccontextmanager
async def mcp_client(headers: dict[str, str] | None = None):
    """连上本进程里的 MCP 服务（真 HTTP 语义，无端口）。"""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from ddp_mcp import server as mcp_server

    app = mcp_server.mcp.http_app(path="/")

    def factory(**kwargs):
        kwargs.pop("transport", None)
        kwargs.setdefault("timeout", 30)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://mcp", trust_env=False, **kwargs)

    async with app.router.lifespan_context(app):
        transport = StreamableHttpTransport(url="http://mcp/", headers=headers or {},
                                            httpx_client_factory=factory)
        async with Client(transport) as client:
            yield client


def entry_client():
    """入口转发过来的调用：服务凭据 + 完整 actor 上下文。

    **刻意不做成 pytest fixture。** `http_app` 的 lifespan 里有 anyio 任务组，
    而 pytest-asyncio 的 async 生成器 fixture 会在**另一个任务**里做收尾，
    于是每条用例都会在 teardown 抛
    `Attempted to exit cancel scope in a different task`——
    一个与被测行为毫无关系、却让整包用例变红的报错。用
    `async with entry_client() as client:` 就没有这个问题。
    """
    return mcp_client({"Authorization": f"Bearer {TOKEN}", **IDENTITY})


def passthrough_mcp(respx_router) -> None:
    """让打给本进程 MCP 应用的请求穿过 respx（只 mock 上游）。

    不加这一条的话，客户端连本服务这一步就会被 respx 当成"未 mock 的请求"拦下来，
    表现是一个与被测行为无关的 AllMockedAssertionError。
    """
    respx_router.route(host="mcp").pass_through()


async def call_text(client, tool: str, **arguments) -> str:
    """调一个返回字符串的工具，取出文本。"""
    result = await client.call_tool(tool, arguments)
    return "".join(item.text for item in result.content if getattr(item, "text", None))
