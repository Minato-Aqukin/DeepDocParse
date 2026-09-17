"""P5 协调者的 peer 目录与出站客户端（P5-INTERFACES-v3 §5）。

职责边界：

- **目录只来自管理员登记**（`FEDERATION_PEERS`）。没登记的 node_id
  一律 `PeerUnavailable`，一个请求都不发 —— 不解析 DNS、不试端口。
- **Fail Closed 的 endpoint 校验**：HTTPS、无 userinfo/query/fragment；
  HTTP 只在显式打开 `FEDERATION_ALLOW_LOOPBACK` 且 host 是**字面**
  127.0.0.1/[::1] 时放行（`localhost` 不给过 —— 那是 DNS）。
- **每个请求一张节点凭证**（`ddp-node-credential/1`）：出站前把
  (audience, 原始调用者, 操作, 范围约束, 方法/路径/正文摘要) 交给**本节点**控制面
  签发，放进 `X-DDP-Node-Credential`。本服务不持有任何对端的口令，也不再把对端的
  `SERVICE_TOKEN` 与自报 actor 头发出去；用户的 API key（连 id 都）不转发（§8.4）。
  签不出凭证是**本节点**的问题：`PeerUnavailable(status=None, code=…)`，协调者记
  unreachable 并保留可重试，绝不说成对端没有资料。
- 出站 HTTP 与仓库铁律 8 一致：`trust_env=False`、`follow_redirects=False`，
  响应体有字节上限，超限即断。凭证只进请求头，重定向不会把它带到第二个监听器；
  即使被截获，audience 与请求绑定也让它在别处、别的请求上无效。
- 传输层与签发方可注入（`transport=` / `signer=`），测试用 `MockTransport` 与本地
  测试签发方就能覆盖全部出站行为，不需要真网络。

`FEDERATION_PEER_AUTH=shared_token_insecure` 保留旧的共享口令形态，仅供开发夹具。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from ddp_core.application import node_credentials as nc
from ddp_core.application.ports import ApplicationError

from ddp_corpus.config import Settings, settings
from ddp_corpus.deps import Actor

NODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
MAX_ENDPOINT_CHARS = 512
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
#: 一个证据集分页的上限就是 50 条；4 MiB 对其信封绰绰有余，再大说明对方失约。
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
#: 对等目录读的分页口径：单页 ≤100（对端校验的上限），一次最多跟随 4 页。
#: 目录页只用来排序；截断的部分不参与排序，但**绝不因此少枚举一个成员**。
DIRECTORY_PAGE_LIMIT = 100
MAX_DIRECTORY_PAGES = 4


class PeerUnavailable(RuntimeError):
    """未登记 / 连不上 / 超时 / 响应超限 / 对方回错 / 签不出凭证，统一走这一个出口。

    `status` 为 None 表示**本端到不了对端**（连不上、超时、DNS、本节点控制面签不出
    凭证），协调者据此记 `unreachable` 而不是 `failed`；有 status 表示对方明确回了
    HTTP 错误。`code` 是对方错误信封里的机器码或本端的契约码。**消息里永远没有凭据。**
    """

    def __init__(self, node_id: str, message: str, *, status: int | None = None,
                 code: str | None = None):
        super().__init__(f"peer {node_id}: {message}")
        self.node_id = node_id
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PeerConfig:
    node_id: str
    endpoint: str
    #: 仅 shared_token_insecure 档位使用；node_credential 档位必须为空。
    service_token: str = ""
    peer_token: str = ""


@dataclass(frozen=True)
class Delegation:
    """这一批出站请求所属的委托范围：一个根任务、一份需求修订。

    约束从这里取而不是从请求体里抄：请求体被错误地换成别的根任务/需求时，
    对端比对不上当场拒绝，而不是签一张"恰好覆盖错误请求"的凭证。
    """

    root_task_id: str
    task_spec_digest: str | None = None


def _loopback_host(host: str | None) -> bool:
    # **字面地址，不是"看起来像本地"**：localhost 会走 DNS，必须由开关挡在外面。
    return host in ("127.0.0.1", "::1")


def validate_endpoint(node_id: str, endpoint, *, allow_loopback: bool) -> str:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_CHARS:
        raise PeerUnavailable(node_id, "endpoint is missing or too long")
    url = urlsplit(endpoint)
    loopback = allow_loopback and _loopback_host(url.hostname)
    if (url.scheme != "https" and not loopback) or not url.hostname \
            or url.username or url.password or url.query or url.fragment:
        raise PeerUnavailable(
            node_id, "endpoint must be https without userinfo/query/fragment "
                     "(http only for literal loopback with FEDERATION_ALLOW_LOOPBACK)")
    return endpoint.rstrip("/")


def parse_peers(raw: str, *, allow_loopback: bool = False,
                shared_token: bool | None = None) -> dict[str, PeerConfig]:
    """把 JSON 配置校验成目录；任何一条坏配置都是启动/调用即失败，不是静默跳过。

    node_credential 档位（默认）只许登记 `endpoint`：带着口令字段是配置错误 ——
    不用的秘密留在配置里，迟早被复制到别处。
    """
    if shared_token is None:
        shared_token = settings.federation_peer_auth == "shared_token_insecure"
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        raise PeerUnavailable("-", "FEDERATION_PEERS is not valid JSON") from None
    if not isinstance(value, dict):
        raise PeerUnavailable("-", "FEDERATION_PEERS must be a JSON object")
    allowed = {"endpoint", "service_token", "peer_token"} if shared_token else {"endpoint"}
    peers: dict[str, PeerConfig] = {}
    for node_id, item in value.items():
        if not isinstance(node_id, str) or not NODE_PATTERN.fullmatch(node_id):
            raise PeerUnavailable(str(node_id)[:64], "invalid node id in FEDERATION_PEERS")
        if not isinstance(item, dict):
            raise PeerUnavailable(node_id, "peer entry must be an object")
        unknown = set(item) - allowed
        if unknown:
            hint = "" if shared_token else (
                " (shared peer tokens are not used with FEDERATION_PEER_AUTH=node_credential)")
            raise PeerUnavailable(node_id, f"unknown peer fields: {sorted(unknown)}{hint}")
        endpoint = validate_endpoint(node_id, item.get("endpoint"), allow_loopback=allow_loopback)
        if shared_token:
            for key in ("service_token", "peer_token"):
                token = item.get(key)
                if not isinstance(token, str) or not token:
                    raise PeerUnavailable(node_id, f"missing {key}")
            peers[node_id] = PeerConfig(node_id=node_id, endpoint=endpoint,
                                        service_token=item["service_token"],
                                        peer_token=item["peer_token"])
        else:
            peers[node_id] = PeerConfig(node_id=node_id, endpoint=endpoint)
    return peers


def _body_bytes(json_body: dict | None) -> bytes:
    # 签名覆盖的是**实际发出的字节**：这里编码一次，签发与发送用同一份。
    if json_body is None:
        return b""
    return json.dumps(json_body, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class PeerClient:
    """一个登记节点的出站客户端。所有方法失败一律 `PeerUnavailable`。"""

    def __init__(self, config: PeerConfig, *, actor: Actor, transport=None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES,
                 signer=None, delegation: Delegation | None = None,
                 shared_token: bool | None = None):
        self.config = config
        self.actor = actor
        self.max_response_bytes = max_response_bytes
        self.signer = signer
        self.delegation = delegation
        self.shared_token = (settings.federation_peer_auth == "shared_token_insecure"
                             if shared_token is None else shared_token)
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, trust_env=False, follow_redirects=False)

    def _shared_headers(self) -> dict[str, str]:
        """shared_token_insecure 档位的旧头：对端服务凭据 + 共享口令 + 自报 actor。"""
        actor = self.actor
        headers = {
            "Authorization": f"Bearer {self.config.service_token}",
            "X-DDP-Peer-Token": self.config.peer_token,
            "X-DDP-Organization": actor.organization_id,
            "X-DDP-Actor": actor.id,
            "X-DDP-Actor-Kind": actor.kind,
            "X-DDP-Role": actor.role,
        }
        if actor.principal_id:
            headers["X-DDP-User"] = actor.principal_id
        if actor.api_key_id:
            headers["X-DDP-Api-Key"] = actor.api_key_id
        return headers

    async def _credential(self, *, operation: str, constraints: dict, method: str,
                          path: str, body: bytes) -> str:
        if self.signer is None or self.delegation is None:
            raise PeerUnavailable(self.config.node_id,
                                  "outbound peer call has no credential signer or root task",
                                  code="credential_unavailable")
        try:
            request = nc.sign_request(
                audience_node_id=self.config.node_id,
                actor={"organization_id": self.actor.organization_id,
                       "subject": self.actor.principal_id or self.actor.id,
                       "kind": self.actor.kind},
                operation=operation, constraints=constraints, method=method, path=path,
                body=body, ttl_seconds=settings.federation_credential_ttl_seconds)
        except ApplicationError as exc:
            # 例如 peer-* 主体想再转委托给第三个节点：actor.kind 不是契约里的主体类型。
            raise PeerUnavailable(self.config.node_id, "cannot request a node credential",
                                  code=exc.code) from None
        try:
            signed = await self.signer.issue(request)
        except PeerUnavailable:
            raise
        except Exception as exc:          # noqa: BLE001 —— 签发方的任何失败都不是对端的错
            code = getattr(exc, "code", None) or "credential_unavailable"
            raise PeerUnavailable(self.config.node_id, "node credential was not issued",
                                  code=str(code)) from None
        token = signed.get("credential") if isinstance(signed, dict) else None
        if not isinstance(token, str) or not token:
            raise PeerUnavailable(self.config.node_id, "node credential was not issued",
                                  code="credential_unavailable")
        return token

    def _constraints(self, **extra) -> dict:
        root = self.delegation.root_task_id if self.delegation else ""
        constraints = {"root_task_id": root}
        constraints.update({name: value for name, value in extra.items() if value})
        return constraints

    async def _request(self, method: str, path: str, *, operation: str,
                       constraints: dict | None = None, json_body: dict | None = None,
                       idempotency_key: str | None = None,
                       params: dict | None = None) -> dict:
        body = _body_bytes(json_body)
        headers = {"Accept": "application/json", "X-DDP-Target-Node": self.config.node_id}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if self.actor.request_id:
            headers["X-Request-Id"] = self.actor.request_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if self.shared_token:
            headers.update(self._shared_headers())
        else:
            headers[nc.HEADER] = await self._credential(
                operation=operation, constraints=constraints or self._constraints(),
                method=method, path=path, body=body)
        try:
            async with self._client.stream(
                    method, self.config.endpoint + path, content=body or None, params=params,
                    headers=headers) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_response_bytes:
                        raise PeerUnavailable(
                            self.config.node_id, "response exceeds the configured byte cap",
                            status=response.status_code)
                status = response.status_code
        except PeerUnavailable:
            raise
        except (httpx.HTTPError, OSError) as exc:
            # 只报异常类型名：httpx 的原文可能带上 URL 细节，而这里的原则是
            # 出站失败只留可排查的最小事实，凭据一个字都不出现。
            raise PeerUnavailable(self.config.node_id,
                                  f"transport error ({type(exc).__name__})") from None
        decoded = self._decode(content, status)
        if status >= 400:
            code = None
            if isinstance(decoded, dict):
                code = str((decoded.get("error") or {}).get("code") or "") or None
            raise PeerUnavailable(self.config.node_id, f"peer answered HTTP {status}",
                                  status=status, code=code)
        if not isinstance(decoded, dict):
            raise PeerUnavailable(self.config.node_id, "peer returned a non-object payload",
                                  status=status)
        return decoded

    def _decode(self, content: bytearray, status: int):
        try:
            return json.loads(bytes(content))
        except ValueError:
            if status >= 400:
                return None
            raise PeerUnavailable(self.config.node_id, "peer returned invalid JSON",
                                  status=status) from None

    def _spec_digest(self, request_value=None):
        if self.delegation is not None and self.delegation.task_spec_digest:
            return self.delegation.task_spec_digest
        return request_value

    async def probe(self, request: dict, *, idempotency_key: str) -> dict:
        constraints = self._constraints(
            task_spec_digest=self._spec_digest(request.get("task_spec_digest")),
            scope_ref=request.get("scope_ref"))
        return await self._request("POST", "/api/v1/federation/probes", operation="probe_create",
                                   constraints=constraints, json_body=request,
                                   idempotency_key=idempotency_key)

    async def admit(self, admission_request: dict, *, idempotency_key: str) -> dict:
        constraints = self._constraints(step_id=admission_request.get("step_id"))
        return await self._request("POST", "/api/v1/federation/admissions",
                                   operation="admission_create", constraints=constraints,
                                   json_body=admission_request, idempotency_key=idempotency_key)

    async def lookup(self, idempotency_key: str) -> dict:
        """按业务幂等键对账一次受理；键放受认证请求体里，不放 URL（计划 §9.5）。

        404 由调用方解释为"从未受理"（`PeerUnavailable.status == 404`），
        **不是**"可以换节点重做"。
        """
        return await self._request("POST", "/api/v1/federation/admissions/lookup",
                                   operation="admission_lookup",
                                   json_body={"idempotency_key": idempotency_key})

    async def execution(self, executor_task_id: str) -> dict:
        return await self._request("GET", f"/api/v1/federation/tasks/{executor_task_id}",
                                   operation="execution_read")

    async def cancel(self, executor_task_id: str) -> dict:
        return await self._request("POST", f"/api/v1/federation/tasks/{executor_task_id}/cancel",
                                   operation="execution_cancel", json_body={},
                                   idempotency_key=f"cancel:{executor_task_id}")

    async def evidence_set(self, set_ref: str) -> dict:
        return await self._request(
            "GET", f"/api/v1/federation/evidence-sets/{set_ref}", operation="evidence_set_read",
            constraints=self._constraints(task_spec_digest=self._spec_digest()))

    async def published_collections(self, *, snapshot_id: str = "", cursor: str = "",
                                    limit: int = 100) -> dict:
        """对等目录读：本节点自发布集合描述符的一页。

        分页协议与 `catalog.snapshot_page` 相同：首请求不带 `snapshot_id` 建快照，
        之后带快照与游标跟随，终止页 `complete=true`。`limit` 上限 100 由对端
        校验，这里不放大。node_credential 档位走节点对节点端点；共享口令档位沿用
        旧的 `/internal/...`（持对端 SERVICE_TOKEN，所以只许开发用）。
        """
        params: dict = {"limit": min(max(1, limit), 100)}
        if snapshot_id:
            params["snapshot_id"] = snapshot_id
        if cursor:
            params["cursor"] = cursor
        path = ("/internal/federation/published-collections" if self.shared_token
                else "/api/v1/federation/published-collections")
        return await self._request("GET", path, operation="catalog_read", params=params)

    async def locate(self, resource_id: str, version_id: str | None = None) -> dict:
        return await self._request("POST", "/api/v1/federation/resources/locate",
                                   operation="resource_locate",
                                   json_body={"resource_id": resource_id, "version_id": version_id})

    async def resolve(self, evidence_ref: str) -> dict:
        return await self._request("POST", "/api/v1/federation/results/resolve",
                                   operation="result_resolve",
                                   json_body={"evidence_ref": evidence_ref})

    async def aclose(self) -> None:
        await self._client.aclose()


class PeerDirectory:
    """登记目录 + 按需建立的客户端；未登记节点在第 0 步就被拒绝。"""

    def __init__(self, peers: dict[str, PeerConfig], *, actor: Actor, transport=None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES,
                 signer=None, delegation: Delegation | None = None,
                 shared_token: bool | None = None):
        self._peers = dict(peers)
        self._actor = actor
        self._transport = transport
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._signer = signer
        self._delegation = delegation
        self._shared_token = shared_token
        self._clients: dict[str, PeerClient] = {}

    @classmethod
    def from_settings(cls, *, actor: Actor, delegation: Delegation | None = None,
                      transport=None, signer=None,
                      settings_: Settings | None = None) -> "PeerDirectory":
        config = settings_ or settings
        shared = config.federation_peer_auth == "shared_token_insecure"
        peers = parse_peers(config.federation_peers,
                            allow_loopback=config.federation_allow_loopback,
                            shared_token=shared)
        if signer is None and not shared:
            from ddp_corpus.node_auth import ControlCredentialSigner

            signer = ControlCredentialSigner()
        return cls(peers, actor=actor, transport=transport, signer=signer,
                   delegation=delegation, shared_token=shared)

    def known(self, node_id: str) -> bool:
        return node_id in self._peers

    def client(self, node_id: str) -> PeerClient:
        if node_id not in self._peers:
            # unknown node 绝不联系：这里不是"先试一下"，而是当场失败。
            raise PeerUnavailable(node_id, "node is not registered in FEDERATION_PEERS")
        if node_id not in self._clients:
            self._clients[node_id] = PeerClient(
                self._peers[node_id], actor=self._actor, transport=self._transport,
                timeout=self._timeout, max_response_bytes=self._max_response_bytes,
                signer=self._signer, delegation=self._delegation,
                shared_token=self._shared_token)
        return self._clients[node_id]

    async def collections(self, node_id: str, *, reserve=None,
                           max_pages: int = MAX_DIRECTORY_PAGES) -> list[dict]:
        """读取一个已登记节点自发布的集合描述符（分页跟随到终止页或有界停止）。

        `reserve` 是每页请求前的预算闸门，返回 False（额度耗尽）即停止；已读到的
        页保留。**预算耗尽不是错误**：少一份排序依据，不删任何成员。同样，
        `PeerUnavailable` 不在这里吞 —— 调用方决定"该节点没有描述符"是否可降级，
        目录层不替它猜。
        """
        client = self.client(node_id)
        collected: list[dict] = []
        snapshot_id = cursor = ""
        for _ in range(max(1, max_pages)):
            if reserve is not None and not reserve():
                break
            page = await client.published_collections(
                snapshot_id=snapshot_id, cursor=cursor, limit=DIRECTORY_PAGE_LIMIT)
            items = page.get("collections") or []
            if not isinstance(items, list):
                raise PeerUnavailable(node_id, "peer catalog page is not an array",
                                      status=200)
            collected.extend(item for item in items if isinstance(item, dict))
            if page.get("complete") or not page.get("next_cursor"):
                break
            snapshot_id = str(page.get("snapshot_id") or "")
            cursor = str(page.get("next_cursor") or "")
            if not snapshot_id or not cursor:
                break
        return collected

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        if self._signer is not None and hasattr(self._signer, "aclose"):
            await self._signer.aclose()

    async def __aenter__(self) -> "PeerDirectory":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()
