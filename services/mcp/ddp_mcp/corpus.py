"""五个语料工具的**HTTP 适配层** —— 本模块不碰数据库，也不碰对象存储。

## 它以前是什么样，为什么必须变

搬迁前这里自己 `create_async_engine(CORPUS_DATABASE_URL)` + 自己连 MinIO，
用的是一条**没有任何 actor 的连接**：`search` 直接 SELECT 全库 evidence，
`get_evidence` 直接按 `crop_key` 从对象存储取像素。于是

- 授权判据一条都没有：谁连得上 MCP 端口，谁就读得到整份语料的原文与裁图；
- 资源层（迁移 0015）上线后这变成实打实的越权 —— 别人私有资源里的内容
  会被原样交给外部 agent，而语料域那边刚建好的 `ddp_corpus.policy`
  在这条路上完全没有落点。

现在取数全在 `corpus-api` 的 `/internal/mcp/*`（`routers/mcp_tools.py`），
和 `/api/*` 共用同一条 `current_actor` + policy 授权链。本模块只做三件事：
**验服务凭据、转发 actor 上下文、把响应还原成 MCP 的返回形状。**

## 身份从哪来，以及为什么必须先验服务凭据

入口（control-api）验完 API key 之后，把 actor 上下文写成一组 `X-DDP-*` 头
转发过来（`internal/identity.Actor.Apply`），并把**客户端传来的同名头
无条件剥掉**。本服务因此只需要回答一个问题：**这组头是不是入口填的？**

判据是随请求一起来的服务凭据（`Authorization: Bearer $SERVICE_TOKEN`，
与入口的 `proxy.Rewrite` 对应）。少了这一步，任何能连到 MCP 端口的人
都能自己发一个 `X-DDP-Role: admin` —— 而下游 corpus-api 会完全正常地
接受它，没有报错、没有异常日志，只是权限没了。

**没有身份就失败，不是降级。** 缺头、凭据不对、压根没有 HTTP 上下文
（stdio 传输）三种情况一律抛错：默认放行的那一版等于把"入口漏挂了鉴权"
表现成"匿名也能读全语料"。
"""
from __future__ import annotations

import json
import os
import secrets

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent

#: 语料 API 的内网地址（compose 里是 `http://corpus-api:8081`）。
CORPUS_URL = os.environ.get("CORPUS_API_URL", "http://127.0.0.1:8081").rstrip("/")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
#: 裁图 URL 的对外基址。语料域返回的是相对路径（它不知道自己被挂在哪个域名下），
#: 绝对化在这里做 —— 外部 agent 拿到相对路径没法用。
PUBLIC_BASE_URL = os.environ.get("MCP_PUBLIC_BASE_URL", "").rstrip("/")

#: 要转给语料 API 的身份头。**与 control-api 的 `identity.Inbound` 同一张表**：
#: 那边加一个头、这边忘了转，表现是"新的授权维度对 MCP 这条路静默失效"。
#: `X-DDP-User` 正是这种头 —— api_key 调用的真实主体在它里面，
#: 丢了它 `policy.resource_condition` 会拿 key id 去比 `owner_id`，
#: 结果是"用 key 调 MCP 的人什么都看不见"。
#: 值写成规范大小写：HTTP 头不分大小写，但**报错信息要说人能搜的那个名字**
#: （部署侧文档与 Go 常量都写作 `X-DDP-Organization`）。
IDENTITY_HEADERS = (
    "X-DDP-Organization", "X-DDP-Actor", "X-DDP-Actor-Kind", "X-DDP-Role",
    "X-DDP-User", "X-DDP-Api-Key", "X-Request-Id", "traceparent",
)
#: 缺任何一个就不算有身份（与 `ddp_corpus.deps.current_actor` 的必填集一致）。
REQUIRED_HEADERS = ("X-DDP-Organization", "X-DDP-Actor", "X-DDP-Actor-Kind", "X-DDP-Role")

_http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=300), trust_env=False)


def forwarded_identity() -> dict[str, str]:
    """取出这次调用的可信 actor 上下文，取不到就抛。

    `get_http_headers` 在没有活动 HTTP 请求时返回空 dict（不抛），所以
    "没有 HTTP 上下文"会自然落到下面缺凭据那一支 —— 这正是想要的：
    失败闭合，而不是"本地跑就当它是 admin"。
    """
    headers = get_http_headers(include={"authorization"})
    if not SERVICE_TOKEN:
        # 空 token 会让 compare_digest 对一个空 Bearer 成立，等于没有门。
        # 这是部署配置错，必须炸在第一次调用上而不是静默放行
        raise ToolError("MCP 服务未配置 SERVICE_TOKEN，拒绝服务（否则转发头不可信）")
    token = headers.get("authorization", "")
    token = token[7:].strip() if token[:7].lower() == "bearer " else ""
    if not token or not secrets.compare_digest(token, SERVICE_TOKEN):
        raise ToolError("缺少或不正确的服务凭据：本服务只接受经入口（control-api）"
                        "转发的调用，不直接受理客户端请求")
    # get_http_headers 给的是小写键；转发时用规范大小写
    identity = {name: headers[name.lower()] for name in IDENTITY_HEADERS
                if headers.get(name.lower())}
    missing = [name for name in REQUIRED_HEADERS if name not in identity]
    if missing:
        raise ToolError(f"缺少 actor 上下文头：{', '.join(missing)}。"
                        "这些头由入口下发；没有它们就无法判断你能看哪些资源，"
                        "因此本次调用被拒绝而不是按匿名处理")
    return {**identity, "authorization": f"Bearer {SERVICE_TOKEN}"}


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{error.get('code') or error.get('type')}: {error.get('message')}"
    return response.text[:200]


async def _call(method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, allow_404: bool = False) -> dict | None:
    """调语料 API 的一个 MCP 端点。404 按调用方要求转成 None。

    连不上、5xx、鉴权失败一律抛 `ToolError` —— **绝不返回空结果**：
    "数据库不可用"长得像"语料里没有"是这个项目明令禁止的形状（契约
    「错误与降级」一节：不得返回看似成功的空数组）。
    """
    try:
        response = await _http.request(method, f"{CORPUS_URL}{path}", json=body,
                                       params=params, headers=forwarded_identity())
    except httpx.HTTPError as exc:
        raise ToolError(f"语料服务不可达：{type(exc).__name__}") from exc
    if response.status_code == 404 and allow_404:
        return None
    if response.status_code >= 400:
        raise ToolError(f"语料服务拒绝了这次调用（HTTP {response.status_code}）："
                        f"{_message(response)}")
    return response.json()


def _absolutize(value):
    """把响应里所有 `crop_url` 补成绝对地址（就地不改原对象语义，递归重建）。"""
    if isinstance(value, list):
        return [_absolutize(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: _absolutize(item) for key, item in value.items()}
    url = out.get("crop_url")
    if isinstance(url, str) and url.startswith("/") and PUBLIC_BASE_URL:
        out["crop_url"] = f"{PUBLIC_BASE_URL}{url}"
    return out


# --------------------------------------------------------------------- 五个工具

async def search_impl(query: str, limit: int = 10) -> dict:
    limit = max(1, min(int(limit), 50))
    return _absolutize(await _call("POST", "/internal/mcp/search",
                                   body={"query": query, "limit": limit}))


async def ask_impl(question: str) -> dict:
    return _absolutize(await _call("POST", "/internal/mcp/ask",
                                   body={"question": question}))


async def get_evidence_impl(evidence_id: str) -> ToolResult:
    """证据 + **MCP 原生 image content**（裁图存在时）。

    图必须以 image content 回去，不能只给 URL：外部 agent 手里没有能取那张图
    的凭据，只给 URL 等于让"可复核"这条属性停在纸面上。
    """
    data = await _call("GET", f"/internal/mcp/evidence/{evidence_id}", allow_404=True)
    if data is None:
        return ToolResult(structured_content={"status": "not_found"}, is_error=True)
    payload = _absolutize(data["evidence"])
    content = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    crop = data.get("crop")
    if crop:
        content.append(ImageContent(type="image", data=crop["data_base64"],
                                    mime_type=crop.get("mime") or "image/png"))
    return ToolResult(content=content, structured_content=payload)


async def read_wiki_impl(value: str) -> dict:
    data = await _call("GET", "/internal/mcp/wiki", params={"value": value},
                       allow_404=True)
    return _absolutize(data) if data else {"status": "not_found"}


async def graph_neighbors_impl(value: str, depth: int = 1) -> dict:
    # **先验身份，再验参数。** 反过来的话，没有凭据的调用方能从"参数没问题"
    # 这件事上拿到一个成功形状的响应 —— 每个工具都必须是同一句话：没有身份，
    # 什么都不回答
    forwarded_identity()
    if not 1 <= depth <= 3:
        # 形状与搬迁前一致：越界不是错误结果，而是一个结构化状态
        return {"status": "invalid_depth"}
    data = await _call("GET", "/internal/mcp/graph/neighbors",
                       params={"value": value, "depth": depth}, allow_404=True)
    return _absolutize(data) if data else {"status": "not_found"}
