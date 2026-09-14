"""App 侧协调者客户端：只依赖冻结的 `federation-tasks-v1.yaml` 形状。

安全边界（P5-INTERFACES-v3 §5、工作区铁律 8）：

- 服务凭据只进 `Authorization` 头，绝不进入 URL、本地状态、日志或异常消息；
- 出站 httpx 固定 `trust_env=False`、`follow_redirects=False`，响应体有字节上限；
- 写请求结果未知时抛 `CenterOutcomeUnknown`，由调用方用 `task()` / `reconcile`
  对账，绝不在客户端内部自动重放。
"""
from __future__ import annotations

import dataclasses
import json
from urllib.parse import quote, urlsplit

import httpx

from ddp_core.application.plans import digest, reject

RESPONSE_BYTES_LIMIT = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def validate_endpoint(endpoint, allow_loopback=False):
    """与 `plans.validate_transport` 同一套端点规则，另开显式回环测试口。"""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        reject("policy_denied", "center endpoint must be a bounded string")
    if any(c.isspace() or ord(c) < 32 for c in endpoint):
        reject("policy_denied", "center endpoint cannot contain whitespace or control characters")
    try:
        url = urlsplit(endpoint)
        if not url.hostname or url.username or url.password or url.query or url.fragment or endpoint.endswith("/"):
            raise ValueError
        if url.scheme != "https" and not (
            url.scheme == "http" and allow_loopback and url.hostname in LOOPBACK_HOSTS
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        reject(
            "policy_denied",
            "center endpoint must be an exact HTTPS origin (HTTP only for literal 127.0.0.1/[::1] "
            "with allow_loopback), without userinfo, query, fragment or trailing slash",
        )


@dataclasses.dataclass(frozen=True)
class CenterConfig:
    endpoint: str
    credential: str = dataclasses.field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT
    allow_loopback: bool = False

    def __post_init__(self):
        validate_endpoint(self.endpoint, self.allow_loopback)
        if type(self.credential) is not str or not self.credential or len(self.credential) > 4096:
            reject("policy_denied", "a center service credential is required")
        if type(self.allow_loopback) is not bool:
            reject("policy_denied", "allow_loopback must be boolean")
        if (
            type(self.timeout_seconds) not in (int, float)
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= MAX_TIMEOUT
        ):
            reject("policy_denied", "timeout_seconds must be a bounded positive number")


class CenterFault(RuntimeError):
    """中心拒绝或不可用；只带机器码与状态，绝不含凭据或 URL。"""

    def __init__(self, code, status=0, retryable=False):
        super().__init__("center fault %s (status=%s)" % (code, status))
        self.code = code
        self.status = status
        self.retryable = retryable


class CenterOutcomeUnknown(CenterFault):
    """写请求可能已送达但响应丢失/歧义；不得自动重放，先对账。"""

    def __init__(self, status=0):
        super().__init__("outcome_unknown", status, False)


def _stable_key(prefix, value):
    return prefix + digest(value).removeprefix("sha256:")[:32]


class CenterFederationClient:
    """协调者端点的最小异步客户端；跨节点凭据由调用方以 actor 头上报。"""

    def __init__(self, config, *, transport=None, actor_headers=None):
        if not isinstance(config, CenterConfig):
            reject("invalid_plan", "a validated CenterConfig is required")
        self.config = config
        # 只透传 X-DDP-* 调用者上下文，杜绝覆盖 Authorization/Host 等敏感头。
        self.actor_headers = {
            str(name): str(value)
            for name, value in (actor_headers or {}).items()
            if str(name).lower().startswith("x-ddp-")
        }
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def _bounded_body(self, response):
        total = 0
        chunks = []
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > RESPONSE_BYTES_LIMIT:
                raise CenterFault("response_too_large", response.status_code, False)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _transport_fault(exc, *, write):
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return CenterFault("unreachable", 0, True)
        if write:
            return CenterOutcomeUnknown(0)
        return CenterFault("transport_error", 0, True)

    @staticmethod
    def _error_fault(status, body):
        code = None
        if body:
            try:
                error = json.loads(body).get("error")
            except (ValueError, AttributeError):
                error = None
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
        return CenterFault(code or "http_%d" % status, status, status >= 500 or status == 429)

    async def _request(
        self, method, path, *, payload=None, params=None, idempotency_key=None,
        accepted=(200,), write=False,
    ):
        headers = dict(self.actor_headers)
        headers["Authorization"] = "Bearer " + self.config.credential
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
                reject("invalid_key", "an idempotency key of 1-128 characters is required")
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with self._client.stream(
                method, self.config.endpoint + path, json=payload, params=params, headers=headers
            ) as response:
                status = response.status_code
                body = await self._bounded_body(response)
        except CenterFault:
            raise
        except httpx.HTTPError as exc:
            raise self._transport_fault(exc, write=write) from None
        if status not in accepted:
            raise self._error_fault(status, body)
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise CenterFault("invalid_response", status, False) from None

    async def create_intent(self, task_spec, exploration_consent, scope_manifest=None, *, idempotency_key=None):
        """持久化任务需求与已批准的探索许可；稳定幂等键让丢响应后的显式重试不造第二个 intent。"""
        body = {"task_spec": task_spec, "exploration_consent": exploration_consent}
        if scope_manifest is not None:
            body["scope_manifest"] = scope_manifest
        return await self._request(
            "POST", "/api/v1/task-intents", payload=body,
            idempotency_key=idempotency_key or _stable_key("intent-", body),
            accepted=(200, 201), write=True,
        )

    async def create_plan(self, root_task_id):
        """按 root_task_id 触发中心 Probe/规划；200 已有计划，202 规划未定可稍后对账。"""
        return await self._request(
            "POST", "/api/v1/task-plans", payload={"root_task_id": root_task_id},
            idempotency_key=_stable_key("plan-", {"root_task_id": root_task_id}),
            accepted=(200, 202), write=True,
        )

    async def approve(self, root_task_id, plan_digest, execution_consent):
        """批准精确的中心计划修订 + 执行许可；同键重提由中心幂等。"""
        body = {"plan_digest": plan_digest, "execution_consent": execution_consent}
        return await self._request(
            "POST", "/api/v1/task-plans/%s/approve" % quote(root_task_id, safe=""), payload=body,
            idempotency_key=_stable_key("approve-", [root_task_id, plan_digest]),
            accepted=(200,), write=True,
        )

    async def submit_task(self, root_task_id, plan_digest, idempotency_key):
        """以调用方稳定幂等键受理；丢响应抛 outcome_unknown，用 task() 对账后可用同键显式重提。"""
        return await self._request(
            "POST", "/api/v1/tasks",
            payload={"root_task_id": root_task_id, "plan_digest": plan_digest},
            idempotency_key=idempotency_key, accepted=(200, 202), write=True,
        )

    async def task(self, root_task_id):
        return await self._request("GET", "/api/v1/tasks/%s" % quote(root_task_id, safe=""))

    async def coverage(self, root_task_id):
        return await self._request("GET", "/api/v1/tasks/%s/coverage" % quote(root_task_id, safe=""))

    async def events(self, root_task_id, after=0):
        if type(after) is not int or after < 0:
            reject("invalid_plan", "events cursor must be a nonnegative integer")
        return await self._request(
            "GET", "/api/v1/tasks/%s/events" % quote(root_task_id, safe=""), params={"after": after}
        )

    async def resume(self, root_task_id):
        return await self._request(
            "POST", "/api/v1/tasks/%s/resume" % quote(root_task_id, safe=""), payload={},
            idempotency_key=_stable_key("resume-", root_task_id), accepted=(200, 202), write=True,
        )

    async def cancel(self, root_task_id):
        return await self._request(
            "POST", "/api/v1/tasks/%s/cancel" % quote(root_task_id, safe=""), payload={},
            idempotency_key=_stable_key("cancel-", root_task_id), write=True,
        )

    async def delivery(self, delivery_id):
        """读取交付字节（有界 JSON）：`{delivery_id, root_task_id, state,
        result_manifest_digest, result, expires_at}`。

        未确认且过 TTL 时中心回 410 `delivery_expired` -> 抛
        `CenterFault(code="delivery_expired", status=410)`；调用方据此把本地
        状态标成 expired，绝不显示"已保存本地"。响应 `Cache-Control: no-store`
        由中心保证，客户端不做本地缓存。
        """
        return await self._request(
            "GET", "/api/v1/deliveries/%s" % quote(delivery_id, safe=""), accepted=(200,)
        )

    async def ack_delivery(self, delivery_id, result_manifest_digest):
        """确认本地已校验并持久的结果；中心幂等，同键重试安全。"""
        body = {"result_manifest_digest": result_manifest_digest}
        return await self._request(
            "POST", "/api/v1/deliveries/%s/ack" % quote(delivery_id, safe=""), payload=body,
            idempotency_key=_stable_key("ack-", [delivery_id, result_manifest_digest]),
            accepted=(200, 204), write=True,
        )
