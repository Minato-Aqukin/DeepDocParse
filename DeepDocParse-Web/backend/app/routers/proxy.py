"""对外 API 与 MCP 代理（方案 A：所有外部流量经 backend）。

第三方开发者视角的入口：
  POST /v1/parse, GET /v1/parse/{id}[, /result]   （异步任务式，同 service 契约）
  POST /v1/chat/completions                        （OpenAI 协议 -> 视觉问答）
  /mcp                                             （Streamable HTTP 反代 -> mcp-server）

统一流程：验 API key -> 查额度/限流 -> 换 SERVICE_TOKEN 转发 -> 记 usage_records。
未来流量大时演进方案 B：backend 签短期 JWT，service 本地验签（见 ARCHITECTURE.md 决策 #3）。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def api_proxy(path: str, request: Request):
    """TODO: 验 key -> 额度 -> httpx 流式转发 DeepDocParse -> 计量"""
    raise HTTPException(status_code=501, detail="TODO")


@router.api_route("/mcp{path:path}", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(path: str, request: Request):
    """TODO: 验 key -> 反代 mcp-server:9100（注意 SSE/Streamable HTTP 透传）"""
    raise HTTPException(status_code=501, detail="TODO")
