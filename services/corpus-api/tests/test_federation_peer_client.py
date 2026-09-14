"""P5 peer 目录与出站客户端（`federation_peers`）：Fail Closed、凭据不外泄。

全部出站行为都由注入的 `httpx.MockTransport` 覆盖 —— 不需要真网络，也不会
因为本机有代理而变绿（`trust_env=False` 由构造参数钉着）。
"""
import json

import httpx
import pytest

from conftest import ACTOR, ORG
from ddp_corpus.deps import Actor
from ddp_corpus.federation_peers import (
    PeerConfig,
    PeerDirectory,
    PeerUnavailable,
    parse_peers,
    validate_endpoint,
)

NODE = "node-" + "c" * 48
ABSENT = "node-" + "d" * 48
ENDPOINT = "https://peer.example"
SERVICE_TOKEN = "peer-service-secret"
PEER_TOKEN = "peer-trust-secret"

ACTOR_OBJECT = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor",
                     request_id="req-1")


def peer_entry(**over) -> str:
    value = {"endpoint": ENDPOINT, "service_token": SERVICE_TOKEN, "peer_token": PEER_TOKEN}
    value.update(over)
    return json.dumps({NODE: value})


def directory(transport, *, peers: str | None = None) -> PeerDirectory:
    return PeerDirectory(parse_peers(peers if peers is not None else peer_entry()),
                         actor=ACTOR_OBJECT, transport=transport)


def test_endpoint_validation_is_fail_closed():
    assert validate_endpoint(NODE, "https://peer.example/api", allow_loopback=False) \
        == "https://peer.example/api"
    for bad in ("http://peer.example/api", "https://user:secret@peer.example/api",
                "https://peer.example/api?q=1", "https://peer.example/api#frag",
                "ftp://peer.example/api", "https:///api", ""):
        with pytest.raises(PeerUnavailable):
            validate_endpoint(NODE, bad, allow_loopback=False)
    # loopback 只认字面回环地址，且必须显式打开开关。
    with pytest.raises(PeerUnavailable):
        validate_endpoint(NODE, "http://127.0.0.1:9001", allow_loopback=False)
    assert validate_endpoint(NODE, "http://127.0.0.1:9001", allow_loopback=True) \
        == "http://127.0.0.1:9001"
    assert validate_endpoint(NODE, "http://[::1]:9001", allow_loopback=True) \
        == "http://[::1]:9001"
    with pytest.raises(PeerUnavailable):
        validate_endpoint(NODE, "http://localhost:9001", allow_loopback=True)


def test_parse_peers_rejects_bad_registrations_loudly():
    assert parse_peers("") == {}
    for raw in ("not json", "[]", json.dumps({"BAD NODE": {"endpoint": ENDPOINT,
                                                           "service_token": "x",
                                                           "peer_token": "y"}}),
                peer_entry(extra="field"), peer_entry(service_token=""),
                peer_entry(peer_token=""), peer_entry(endpoint="http://insecure.example")):
        with pytest.raises(PeerUnavailable):
            parse_peers(raw)
    parsed = parse_peers(peer_entry())
    assert parsed[NODE].endpoint == ENDPOINT and parsed[NODE].peer_token == PEER_TOKEN


def test_unknown_node_is_never_contacted():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    peers = directory(httpx.MockTransport(handler))
    with pytest.raises(PeerUnavailable) as info:
        peers.client(ABSENT)
    assert info.value.node_id == ABSENT and calls == []


async def test_endpoint_path_prefix_is_kept_for_every_call():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={})

    peers_json = json.dumps({NODE: {"endpoint": "https://peer.example/base",
                                    "service_token": SERVICE_TOKEN,
                                    "peer_token": PEER_TOKEN}})
    peers = directory(httpx.MockTransport(handler), peers=peers_json)
    await peers.client(NODE).execution("exec-1")
    assert seen["path"] == "/base/api/v1/federation/tasks/exec-1"
    await peers.aclose()


async def test_headers_forward_credentials_and_the_original_actor():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        return httpx.Response(201, json={"ok": True})

    peers = directory(httpx.MockTransport(handler))
    result = await peers.client(NODE).probe({"schema": "x"}, idempotency_key="probe-key")
    assert result == {"ok": True}
    assert seen["method"] == "POST" and seen["path"] == "/api/v1/federation/probes"
    headers = seen["headers"]
    assert headers["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert headers["x-ddp-peer-token"] == PEER_TOKEN
    assert headers["x-ddp-target-node"] == NODE
    assert headers["x-ddp-organization"] == ORG
    assert headers["x-ddp-actor"] == ACTOR
    assert headers["x-ddp-actor-kind"] == "user"
    assert headers["x-ddp-role"] == "contributor"
    assert headers["x-ddp-user"] == ACTOR
    assert headers["idempotency-key"] == "probe-key"
    await peers.aclose()


def test_api_key_actor_forwards_key_and_user_context():
    key_actor = Actor(id="key-1", kind="api_key", organization_id=ORG, role="viewer",
                      api_key_id="key-1", user_id="user-9")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={})

    peers = PeerDirectory(parse_peers(peer_entry()), actor=key_actor,
                          transport=httpx.MockTransport(handler))
    with pytest.raises(PeerUnavailable):
        peers.client(ABSENT)   # 只为拿到目录行为；下面直接发一次请求
    import asyncio

    async def run():
        await peers.client(NODE).execution("exec-1")
    asyncio.run(run())
    assert seen["x-ddp-actor"] == "key-1"
    assert seen["x-ddp-user"] == "user-9"
    assert seen["x-ddp-api-key"] == "key-1"


def test_credentials_never_leak_into_errors_or_followed_redirects():
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    # asyncio.run 会给每个循环新建客户端，这里用一个事件循环跑完两个用例。
    import asyncio

    async def run():
        peers = directory(httpx.MockTransport(offline))
        with pytest.raises(PeerUnavailable) as info:
            await peers.client(NODE).admit({"schema": "x"}, idempotency_key="k")
        message = str(info.value)
        assert SERVICE_TOKEN not in message and PEER_TOKEN not in message
        assert info.value.status is None and info.value.node_id == NODE
        await peers.aclose()

        def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://attacker.example/x"})

        redirected = directory(httpx.MockTransport(redirect))
        with pytest.raises(PeerUnavailable) as info:
            await redirected.client(NODE).execution("exec-1")
        assert info.value.status == 302
        await redirected.aclose()

        def conflict(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"error": {"code": "idempotency_conflict"}})

        conflicted = directory(httpx.MockTransport(conflict))
        with pytest.raises(PeerUnavailable) as info:
            await conflicted.client(NODE).admit({}, idempotency_key="k")
        assert info.value.status == 409 and info.value.code == "idempotency_conflict"
        assert PEER_TOKEN not in str(info.value)
        await conflicted.aclose()

    asyncio.run(run())


async def test_response_size_cap_and_method_paths():
    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    peers = PeerDirectory(parse_peers(peer_entry()), actor=ACTOR_OBJECT,
                          transport=httpx.MockTransport(huge), max_response_bytes=1024)
    with pytest.raises(PeerUnavailable):
        await peers.client(NODE).execution("exec-1")
    await peers.aclose()

    calls = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"items": []})

    peers = directory(httpx.MockTransport(record))
    client = peers.client(NODE)
    await client.probe({}, idempotency_key="p")
    await client.admit({}, idempotency_key="a")
    await client.lookup("a")
    await client.execution("exec-1")
    await client.cancel("exec-1")
    await client.evidence_set("federation-probe:probe-1")
    await client.locate("resource-1", "version-1")
    await client.resolve("evidence:1")
    await peers.aclose()
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/api/v1/federation/probes"),
        ("POST", "/api/v1/federation/admissions"),
        ("POST", "/api/v1/federation/admissions/lookup"),
        ("GET", "/api/v1/federation/tasks/exec-1"),
        ("POST", "/api/v1/federation/tasks/exec-1/cancel"),
        ("GET", "/api/v1/federation/evidence-sets/federation-probe:probe-1"),
        ("POST", "/api/v1/federation/resources/locate"),
        ("POST", "/api/v1/federation/results/resolve"),
    ]
    # 幂等键在受认证请求体里，不在 URL（计划 §9.5）。
    assert json.loads(calls[2][2]) == {"idempotency_key": "a"}
