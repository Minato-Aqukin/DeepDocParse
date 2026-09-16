"""节点凭证的语料侧 I/O 适配：出站申请签发、入站查信任记录、记 jti、组装远端主体。

判定规则全部在 `ddp_core.application.node_credentials`（零 I/O，Go 签发方用同一组
冻结夹具钉着）；这里只做它不能做的三件事 —— 向本节点控制面发 HTTP、写重放账本、
把验过的凭证变成一个本地 `Actor`。契约与验证顺序：
`packages/contracts/ddp/node-credential-format.md`。

## 远端主体（T32）

凭证通过 ≠ 资源权限。执行者构造的是 `Actor(kind="peer", role="viewer")`：

- `id` 由 (issuer, 发行者组织, kind, subject) 派生、带 `peer-` 前缀 —— 永远不等于
  本地任何用户，所以远端同名 `alice` 继承不了本地 `alice` 的私有资产；
- `organization_id` 取**本节点控制面批准该 issuer 的成员记录**，不取凭证里发行者自报的组织；
- 只读 viewer：本组织已发布的资源可见，任何人的私有资源不可见，按本地 ACL 同形 404。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx
from fastapi import Depends, Header, Request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import node_credentials as nc
from ddp_core.application.ports import ApplicationError
from ddp_core.node_signature import ed25519_verify

from ddp_corpus import node_identity
from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.federation_models import FederationCredentialNonce

CREDENTIALS_PATH = "/internal/federation/node-credentials"
PEER_KEYS_PATH = "/internal/federation/peer-keys/"

_STATUS = {
    "credential_invalid": (401, "authentication_error"),
    "credential_expired": (401, "authentication_error"),
    "credential_replayed": (401, "authentication_error"),
    "credential_audience_mismatch": (401, "authentication_error"),
    "node_unknown": (401, "authentication_error"),
    "node_revoked": (401, "authentication_error"),
    "credential_operation_denied": (403, "permission_error"),
    "credential_scope_denied": (403, "permission_error"),
    "node_identity_mismatch": (503, "server_error"),
    "node_identity_unavailable": (503, "server_error"),
    "credential_unavailable": (503, "server_error"),
}


def credential_error(exc: ApplicationError) -> APIError:
    status, kind = _STATUS.get(exc.code, (401, "authentication_error"))
    # 消息只说哪条规则，不回显凭证里的任何值。
    return APIError(status, str(exc), kind, exc.code)


class CredentialUnavailable(RuntimeError):
    """本节点控制面没能签出凭证。`code` 是契约错误码；消息里永远没有凭证。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- 出站

class CredentialSigner(Protocol):
    async def issue(self, sign_request: dict) -> dict: ...

    async def aclose(self) -> None: ...


def _service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.service_token}", "X-DDP-Service": "corpus-api"}


class ControlCredentialSigner:
    """向**本节点**控制面申请一张出站凭证。私钥从不离开控制面（不变式 5）。"""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._owned = http is None
        self._http = http or httpx.AsyncClient(timeout=5.0, trust_env=False,
                                               follow_redirects=False)

    async def issue(self, sign_request: dict) -> dict:
        try:
            response = await self._http.post(
                settings.control_url.rstrip("/") + CREDENTIALS_PATH, json=sign_request,
                headers=_service_headers(), timeout=5.0, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise CredentialUnavailable(
                "credential_unavailable",
                f"control-api unreachable for credential issuance ({type(exc).__name__})") \
                from None
        if response.status_code != 200:
            code = "credential_unavailable"
            try:
                code = str(response.json()["error"]["code"]) or code
            except (ValueError, KeyError, TypeError):
                pass
            # 控制面拒签（node_unknown / node_revoked / scope）按它的码报出来：
            # "本节点没把对端批准"与"控制面挂了"是两件事，都不是对端没有资料。
            raise CredentialUnavailable(code, f"control-api refused to issue ({response.status_code})")
        try:
            body = response.json()
            credential, issuer = body["credential"], body["issuer_node_id"]
        except (ValueError, KeyError, TypeError):
            raise CredentialUnavailable("credential_unavailable",
                                        "control-api returned a malformed credential") from None
        if not isinstance(credential, str) or not credential or not isinstance(issuer, str):
            raise CredentialUnavailable("credential_unavailable",
                                        "control-api returned a malformed credential")
        try:
            node_identity.observe_authority(issuer)
        except APIError as exc:
            raise CredentialUnavailable(exc.code, "issued credential names another node") from None
        return body

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()


def actor_ref(actor: Actor) -> dict:
    """发行者组织内的主体引用。**不带角色、不带 API key 或其 id**（§8.4）。"""
    return {"organization_id": actor.organization_id,
            "subject": actor.principal_id or actor.id, "kind": actor.kind}


# --------------------------------------------------------------------- 入站

class PeerTrustSource(Protocol):
    async def trust(self, node_id: str) -> dict | None: ...


class ControlPeerTrust:
    """向本节点控制面查签发节点的信任记录；只缓存 approved，且有硬上界。"""

    def __init__(self, http: httpx.AsyncClient | None = None, *, clock=time.monotonic):
        self._http = http
        self._clock = clock
        self._cache: dict[str, tuple[dict, float]] = {}

    async def trust(self, node_id: str) -> dict | None:
        ttl = settings.federation_peer_key_cache_seconds
        cached = self._cache.get(node_id)
        if cached is not None and ttl > 0 and self._clock() - cached[1] < ttl:
            return cached[0]
        self._cache.pop(node_id, None)
        client = self._http or httpx.AsyncClient(timeout=5.0, trust_env=False,
                                                 follow_redirects=False)
        try:
            response = await client.get(
                settings.control_url.rstrip("/") + PEER_KEYS_PATH + node_id,
                headers=_service_headers(), timeout=5.0, follow_redirects=False)
        except httpx.HTTPError:
            raise ApplicationError("credential_unavailable",
                                   "control-api unreachable for peer trust") from None
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ApplicationError("credential_unavailable",
                                   f"control-api peer trust lookup failed ({response.status_code})")
        try:
            record = response.json()
        except ValueError:
            raise ApplicationError("credential_unavailable",
                                   "control-api returned a malformed trust record") from None
        if not isinstance(record, dict):
            raise ApplicationError("credential_unavailable",
                                   "control-api returned a malformed trust record")
        if record.get("state") == "approved" and ttl > 0:
            self._cache[node_id] = (record, self._clock())
        return record


def _trust_source(request: Request) -> PeerTrustSource:
    source = getattr(request.app.state, "peer_trust", None)
    if source is None:
        source = ControlPeerTrust(getattr(request.app.state, "http", None))
        request.app.state.peer_trust = source
    return source


async def consume_jti(session: AsyncSession, claims: dict, *, now: float) -> None:
    """持久记下这张凭证用过了。唯一约束仲裁并发重放。"""
    session.add(FederationCredentialNonce(
        jti=claims["jti"], issuer_node_id=claims["issuer_node_id"],
        operation=claims["operation"],
        expires_at=datetime.fromtimestamp(claims["expires_at"], timezone.utc),
        created_at=datetime.fromtimestamp(now, timezone.utc)))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApplicationError("credential_replayed", "credential has already been used") from None


async def sweep_nonces(session: AsyncSession, *, now: datetime) -> int:
    """删除已经过期的 jti。过期凭证本来就过不了时间窗，留着它们只占地方。"""
    result = await session.execute(delete(FederationCredentialNonce).where(
        FederationCredentialNonce.expires_at < now))
    await session.commit()
    return result.rowcount or 0


@dataclass(frozen=True)
class PeerContext:
    """一次已认证的节点对节点请求。`claims` 在共享口令档位下为 None。"""

    actor: Actor
    claims: dict | None = None
    trust: dict | None = None

    @property
    def issuer_node_id(self) -> str | None:
        return None if self.claims is None else self.claims["issuer_node_id"]

    def require(self, **observed) -> None:
        """请求体里的值：凭证约束必须逐字相等（缺省不是通配）。"""
        if self.claims is None:
            return
        try:
            nc.require_constraints(self.claims, **observed)
        except ApplicationError as exc:
            raise credential_error(exc) from None

    def within(self, **row) -> None:
        """被读写的那一行：凭证带了哪个约束就比哪个；root_task_id 恒比。"""
        if self.claims is None:
            return
        present = self.claims.get("constraints") or {}
        observed = {name: value for name, value in row.items()
                    if name == "root_task_id" or name in present}
        self.require(**observed)


def peer_actor(claims: dict, trust: dict, *, request_id: str = "") -> Actor:
    return Actor(id=nc.peer_actor_id(claims), kind="peer",
                 organization_id=str(trust["organization_id"]), role="viewer",
                 request_id=request_id)


async def _shared_token_context(request: Request) -> PeerContext:
    """开发档位：旧的服务凭据 + actor 头 + 共享口令。只在显式配置下可达。"""
    import hmac

    from ddp_corpus.deps import current_actor, require_gateway_credentials

    await require_gateway_credentials(request.headers.get("authorization"))
    headers = request.headers
    actor = await current_actor(
        request, None, headers.get("x-ddp-organization"), headers.get("x-ddp-actor"),
        headers.get("x-ddp-actor-kind"), headers.get("x-ddp-role"),
        headers.get("x-ddp-api-key"), headers.get("x-ddp-user"), headers.get("x-request-id"))
    configured = settings.federation_peer_token or ""
    presented = headers.get("x-ddp-peer-token") or ""
    if not configured or not presented or not hmac.compare_digest(configured, presented):
        raise APIError(401, "invalid or missing peer credentials", "authentication_error",
                       "peer_unauthenticated")
    return PeerContext(actor=actor)


def peer_context(operation: str):
    """FastAPI 依赖工厂：本端点的凭证操作写死在路由上（与契约 x-ddp-node-credential-operation 一致）。"""
    if operation not in nc.NODE_CREDENTIAL_OPERATION_VALUES:
        raise ValueError(f"unknown node credential operation {operation}")

    async def dependency(request: Request, session: AsyncSession = Depends(get_session),
                         x_ddp_node_credential: str | None = Header(default=None)
                         ) -> PeerContext:
        if node_identity.shared_token_mode():
            return await _shared_token_context(request)
        if not x_ddp_node_credential:
            raise APIError(401, "a node credential is required", "authentication_error",
                           "peer_unauthenticated")
        node = node_identity.local_node_id()
        now = time.time()
        body = await request.body()
        try:
            decoded = nc.inspect(
                x_ddp_node_credential, audience_node_id=node, now=now, operation=operation,
                method=request.method, path=request.url.path, body_digest=nc.body_digest(body))
            trust = await _trust_source(request).trust(decoded.claims["issuer_node_id"])
            if isinstance(trust, dict) and trust.get("authority_node_id") is not None:
                node_identity.observe_authority(trust.get("authority_node_id"))
            claims = nc.authenticate(decoded, trust=trust, verify_signature=ed25519_verify)
            await consume_jti(session, claims, now=now)
        except ApplicationError as exc:
            raise credential_error(exc) from None
        return PeerContext(actor=peer_actor(claims, trust,
                                            request_id=request.headers.get("x-request-id") or ""),
                           claims=claims, trust=trust)

    return dependency
