"""对 control-api 的内部调用。

语料侧需要控制面的两样东西，都是**它自己无权直接读的**（control schema 归 Go）：

  1. **稳定文件 URL**（`/files/{token}`）—— 模型网关下载原件用。
     这个 URL 的路径必须永远稳定：文档身份 `doc_hash` 在没有 `doc_id` 时
     会回退成 `sha256(file_url)`，URL 一变，网关的幂等与向量索引分块键
     全部失效（ADR #11/#12，这个项目踩过两次）。
  2. **actor 显示名** —— 见 `directory.py`。

两者都走服务凭据，且**只在内网可达**。
"""
import httpx

from ddp_corpus.config import settings
from ddp_corpus.errors import APIError


class ControlClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.service_token}",
                "X-DDP-Service": "corpus-api"}

    async def stable_file_url(self, *, organization_id: str, document_id: str,
                              object_key: str, mime: str) -> str:
        """取（或建）这份文档的稳定文件 URL。

        **必须幂等**：同一份文档反复调用要拿到同一个 URL，否则每次重解析
        都会换一个 doc_hash。control 侧按 (organization_id, document_id, scope)
        复用同一行来保证这件事。
        """
        try:
            resp = await self._http.post(
                f"{settings.control_url}/internal/file-grants",
                json={"organization_id": organization_id, "document_id": document_id,
                      "object_key": object_key, "mime": mime},
                headers=self._headers(), timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise APIError(502, f"control-api 不可达：{exc}", "upstream_error",
                           "control_unavailable") from exc
        if resp.status_code >= 300:
            raise APIError(502, f"control-api 拒绝签发文件凭证（{resp.status_code}）",
                           "upstream_error", "file_grant_failed")
        return resp.json()["url"]
