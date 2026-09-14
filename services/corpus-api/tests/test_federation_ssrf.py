"""P5 出站客户端的 SSRF 边界：重定向、明文、跨主机与 DNS 换名。

与 `test_federation_peer_client.py` 的分工：那一份用 `MockTransport` 把出站
**协议形状**（头、路径、错误码、字节上限）钉住；这一份起**真回环 HTTP 服务
器**，量的是"请求到底去了哪、带没带凭据"—— 重定向跨主机、跨监听器转发
token 这类事，在 MockTransport 里可以声称但量不到。

安全承诺按 Fail Closed 写死：

- `follow_redirects=False`：302 就是出站失败，不是"跟过去再试一次"；
- endpoint 在 `parse_peers` 时校验一次，之后每个请求都用同一个 host/scheme；
- 只有显式打开 `FEDERATION_ALLOW_LOOPBACK` 才允许 `http://` 字面回环；
- 每个请求都带 `X-DDP-Target-Node`：请求就算被 DNS 换到别的节点，对方也
  能看出"这不是发给我的"（配合 peer token 复核）。
"""
from __future__ import annotations

import http.server
import json
import threading

import httpx
import pytest

from conftest import ACTOR, ORG
from ddp_corpus.deps import Actor
from ddp_corpus.federation_peers import (
    PeerConfig,
    PeerClient,
    PeerDirectory,
    PeerUnavailable,
    parse_peers,
    validate_endpoint,
)

NODE = "node-" + "c" * 48
OTHER = "node-" + "d" * 48
SERVICE_TOKEN = "ssrf-service-secret"
PEER_TOKEN = "ssrf-trust-secret"
ACTOR_OBJECT = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor",
                     request_id="req-ssrf")


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):        # 测试输出里不要 HTTP server 的默认噪音
        return

    def do_GET(self):
        self._record_and_reply()

    def do_POST(self):
        self._record_and_reply()

    def _record_and_reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append({
            "method": self.command, "path": self.path, "body": body,
            "authorization": self.headers.get("Authorization"),
            "peer_token": self.headers.get("X-DDP-Peer-Token"),
            "target_node": self.headers.get("X-DDP-Target-Node"),
        })
        responder = self.server.responder
        status, headers, payload = responder(self)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, responder):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[dict] = []
        self.responder = responder


def start_server(responder):
    server = _Server(responder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def directory_for(server, *, allow_loopback: bool = True) -> PeerDirectory:
    config = PeerConfig(node_id=NODE, endpoint=f"http://127.0.0.1:{server.server_port}",
                        service_token=SERVICE_TOKEN, peer_token=PEER_TOKEN)
    return PeerDirectory({NODE: config}, actor=ACTOR_OBJECT)


async def test_redirect_to_another_listener_is_not_followed_and_never_sees_the_token():
    """302 的 Location 指向另一个回环监听器：那里必须一个请求都收不到。"""
    stolen: list[dict] = []

    def target_responder(_handler):
        return 200, {"Content-Type": "application/json"}, b"{}"

    target, target_thread = start_server(target_responder)
    target.seen = stolen

    def redirecting_responder(_handler):
        return 302, {"Location": f"http://127.0.0.1:{target.server_port}/steal"}, b""

    source, source_thread = start_server(redirecting_responder)
    try:
        client = directory_for(source).client(NODE)
        with pytest.raises(PeerUnavailable) as info:
            await client.execution("exec-1")
        assert info.value.status == 302
        assert len(source.seen) == 1, "客户端对同一个登记节点只发了一次请求"
        first = source.seen[0]
        assert first["authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert first["peer_token"] == PEER_TOKEN
        assert first["target_node"] == NODE
        # 重定向目标（另一个主机/端口）从未收到任何请求，自然也没有凭据。
        assert stolen == [], f"302 被跟随，凭据被转发到第二个监听器：{stolen}"
    finally:
        await client.aclose()
        stop_server(source, source_thread)
        stop_server(target, target_thread)


async def test_redirect_to_a_foreign_hostname_is_surfaced_not_followed():
    """指向非回环主机的重定向同样是失败（302），不是"换个地方再问"。"""

    def responder(_handler):
        return 302, {"Location": "http://attacker.example/steal"}, b""

    server, thread = start_server(responder)
    try:
        client = directory_for(server).client(NODE)
        with pytest.raises(PeerUnavailable) as info:
            await client.execution("exec-1")
        assert info.value.status == 302
        assert len(server.seen) == 1
    finally:
        await client.aclose()
        stop_server(server, thread)


async def test_loopback_flag_is_required_and_tokens_only_go_to_the_registered_listener():
    """不开逃生口时连字面回环都拒；开了才发，且凭据只进那一个登记地址。"""
    server, thread = start_server(
        lambda _handler: (200, {"Content-Type": "application/json"}, b"{}"))
    client = None
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(PeerUnavailable):
            parse_peers(json.dumps({NODE: {"endpoint": endpoint,
                                           "service_token": SERVICE_TOKEN,
                                           "peer_token": PEER_TOKEN}}),
                        allow_loopback=False)
        parsed = parse_peers(json.dumps({NODE: {"endpoint": endpoint,
                                                "service_token": SERVICE_TOKEN,
                                                "peer_token": PEER_TOKEN}}),
                             allow_loopback=True)
        directory = PeerDirectory(parsed, actor=ACTOR_OBJECT)
        client = directory.client(NODE)
        await client.execution("exec-1")
        assert len(server.seen) == 1
        assert server.seen[0]["authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert server.seen[0]["target_node"] == NODE
        await directory.aclose()
    finally:
        if client is not None:
            await client.aclose()
        stop_server(server, thread)


def test_endpoint_validation_is_fail_closed_for_plain_http_userinfo_query_fragment():
    for bad in ("http://peer.example/api", "ftp://peer.example/api",
                "https://user:secret@peer.example/api",
                "https://peer.example/api?q=1", "https://peer.example/api#frag",
                "https:///api", "", "http://localhost:9001"):
        with pytest.raises(PeerUnavailable):
            validate_endpoint(NODE, bad, allow_loopback=True)
    # 字面回环是唯一例外，而且必须在开关打开时。
    with pytest.raises(PeerUnavailable):
        validate_endpoint(NODE, "http://127.0.0.1:9001", allow_loopback=False)
    assert validate_endpoint(NODE, "http://127.0.0.1:9001", allow_loopback=True) \
        == "http://127.0.0.1:9001"
    assert validate_endpoint(NODE, "http://[::1]:9001", allow_loopback=True) \
        == "http://[::1]:9001"


async def test_registered_host_is_the_only_host_ever_asked_for():
    """DNS 换名式的"同名字、别人接"：客户端只认登记时的 host/scheme。

    `peer.example` 在配置里登记一次；此后每个请求都由 `PeerClient` 用同一个
    endpoint 拼出。响应体改不了主机，重定向也不会被跟（上面两条）。即使
    解析被换到别处，请求里仍带 `X-DDP-Target-Node`：诚实的接收方会以
    `wrong_target` 拒绝，而客户端拿到的就是一次失败，不是"换个人继续"。
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"scheme": request.url.scheme, "host": request.url.host,
                     "target_node": request.headers.get("x-ddp-target-node")})
        return httpx.Response(409, json={"error": {"code": "wrong_target"}})

    peers = PeerDirectory(
        parse_peers(json.dumps({NODE: {"endpoint": "https://peer.example",
                                       "service_token": SERVICE_TOKEN,
                                       "peer_token": PEER_TOKEN}})),
        actor=ACTOR_OBJECT, transport=httpx.MockTransport(handler))
    client = peers.client(NODE)
    with pytest.raises(PeerUnavailable) as info:
        await client.execution("exec-1")
    assert info.value.status == 409 and info.value.code == "wrong_target"
    assert seen == [{"scheme": "https", "host": "peer.example", "target_node": NODE}]
    # 没有第二个 host 被尝试过：DNS 换名不会让客户端"再找一个"。
    assert {item["host"] for item in seen} == {"peer.example"}
    await peers.aclose()


async def test_dns_swapped_response_identity_is_never_treated_as_the_approved_peer():
    """被换名的接收方回了另一套身份的 200：客户端不解析响应里的身份。

    `PeerClient` 的返回值是**操作结果**，不是对端身份证明；它从不为响应
    里的 node 字段改登记、改 actor、或把结果标成"来自登记节点"。这条用
    一个声称自己是别的节点的响应把这件事钉死：形状仍是操作结果，且
    调用方拿不到任何可用来替换目录的字段。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "executor_task_id": "exec-1", "node_id": OTHER,
            "authority_node_id": OTHER, "approved": True,
            "state": "succeeded", "operation": "retrieve"})

    peers = PeerDirectory(
        parse_peers(json.dumps({NODE: {"endpoint": "https://peer.example",
                                       "service_token": SERVICE_TOKEN,
                                       "peer_token": PEER_TOKEN}})),
        actor=ACTOR_OBJECT, transport=httpx.MockTransport(handler))
    body = await peers.client(NODE).execution("exec-1")
    assert body["node_id"] == OTHER, "测试夹具必须真的带一个外来身份"
    # 这一层不把外来身份当自己人：没有目录变更入口，也没有身份提升。
    assert peers.known(NODE) is True and peers.known(OTHER) is False
    with pytest.raises(PeerUnavailable):
        peers.client(OTHER)
    await peers.aclose()
