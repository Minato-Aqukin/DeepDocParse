"""DeepDocParse（service）客户端 —— 唯一依赖面是 ../../DeepDocParse/openapi.yaml。

约定：
- 一律 Bearer SERVICE_TOKEN（service 不感知用户，用户鉴权在本层做完）
- trust_env=False：本机 SOCKS 代理会污染 localhost 调用（见根 CLAUDE.md 陷阱 2）
"""
import hashlib
import hmac
import secrets

import httpx

from ddp_corpus.config import settings


def callback_token(job_id: str) -> str:
    """内部解析回调的 per-job HMAC 令牌。

    以 `service_token` 为 key 对 job id 做 HMAC-SHA256，拼在内部
    `callback_url` 的 `?token=` 上（网关按 scheme+host+port+path 做
    allowlist，query 不透明透传，所以令牌能原样穿过去）。
    验证端（`routers/internal.py`）用 `verify_callback_token` —— 令牌形状两边
    共用，改一边必须同步另一边。
    """
    return hmac.new(settings.service_token.encode(), str(job_id).encode(),
                    hashlib.sha256).hexdigest()


def verify_callback_token(job_id: str, token: str | None) -> bool:
    """`callback_token(job_id)` 的常量时间验证。空令牌一律拒绝。"""
    if not token:
        return False
    return secrets.compare_digest(callback_token(job_id), token)


class ServiceError(RuntimeError):
    """service 返回了非预期状态码。"""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"service returned {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class ServiceClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.service_token}"}

    async def submit_parse(self, file_url: str, doc_id: str, callback_url: str | None = None,
                           engine: str = "", options: dict | None = None) -> str:
        """POST /v1/parse -> task_id。队列满时 service 返回 429，原样抛给调用方处理。"""
        payload: dict = {"file_url": file_url, "doc_id": doc_id,
                         "engine": engine or settings.default_parse_engine}
        if options:
            payload["options"] = options
        if callback_url:
            payload["callback_url"] = callback_url
        resp = await self._http.post(f"{settings.service_url}/v1/parse",
                                     json=payload, headers=self.headers)
        if resp.status_code != 202:
            raise ServiceError(resp.status_code, resp.text)
        return resp.json()["task_id"]

    async def get_status(self, service_task_id: str) -> dict:
        """GET /v1/parse/{id} -> {task_id, status, progress, error}。"""
        resp = await self._http.get(f"{settings.service_url}/v1/parse/{service_task_id}",
                                    headers=self.headers)
        if resp.status_code != 200:
            raise ServiceError(resp.status_code, resp.text)
        return resp.json()

    async def get_result(self, service_task_id: str, *, max_bytes: int = 64 * 1024 * 1024) -> dict | None:
        """GET /v1/parse/{id}/result -> {markdown, layout_json, images}。

        409 = 结果尚未归档完成（service 侧 worker 还在跑），返回 None 让调用方稍后重试；
        404 = 已过 24h TTL 被清理，抛错由对账逻辑落 failed。
        体上限 max_bytes：结果体含 base64 图片，无界 resp.json() 即 OOM（不变式 6）。
        超限抛 ServiceError(413) —— 调用方按可重试处理（release_job），不落终态。
        """
        resp = await self._http.get(f"{settings.service_url}/v1/parse/{service_task_id}/result",
                                    headers=self.headers)
        if resp.status_code == 409:
            return None
        if resp.status_code != 200:
            raise ServiceError(resp.status_code, resp.text)
        length = resp.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > max_bytes:
            raise ServiceError(413, f"service result exceeds {max_bytes} bytes")
        body = b""
        async for piece in resp.aiter_bytes():
            body += piece
            if len(body) > max_bytes:
                raise ServiceError(413, f"service result exceeds {max_bytes} bytes")
        import json as _json

        try:
            return _json.loads(body.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError(resp.status_code, f"invalid service result: {exc}") from exc


def new_http_client() -> httpx.AsyncClient:
    # 读超时放宽：结果体含 base64 图片，长文档可能很大
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), trust_env=False)
