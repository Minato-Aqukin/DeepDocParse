"""MCP 平面 —— 单一复合工具 ask_document（决策 #7）。

传输：Streamable HTTP。对外经 DeepDocParse-Web/backend 代理（方案 A，key 鉴权在 backend），
本服务只对内网开放，调 gateway 时带 SERVICE_TOKEN。

设计要点：
- 工具越少、描述越清晰，agent 选择准确率越高 -> 只此一个工具
- 大文档解析耗时 -> "解析中即返回 + 请稍后重试"模式，不阻塞 MCP 同步调用
- 返回"证据 + 出处（页码/bbox/切片图）"而非只有结论；
  FastMCP 支持返回 Image content block，多模态 agent 原生看到像素
- v1 检索 = BM25/关键词；v2 换向量检索 —— 只改内部实现，工具签名永不变
"""
from fastmcp import FastMCP

mcp = FastMCP("DeepDocParse")

GATEWAY = "http://gateway:9000"  # TODO(M3): 走 env 配置


@mcp.tool()
async def ask_document(file_url: str, question: str) -> str:
    """对文档或图片提问，返回带出处的答案。

    支持 PDF/DOCX/PPTX/XLSX/图片的 URL。首次询问大文档时会触发解析，
    若返回"解析中"，请稍后用相同参数重试。

    TODO(M3) 内部路由：
    1. 判断输入类型（图片扩展名/单页 -> 直接走 gateway /v1/chat/completions，秒回）
    2. 文档：按 file_url 哈希查解析缓存
       - 无缓存 -> POST /v1/parse 提交 -> 返回 "解析中，预计N分钟，请稍后重试"
       - 有缓存 -> v1: BM25/关键词在 markdown 中检索相关块
                  （短文档直接全文作为证据返回，让 agent 自己的 LLM 综合）
    3. 命中块的 bbox -> 裁剪原图区域 -> VQA 验证（gateway /v1/chat/completions）
    4. 返回：答案/证据 + 出处（页码、坐标）+ 完整结果下载 URL
       （需要整份 Markdown 的场景由此覆盖，不设第二个工具）
    TODO(M4, v2): 步骤 2 检索换 embedding（gateway /v1/embeddings + Redis 向量索引）
    """
    raise NotImplementedError("TODO(M3)")


if __name__ == "__main__":
    # Streamable HTTP，供 backend 反向代理
    mcp.run(transport="http", host="0.0.0.0", port=9100)
