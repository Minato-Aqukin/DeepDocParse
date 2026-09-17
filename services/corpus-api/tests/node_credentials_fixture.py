"""节点凭证（DDP-NODE-CREDENTIAL v1）的测试装配。

**单测跑在生产的认证形态上**：入站请求带一张真的 Ed25519 凭证，接收方真的
验签、真的查信任记录、真的往 `federation_credential_nonces` 里记 jti。没有
"测试档位跳过验签"这种东西 —— 那样一来认证链路的任何回归都只会在真机上才炸。

三个替身，各自替掉的都是 **I/O**，判定逻辑一律走生产实现：

- `LocalControlSigner` 替掉 `ControlCredentialSigner`（本节点控制面的 HTTP
  签发接口）。它持有本节点私钥，按 `SignRequest` 填 issuer/时间/jti 并签名 ——
  与 Go 的 `handleIssueNodeCredential` 同一套规则，跨语言一致性由冻结夹具
  `tests/fixtures/node-credential-v1.json` 钉着，不在这里重复。
- `StaticPeerTrust` 替掉 `ControlPeerTrust`（查成员目录的 HTTP）。返回的记录
  形状与控制面 `GET /internal/federation/peer-keys/{id}` 相同。
- `PeerCaller` 替掉对端协调者的出站客户端：按路由表挑操作、按请求体算摘要、
  签一张只覆盖这一次请求的凭证。它**不复用** `PeerClient` 的实现，因为那份
  实现正是被测对象之一 —— 两边独立写才能在编码漂移时有一边红。

节点 id 的约束：签发方的 node_id 必须由它的公钥派生（`authenticate` 会复核），
所以 peer 的 id 只能算出来，不能随手写成 `node-bbbb…`。接收方（本节点）的 id
不受这条约束 —— 它只被拿去和绑定身份比对，所以既有用例里的 `node-ffff…` 照旧。
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ddp_core.application import node_credentials as nc

from ddp_corpus import node_identity
from ddp_corpus.config import settings
from ddp_corpus.federation_peers import _body_bytes
from ddp_corpus.routers.federation import ROUTE_OPERATIONS

#: jti 必须进程内唯一：原来用 `id(self)` 派生，短命调用方被回收后地址复用，
#: 同一秒内的两张凭证会撞 jti，第二张被重放账本当重放打掉（全量跑偶发红、
#: 单跑绿的假失败）。计数器只增不减，不复用。
_JTI_COUNTER = itertools.count(1)

#: 测试密钥由公开标签派生，**不是也从来不是任何部署密钥**（与冻结夹具同一约定）。
def key_for(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def node_id_for(label: str) -> str:
    return nc.node_id_for_public_key(public_key_b64(key_for(label)))


#: 一个"另一个中心"的身份：它给本节点发请求时是签发方，所以 id 必须是派生的。
PEER_LABEL = "ddp-test-peer-centre"
PEER_KEY = key_for(PEER_LABEL)
PEER_PUBLIC_KEY = public_key_b64(PEER_KEY)
PEER_NODE_ID = node_id_for(PEER_LABEL)

#: 第二个远端中心，用来验"另一个节点的同名主体读不到我的东西"。
OTHER_LABEL = "ddp-test-other-centre"
OTHER_KEY = key_for(OTHER_LABEL)
OTHER_PUBLIC_KEY = public_key_b64(OTHER_KEY)
OTHER_NODE_ID = node_id_for(OTHER_LABEL)

#: 本节点（作为协调者出站时的签发方）的密钥。
LOCAL_LABEL = "ddp-test-local-centre"
LOCAL_KEY = key_for(LOCAL_LABEL)
LOCAL_PUBLIC_KEY = public_key_b64(LOCAL_KEY)


def trust_record(node_id: str, public_key: str, *, organization_id: str,
                 authority_node_id: str, state: str = "approved", revision: int = 1) -> dict:
    """与控制面 `GET /internal/federation/peer-keys/{node_id}` 同形状的信任记录。"""
    raw = base64.b64decode(public_key)
    return {"node_id": node_id, "state": state, "public_key": public_key,
            "key_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "organization_id": organization_id, "authority_node_id": authority_node_id,
            "revision": revision}


class StaticPeerTrust:
    """内存成员目录。`calls` 记下查了谁，用来断言缓存与"撤销多久生效"。"""

    def __init__(self, records: dict[str, dict] | None = None):
        self.records = dict(records or {})
        self.calls: list[str] = []
        self.unavailable = False

    async def trust(self, node_id: str):
        self.calls.append(node_id)
        if self.unavailable:
            from ddp_core.application.ports import ApplicationError
            raise ApplicationError("credential_unavailable", "control-api unreachable")
        return self.records.get(node_id)


class LocalControlSigner:
    """本节点控制面的签发端点替身：填 issuer / 时间 / jti 并用本节点私钥签名。

    `issue` 的入参与返回形状必须与真控制面一致（`SignRequest` / `SignedCredential`），
    否则出站客户端对不上 —— 形状本身由 `ddp_core` 的 `sign_request` 先校验过一遍。
    """

    def __init__(self, *, issuer_node_id: str, key: Ed25519PrivateKey | None = None,
                 now=time.time, ttl_override: int | None = None):
        self.issuer_node_id = issuer_node_id
        self.key = key or LOCAL_KEY
        self.now = now
        self.ttl_override = ttl_override
        self.requests: list[dict] = []
        self.failure: Exception | None = None
        self.closed = False

    async def issue(self, sign_request: dict) -> dict:
        self.requests.append(sign_request)
        if self.failure is not None:
            raise self.failure
        issued = int(self.now())
        ttl = self.ttl_override or int(sign_request["ttl_seconds"])
        claims = {
            "schema": nc.SCHEMA, "alg": nc.ALG, "issuer_node_id": self.issuer_node_id,
            "audience_node_id": sign_request["audience_node_id"],
            "actor": dict(sign_request["actor"]), "operation": sign_request["operation"],
            "constraints": dict(sign_request["constraints"]),
            "request": dict(sign_request["request"]),
            "issued_at": issued, "expires_at": issued + ttl,
            "jti": base64.urlsafe_b64encode(
                hashlib.sha256(f"{len(self.requests)}:{issued}:{next(_JTI_COUNTER)}".encode())
                .digest()[:16]).rstrip(b"=").decode("ascii"),
        }
        token = nc.encode(claims, self.key.sign(nc.signing_input(claims)))
        return {"credential": token, "issuer_node_id": self.issuer_node_id,
                "jti": claims["jti"], "expires_at": claims["expires_at"]}

    async def aclose(self) -> None:
        self.closed = True


# ------------------------------------------------------------------ 入站调用方

def _operation_for(method: str, path: str) -> str:
    """把具体 URL 映射回路由模板上的操作 —— 与凭证里 `request.path` 的口径一致。

    凭证绑定的是**路由路径**（带 `{}` 的那个），不是具体 id：否则每个 id 都要
    在契约里登记一遍。模板匹配按段比对，段数不同直接不匹配。
    """
    wanted = [segment for segment in path.split("/") if segment]
    for (route_method, template), operation in ROUTE_OPERATIONS.items():
        if route_method != method:
            continue
        parts = [segment for segment in template.split("/") if segment]
        if len(parts) != len(wanted):
            continue
        if all(part.startswith("{") or part == got for part, got in zip(parts, wanted)):
            return operation
    raise AssertionError(f"no node credential operation for {method} {path}")


class PeerCaller:
    """一个远端中心对本节点的调用方。每次调用现签一张凭证。

    `constraints` 默认从请求体里取（`root_task_id` / `step_id` / `scope_ref` /
    `task_spec_digest`），这与协调者出站时的行为一致；要测"凭证范围不覆盖这次
    请求"就显式传一个不同的值进来。
    """

    def __init__(self, client, *, issuer_node_id: str = PEER_NODE_ID,
                 key: Ed25519PrivateKey | None = None, audience_node_id: str = "",
                 organization_id: str = "org-peer", subject: str = "user-remote",
                 kind: str = "user", ttl_seconds: int = 60, now=time.time):
        self.client = client
        self.issuer_node_id = issuer_node_id
        self.key = key or PEER_KEY
        self.audience_node_id = audience_node_id
        self.actor = {"organization_id": organization_id, "subject": subject, "kind": kind}
        self.ttl_seconds = ttl_seconds
        self.now = now
        self.tokens: list[str] = []

    @property
    def actor_id(self) -> str:
        """这个远端主体在本节点的 actor id（`peer-…`）。"""
        return nc.peer_actor_id({"issuer_node_id": self.issuer_node_id, "actor": self.actor})

    def credential(self, method: str, path: str, *, body: bytes = b"",
                   operation: str | None = None, constraints: dict | None = None,
                   audience: str | None = None, issued_at: int | None = None,
                   ttl_seconds: int | None = None, jti: str | None = None,
                   route_path: str | None = None) -> str:
        operation = operation or _operation_for(method, route_path or path)
        issued = issued_at if issued_at is not None else int(self.now())
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        claims = {
            "schema": nc.SCHEMA, "alg": nc.ALG, "issuer_node_id": self.issuer_node_id,
            "audience_node_id": audience or self.audience_node_id,
            "actor": dict(self.actor), "operation": operation,
            "constraints": dict(constraints or {}),
            "request": {"method": method, "path": path, "body_digest": nc.body_digest(body)},
            "issued_at": issued, "expires_at": issued + ttl,
            "jti": jti or base64.urlsafe_b64encode(
                hashlib.sha256(f"{path}:{issued}:{next(_JTI_COUNTER)}".encode())
                .digest()[:16]).rstrip(b"=").decode("ascii"),
        }
        token = nc.encode(claims, self.key.sign(nc.signing_input(claims)))
        self.tokens.append(token)
        return token

    def _constraints(self, body: dict | None, extra: dict | None) -> dict:
        if extra is not None:
            return extra
        body = body or {}
        out = {"root_task_id": str(body.get("root_task_id") or "root-1")}
        for name in ("step_id", "scope_ref", "task_spec_digest"):
            value = body.get(name)
            if value:
                out[name] = str(value)
        return out

    async def request(self, method: str, path: str, *, json_body: dict | None = None,
                      constraints: dict | None = None, headers: dict | None = None,
                      params: dict | None = None, credential: str | None = None,
                      target: bool = True, **credential_over):
        body = _body_bytes(json_body)
        sent = dict(headers or {})
        if json_body is not None:
            sent["Content-Type"] = "application/json"
        if target:
            sent.setdefault("X-DDP-Target-Node", self.audience_node_id)
        token = credential if credential is not None else self.credential(
            method, path, body=body,
            constraints=self._constraints(json_body, constraints), **credential_over)
        if token:
            sent[nc.HEADER] = token
        return await self.client.request(method, path, content=body or None,
                                         headers=sent, params=params)

    async def post(self, path: str, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def get(self, path: str, **kwargs):
        return await self.request("GET", path, **kwargs)


# ------------------------------------------------------------------- 一把装配

def install(monkeypatch, app, *, node_id: str, organization_id: str,
            peers: tuple[tuple[str, str], ...] = ((PEER_NODE_ID, PEER_PUBLIC_KEY),
                                                  (OTHER_NODE_ID, OTHER_PUBLIC_KEY)),
            ) -> StaticPeerTrust:
    """把本节点身份钉成 `node_id`，并给 app 装上信任目录替身。

    返回目录对象，用例可以往里加/改成员（pending / revoked / 换公钥）。

    进程级的 `node_identity` 状态由 conftest 的 autouse 夹具每条用例重置，
    所以这里直接写就行 —— 不重置的话，一条用例把身份标成 mismatch 会让
    后面所有用例莫名 503，而且顺序一换现象就变。
    """
    monkeypatch.setattr(settings, "bundle_node_id", node_id)
    node_identity.reset()
    node_identity.bind_static_for_tests(node_id)
    trust = StaticPeerTrust({
        peer_node: trust_record(peer_node, public_key, organization_id=organization_id,
                                authority_node_id=node_id)
        for peer_node, public_key in peers})
    monkeypatch.setattr(app.state, "peer_trust", trust, raising=False)
    return trust


def caller(client, *, audience_node_id: str, **over) -> PeerCaller:
    return PeerCaller(client, audience_node_id=audience_node_id, **over)


def signer(issuer_node_id: str, **over) -> LocalControlSigner:
    return LocalControlSigner(issuer_node_id=issuer_node_id, **over)


def dumps(body: dict) -> str:
    """与出站客户端一致的正文编码（签名覆盖的就是这串字节）。"""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
