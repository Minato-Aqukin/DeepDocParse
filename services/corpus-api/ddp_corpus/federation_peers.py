"""P5 协调者的 peer 目录与出站客户端（P5-INTERFACES-v3 §5）。

职责边界：

- **目录只来自管理员登记**（`FEDERATION_PEERS`）。没登记的 node_id
  一律 `PeerUnavailable`，一个请求都不发 —— 不解析 DNS、不试端口。
- **Fail Closed 的 endpoint 校验**：HTTPS、无 userinfo/query/fragment；
  HTTP 只在显式打开 `FEDERATION_ALLOW_LOOPBACK` 且 host 是**字面**
  127.0.0.1/[::1] 时放行（`localhost` 不给过 —— 那是 DNS）。
- **凭据只进请求头**：三个字段绝不回显、不入日志、不进异常消息；
  出站 HTTP 与仓库铁律 8 一致：`trust_env=False`、`follow_redirects=False`，
  响应体有字节上限，超限即断。
- 传输层可注入（`transport=`），测试用 `httpx.ASGITransport` / `MockTransport`
  就能覆盖全部出站行为，不需要真网络。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

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
    """未登记 / 连不上 / 超时 / 响应超限 / 对方回错，统一走这一个出口。

    `status` 为 None 表示**传输层失败**（连不上、超时、DNS），协调者据此记
    `unreachable` 而不是 `failed`；有 status 表示对方明确回了 HTTP 错误。
    `code` 是对方错误信封里的机器码（如果给了）。**消息里永远没有凭据。**
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
    service_token: str
    peer_token: str


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


def parse_peers(raw: str, *, allow_loopback: bool = False) -> dict[str, PeerConfig]:
    """把 JSON 配置校验成目录；任何一条坏配置都是启动/调用即失败，不是静默跳过。"""
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        raise PeerUnavailable("-", "FEDERATION_PEERS is not valid JSON") from None
    if not isinstance(value, dict):
        raise PeerUnavailable("-", "FEDERATION_PEERS must be a JSON object")
    peers: dict[str, PeerConfig] = {}
    for node_id, item in value.items():
        if not isinstance(node_id, str) or not NODE_PATTERN.fullmatch(node_id):
            raise PeerUnavailable(str(node_id)[:64], "invalid node id in FEDERATION_PEERS")
        if not isinstance(item, dict):
            raise PeerUnavailable(node_id, "peer entry must be an object")
        unknown = set(item) - {"endpoint", "service_token", "peer_token"}
        if unknown:
            raise PeerUnavailable(node_id, f"unknown peer fields: {sorted(unknown)}")
        endpoint = validate_endpoint(node_id, item.get("endpoint"), allow_loopback=allow_loopback)
        for key in ("service_token", "peer_token"):
            token = item.get(key)
            if not isinstance(token, str) or not token:
                raise PeerUnavailable(node_id, f"missing {key}")
        peers[node_id] = PeerConfig(node_id=node_id, endpoint=endpoint,
                                    service_token=item["service_token"],
                                    peer_token=item["peer_token"])
    return peers


class PeerClient:
    """一个登记节点的出站客户端。所有方法失败一律 `PeerUnavailable`。"""

    def __init__(self, config: PeerConfig, *, actor: Actor, transport=None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.config = config
        self.actor = actor
        self.max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, trust_env=False, follow_redirects=False)

    def headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        """转发**原始调用者**的完整 actor 上下文，而不是本服务的服务身份。

        远端按同一组织与权限复核；`X-DDP-Target-Node` 是"只服务自己数据"的
        绑定（远端有 require_target 守卫）。凭据字段只在这里出现。
        """
        actor = self.actor
        headers = {
            "Authorization": f"Bearer {self.config.service_token}",
            "X-DDP-Peer-Token": self.config.peer_token,
            "X-DDP-Target-Node": self.config.node_id,
            "Accept": "application/json",
            "X-DDP-Organization": actor.organization_id,
            "X-DDP-Actor": actor.id,
            "X-DDP-Actor-Kind": actor.kind,
            "X-DDP-Role": actor.role,
        }
        principal = actor.principal_id
        if principal:
            headers["X-DDP-User"] = principal
        if actor.api_key_id:
            headers["X-DDP-Api-Key"] = actor.api_key_id
        if actor.request_id:
            headers["X-Request-Id"] = actor.request_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(self, method: str, path: str, *, json_body: dict | None = None,
                       idempotency_key: str | None = None,
                       params: dict | None = None) -> dict:
        try:
            async with self._client.stream(
                    method, self.config.endpoint + path, json=json_body, params=params,
                    headers=self.headers(idempotency_key=idempotency_key)) as response:
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
        body = self._decode(content, status)
        if status >= 400:
            code = None
            if isinstance(body, dict):
                code = str((body.get("error") or {}).get("code") or "") or None
            raise PeerUnavailable(self.config.node_id, f"peer answered HTTP {status}",
                                  status=status, code=code)
        if not isinstance(body, dict):
            raise PeerUnavailable(self.config.node_id, "peer returned a non-object payload",
                                  status=status)
        return body

    def _decode(self, content: bytearray, status: int):
        try:
            return json.loads(bytes(content))
        except ValueError:
            if status >= 400:
                return None
            raise PeerUnavailable(self.config.node_id, "peer returned invalid JSON",
                                  status=status) from None

    async def probe(self, request: dict, *, idempotency_key: str) -> dict:
        return await self._request("POST", "/api/v1/federation/probes",
                                   json_body=request, idempotency_key=idempotency_key)

    async def admit(self, admission_request: dict, *, idempotency_key: str) -> dict:
        return await self._request("POST", "/api/v1/federation/admissions",
                                   json_body=admission_request, idempotency_key=idempotency_key)

    async def lookup(self, idempotency_key: str) -> dict:
        """按业务幂等键对账一次受理；键放受认证请求体里，不放 URL（计划 §9.5）。

        404 由调用方解释为"从未受理"（`PeerUnavailable.status == 404`），
        **不是**"可以换节点重做"。
        """
        return await self._request("POST", "/api/v1/federation/admissions/lookup",
                                   json_body={"idempotency_key": idempotency_key})

    async def execution(self, executor_task_id: str) -> dict:
        return await self._request("GET", f"/api/v1/federation/tasks/{executor_task_id}")

    async def cancel(self, executor_task_id: str) -> dict:
        return await self._request("POST", f"/api/v1/federation/tasks/{executor_task_id}/cancel",
                                   json_body={}, idempotency_key=f"cancel:{executor_task_id}")

    async def evidence_set(self, set_ref: str) -> dict:
        return await self._request("GET", f"/api/v1/federation/evidence-sets/{set_ref}")

    async def published_collections(self, *, snapshot_id: str = "", cursor: str = "",
                                    limit: int = 100) -> dict:
        """对等目录读：本节点自发布集合的一页描述符。

        与 P4 对等目录读同一条服务身份链路（`Authorization` + `X-DDP-Target-Node`）；
        分页协议与 `catalog.snapshot_page` 相同：首请求不带 `snapshot_id` 建快照，
        之后带快照与游标跟随，终止页 `complete=true`。`limit` 上限 100 由对端
        校验，这里不放大。
        """
        params: dict = {"limit": min(max(1, limit), 100)}
        if snapshot_id:
            params["snapshot_id"] = snapshot_id
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/internal/federation/published-collections",
                                   params=params)

    async def locate(self, resource_id: str, version_id: str | None = None) -> dict:
        return await self._request("POST", "/api/v1/federation/resources/locate",
                                   json_body={"resource_id": resource_id, "version_id": version_id})

    async def resolve(self, evidence_ref: str) -> dict:
        return await self._request("POST", "/api/v1/federation/results/resolve",
                                   json_body={"evidence_ref": evidence_ref})

    async def aclose(self) -> None:
        await self._client.aclose()


class PeerDirectory:
    """登记目录 + 按需建立的客户端；未登记节点在第 0 步就被拒绝。"""

    def __init__(self, peers: dict[str, PeerConfig], *, actor: Actor, transport=None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self._peers = dict(peers)
        self._actor = actor
        self._transport = transport
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._clients: dict[str, PeerClient] = {}

    @classmethod
    def from_settings(cls, *, actor: Actor, transport=None,
                      settings_: Settings | None = None) -> "PeerDirectory":
        config = settings_ or settings
        peers = parse_peers(config.federation_peers,
                            allow_loopback=config.federation_allow_loopback)
        return cls(peers, actor=actor, transport=transport)

    def known(self, node_id: str) -> bool:
        return node_id in self._peers

    def client(self, node_id: str) -> PeerClient:
        if node_id not in self._peers:
            # unknown node 绝不联系：这里不是"先试一下"，而是当场失败。
            raise PeerUnavailable(node_id, "node is not registered in FEDERATION_PEERS")
        if node_id not in self._clients:
            self._clients[node_id] = PeerClient(
                self._peers[node_id], actor=self._actor, transport=self._transport,
                timeout=self._timeout, max_response_bytes=self._max_response_bytes)
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

    async def __aenter__(self) -> "PeerDirectory":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()
