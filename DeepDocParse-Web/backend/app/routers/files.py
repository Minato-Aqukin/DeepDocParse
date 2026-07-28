"""稳定文件 URL：/files/{token} -> 原件。

刻意不做 JWT/key 鉴权 —— token 本身就是凭证（32 字节随机、可撤销、可设过期）。
理由：service 要下载这个 URL，而它只持有 SERVICE_TOKEN；MCP 平面的 ask_document
更是只能拿到一个裸 URL。用预签名 URL 则会过期、且每次签名不同，导致
service 侧的幂等与向量索引分块键失效（M4a 的向量检索在生产就白做了）。
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_storage
from app.errors import APIError
from app.models import Document, FileToken, as_aware
from app.storage import Storage

router = APIRouter()

# 只有这些类型允许 inline 预览（前端 iframe/PDF 渲染要用）。HTML/SVG/XML 一律不在内 ——
# 它们能在本站同源执行脚本。
INLINE_SAFE_MIMES = {
    "application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif", "text/plain",
}


@router.get("/files/{token}")
async def get_file(token: str, session: AsyncSession = Depends(get_session),
                   storage: Storage = Depends(get_storage)):
    row = await session.get(FileToken, token)
    if row is None or row.revoked:
        raise APIError(404, "file not found", "invalid_request_error", "file_not_found")
    if row.expires_at is not None and as_aware(row.expires_at) < datetime.now(UTC):
        raise APIError(404, "file link expired", "invalid_request_error", "file_expired")

    document = await session.get(Document, row.document_id)
    if document is None or document.deleted_at is not None or not document.object_key:
        raise APIError(404, "file not found", "invalid_request_error", "file_not_found")
    try:
        data = await storage.get(document.object_key)
    except Exception:
        raise APIError(404, "file object missing", "invalid_request_error", "file_not_found")

    # 安全要点：document.mime 来自上传方的 multipart 头，完全可控。原样 inline 回去
    # 等于允许上传 text/html 并在本站同源执行脚本（token 一转发就能偷走别人
    # localStorage 里的 JWT）。因此：白名单之外一律当二进制附件下载，并禁止嗅探。
    inline = document.mime in INLINE_SAFE_MIMES
    headers = {
        "Content-Disposition": ("inline" if inline else "attachment")
        + f'; filename="{document.filename}"',
        # nosniff 是这里的关键：配上确定的 Content-Type，浏览器不会把内容重新猜成 HTML
        "X-Content-Type-Options": "nosniff",
    }
    if not inline:
        # 严格 CSP 只加在非白名单分支。不给白名单加 sandbox 是有意为之：
        # 白名单里的类型（PDF/图片/纯文本）配上确定的 Content-Type + nosniff 本就无法
        # 在本站源里执行脚本，而 sandbox/default-src 有挡住浏览器内置 PDF 阅读器的风险
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return Response(content=data,
                    media_type=document.mime if inline else "application/octet-stream",
                    headers=headers)
