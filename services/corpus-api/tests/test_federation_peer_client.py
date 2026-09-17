"""P5 peer 目录与出站客户端（`federation_peers`）：Fail Closed、凭据不外泄。

全部出站行为都由注入的 `httpx.MockTransport` 覆盖 —— 不需要真网络，也不会
因为本机有代理而变绿（`trust_env=False` 由构造参数钉着）。

默认档位是 **node_credential**：每次出站现向本节点控制面要一张只覆盖这一次
请求的凭证，请求里**没有**对端 SERVICE_TOKEN、**没有**自报的 actor 头。
文件末尾单独一节仍然覆盖 `shared_token_insecure`（开发夹具档位），因为那条路
还在，而"还在但没人测"正是它悄悄回归的方式。
"""
import json

import httpx
import pytest

from conftest import ACTOR, ORG
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from ddp_core.application import node_credentials as nc
from ddp_corpus.deps import Actor
from ddp_corpus.federation_peers import (
    Delegation,
    PeerConfig,
    PeerDirectory,
    PeerUnavailable,
    parse_peers,
    validate_endpoint,
)
from node_credentials_fixture import (
    LOCAL_KEY,
    PEER_KEY,
    LocalControlSigner,
    PEER_NODE_ID,
)

NODE = PEER_NODE_ID                       # 出站的对端（audience）
ABSENT = "node-" + "d" * 48
ENDPOINT = "https://peer.example"
SERVICE_TOKEN = "peer-service-secret"
PEER_TOKEN = "peer-trust-secret"
LOCAL_NODE = "node-" + "a" * 48           # 本节点（凭证的 issuer）
ROOT_TASK = "root-1"

ACTOR_OBJECT = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor",
                     request_id="req-1")
DELEGATION = Delegation(root_task_id=ROOT_TASK, task_spec_digest="sha256:" + "1" * 64)


def peer_entry(**over) -> str:
    """node_credential 档位的登记：只有 endpoint。"""
    value = {"endpoint": ENDPOINT}
    value.update(over)
    return json.dumps({NODE: value})


def shared_entry(**over) -> str:
    value = {"endpoint": ENDPOINT, "service_token": SERVICE_TOKEN, "peer_token": PEER_TOKEN}
    value.update(over)
    return json.dumps({NODE: value})


def directory(transport, *, peers: str | None = None, actor: Actor | None = None,
              signer=None, delegation: Delegation | None = DELEGATION,
              **over) -> PeerDirectory:
    return PeerDirectory(
        parse_peers(peers if peers is not None else peer_entry(), shared_token=False),
        actor=actor or ACTOR_OBJECT, transport=transport, shared_token=False,
        signer=signer or LocalControlSigner(issuer_node_id=LOCAL_NODE),
        delegation=delegation, **over)


def shared_directory(transport, *, peers: str | None = None,
                     actor: Actor | None = None) -> PeerDirectory:
    return PeerDirectory(
        parse_peers(peers if peers is not None else shared_entry(), shared_token=True),
        actor=actor or ACTOR_OBJECT, transport=transport, shared_token=True)


def decode(token: str) -> dict:
    return nc.decode(token).claims


def _private_hex(key) -> str:
    """私钥裸字节 hex：错误消息里出现它就是真泄露（密钥标签名本身不是秘密）."""
    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()


# --------------------------------------------------------------- 目录与端点

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
    assert parse_peers("", shared_token=False) == {}
    for raw in ("not json", "[]", json.dumps({"BAD NODE": {"endpoint": ENDPOINT}}),
                peer_entry(extra="field"), peer_entry(endpoint="http://insecure.example")):
        with pytest.raises(PeerUnavailable):
            parse_peers(raw, shared_token=False)
    parsed = parse_peers(peer_entry(), shared_token=False)
    assert parsed[NODE] == PeerConfig(node_id=NODE, endpoint=ENDPOINT)


def test_node_credential_registration_refuses_leftover_shared_secrets():
    """**不用的秘密不许留在配置里** —— 留着迟早被复制到别处。

    反过来，共享口令档位缺了任何一个口令也是配置错误，不是"那就不带"。
    """
    with pytest.raises(PeerUnavailable) as info:
        parse_peers(shared_entry(), shared_token=False)
    assert "service_token" in str(info.value) and "peer_token" in str(info.value)
    for raw in (shared_entry(service_token=""), shared_entry(peer_token=""),
                peer_entry()):
        with pytest.raises(PeerUnavailable):
            parse_peers(raw, shared_token=True)


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
        seen["credential"] = decode(request.headers[nc.HEADER])
        return httpx.Response(200, json={})

    peers = directory(httpx.MockTransport(handler),
                      peers=json.dumps({NODE: {"endpoint": "https://peer.example/base"}}))
    await peers.client(NODE).execution("exec-1")
    assert seen["path"] == "/base/api/v1/federation/tasks/exec-1"
    # 凭证绑的是**服务相对路径**（契约的 `request.path` 收窄到 /api/v1/federation/…）。
    # 带前缀的 endpoint 必须由反向代理把前缀剥掉，否则对端会 403 credential_scope_denied ——
    # 这是显式失败，不是静默放行，已记在 node-credential-format.md 的已知局限里。
    assert seen["credential"]["request"]["path"] == "/api/v1/federation/tasks/exec-1"
    await peers.aclose()


# --------------------------------------------------------- 出站携带什么凭据

async def test_outbound_carries_a_request_bound_credential_and_no_shared_secret():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["content"] = request.content
        return httpx.Response(201, json={"ok": True})

    signer = LocalControlSigner(issuer_node_id=LOCAL_NODE)
    peers = directory(httpx.MockTransport(handler), signer=signer)
    body = {"schema": "x", "scope_ref": "scope-1"}
    result = await peers.client(NODE).probe(body, idempotency_key="probe-key")
    assert result == {"ok": True}
    assert seen["method"] == "POST" and seen["path"] == "/api/v1/federation/probes"

    headers = seen["headers"]
    # **一个共享秘密都没有**：既不带对端 SERVICE_TOKEN，也不带自报 actor 头。
    for gone in ("authorization", "x-ddp-peer-token", "x-ddp-organization", "x-ddp-actor",
                 "x-ddp-actor-kind", "x-ddp-role", "x-ddp-user", "x-ddp-api-key"):
        assert gone not in headers, gone
    assert headers["x-ddp-target-node"] == NODE
    assert headers["idempotency-key"] == "probe-key"

    claims = decode(headers[nc.HEADER.lower()])
    assert claims["issuer_node_id"] == LOCAL_NODE and claims["audience_node_id"] == NODE
    assert claims["operation"] == "probe_create"
    # 绑定到**这一次**请求：方法、路径、正文摘要，逐字。
    assert claims["request"] == {"method": "POST", "path": "/api/v1/federation/probes",
                                 "body_digest": nc.body_digest(seen["content"])}
    # 委托范围从 Delegation 取，不从请求体里抄。
    assert claims["constraints"]["root_task_id"] == ROOT_TASK
    assert claims["constraints"]["task_spec_digest"] == DELEGATION.task_spec_digest
    assert claims["constraints"]["scope_ref"] == "scope-1"
    # 单次、短期：有效期就是配置的那个，不是"随便多久"。
    assert 0 < claims["expires_at"] - claims["issued_at"] <= nc.MAX_LIFETIME_SECONDS
    assert signer.requests[0]["audience_node_id"] == NODE
    await peers.aclose()


async def test_every_call_gets_its_own_single_use_credential():
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers[nc.HEADER])
        return httpx.Response(200, json={})

    peers = directory(httpx.MockTransport(handler))
    client = peers.client(NODE)
    await client.execution("exec-1")
    await client.execution("exec-1")
    await peers.aclose()
    # 同一个操作、同一条路径、同一份正文 —— 凭证仍然必须是两张不同的（jti 不同）。
    # 复用一张的话，对端的重放账本会把第二次请求拒掉，而那时才发现就太晚了。
    assert tokens[0] != tokens[1]
    assert decode(tokens[0])["jti"] != decode(tokens[1])["jti"]


async def test_api_key_actor_delegates_the_person_not_the_key():
    """API key 调用者出站时**只带人**：key 的 id 一个字都不出去（§8.4）。"""
    key_actor = Actor(id="key-1", kind="api_key", organization_id=ORG, role="viewer",
                      api_key_id="key-1", user_id="user-9")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={})

    peers = directory(httpx.MockTransport(handler), actor=key_actor)
    await peers.client(NODE).execution("exec-1")
    await peers.aclose()
    claims = decode(seen[nc.HEADER.lower()])
    assert claims["actor"] == {"organization_id": ORG, "subject": "user-9", "kind": "api_key"}
    assert "key-1" not in json.dumps(claims)
    assert "x-ddp-api-key" not in seen


async def test_a_remote_principal_cannot_redelegate_to_a_third_node():
    """转委托的闸门在**申请**阶段就落下：`peer` 不是契约里的主体类型。

    否则 A→B 的一次调用会让 B 用 A 的名义再去调 C，而 C 只看得到 B 的签名。
    """
    peer_actor = Actor(id="peer-" + "0" * 27, kind="peer", organization_id=ORG, role="viewer")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    peers = directory(httpx.MockTransport(handler), actor=peer_actor)
    with pytest.raises(PeerUnavailable) as info:
        await peers.client(NODE).execution("exec-1")
    await peers.aclose()
    assert info.value.status is None and info.value.code == "credential_invalid"
    assert calls == [], "拿不到凭证就一个字节都不该发出去"


async def test_unsignable_outbound_is_this_node_s_problem_not_the_peer_s():
    """签不出凭证 -> `status is None`（协调者据此记 unreachable 且可重试）。

    报成"对端回了错"的话，协调者会把本节点控制面挂掉说成"对方没有资料"。
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)

    class _Refusing(LocalControlSigner):
        async def issue(self, sign_request):
            raise AssertionError("must not be reached")

    # ① 根本没有委托范围（调用点忘了传 Delegation）。
    peers = directory(transport, signer=_Refusing(issuer_node_id=LOCAL_NODE), delegation=None)
    with pytest.raises(PeerUnavailable) as info:
        await peers.client(NODE).execution("exec-1")
    assert info.value.status is None and info.value.code == "credential_unavailable"
    await peers.aclose()

    # ② 控制面拒签，且给出了自己的机器码 —— 原样报出来。
    from ddp_corpus.node_auth import CredentialUnavailable

    refused = LocalControlSigner(issuer_node_id=LOCAL_NODE)
    refused.failure = CredentialUnavailable("node_revoked", "control refused")
    peers = directory(transport, signer=refused)
    with pytest.raises(PeerUnavailable) as info:
        await peers.client(NODE).execution("exec-1")
    assert info.value.status is None and info.value.code == "node_revoked"
    await peers.aclose()
    assert calls == []


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
        # "credential" 是合法消息用词（"cannot request a node credential" ——
        # admission_create 缺 step_id，签发校验在出站前就拦下），禁的是头全名与
        # 秘密值：头名、共享口令、私钥派生值一个字都不许进错误消息。
        assert nc.HEADER.lower() not in message.lower()
        for secret in (SERVICE_TOKEN, PEER_TOKEN,
                       _private_hex(LOCAL_KEY), _private_hex(PEER_KEY)):
            assert secret not in message, "秘密值泄露进错误消息"
        assert info.value.status is None and info.value.node_id == NODE
        assert info.value.code == "credential_invalid"
        await peers.aclose()

        seen = []

        def redirect(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            return httpx.Response(302, headers={"Location": "https://attacker.example/x"})

        redirected = directory(httpx.MockTransport(redirect))
        with pytest.raises(PeerUnavailable) as info:
            await redirected.client(NODE).execution("exec-1")
        assert info.value.status == 302
        # 只发了一次：凭证不会被带到 Location 指的第二个监听器上。
        assert len(seen) == 1
        sent_token = seen[0].get(nc.HEADER.lower())
        assert sent_token, "出站必须真的带上节点凭证，否则'不跟随'是空断言"
        assert sent_token not in str(info.value), "签发的 token 全文不得进错误消息"
        await redirected.aclose()

        def conflict(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"error": {"code": "idempotency_conflict"}})

        conflicted = directory(httpx.MockTransport(conflict))
        with pytest.raises(PeerUnavailable) as info:
            # admission_create 必须带 step_id 约束（签发校验）：不带连签发都过不了，
            # 到不了原来要量的 409 透出。
            await conflicted.client(NODE).admit({"step_id": "retrieve-1"},
                                                idempotency_key="k")
        assert info.value.status == 409 and info.value.code == "idempotency_conflict"
        await conflicted.aclose()

    asyncio.run(run())


async def test_response_size_cap_and_method_paths():
    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    peers = directory(httpx.MockTransport(huge), max_response_bytes=1024)
    with pytest.raises(PeerUnavailable):
        await peers.client(NODE).execution("exec-1")
    await peers.aclose()

    calls = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content,
                      decode(request.headers[nc.HEADER])))
        return httpx.Response(200, json={"items": []})

    peers = directory(httpx.MockTransport(record))
    client = peers.client(NODE)
    await client.probe({}, idempotency_key="p")
    await client.admit({"step_id": "retrieve-1"}, idempotency_key="a")
    await client.lookup("a")
    await client.execution("exec-1")
    await client.cancel("exec-1")
    await client.evidence_set("federation-probe:probe-1")
    await client.locate("resource-1", "version-1")
    await client.resolve("evidence:1")
    await client.published_collections(limit=100)
    await peers.aclose()
    assert [(method, path) for method, path, _, _ in calls] == [
        ("POST", "/api/v1/federation/probes"),
        ("POST", "/api/v1/federation/admissions"),
        ("POST", "/api/v1/federation/admissions/lookup"),
        ("GET", "/api/v1/federation/tasks/exec-1"),
        ("POST", "/api/v1/federation/tasks/exec-1/cancel"),
        ("GET", "/api/v1/federation/evidence-sets/federation-probe:probe-1"),
        ("POST", "/api/v1/federation/resources/locate"),
        ("POST", "/api/v1/federation/results/resolve"),
        ("GET", "/api/v1/federation/published-collections"),
    ]
    # 幂等键在受认证请求体里，不在 URL（计划 §9.5）。
    assert json.loads(calls[2][2]) == {"idempotency_key": "a"}
    # 每个端点签的是**它自己的**操作：一张读执行状态的凭证不能拿去受理。
    assert [claims["operation"] for _, _, _, claims in calls] == [
        "probe_create", "admission_create", "admission_lookup", "execution_read",
        "execution_cancel", "evidence_set_read", "resource_locate", "result_resolve",
        "catalog_read"]
    # 必带约束：受理钉步骤，探测钉需求修订。
    assert calls[1][3]["constraints"]["step_id"] == "retrieve-1"
    assert calls[0][3]["constraints"]["task_spec_digest"] == DELEGATION.task_spec_digest


# -------------------------------------------------- 共享口令档位（仅开发夹具）

async def test_shared_token_mode_still_forwards_the_old_headers():
    """旧形态原样保留，但**只在显式配置下可达**（启动检查在 config 里另测）。

    它一直是"任何同伴都能冒充任何同伴"的那条路，留着是为了没有控制面的开发
    夹具；这条用例钉住它仍然只在 `shared_token=True` 时发生。
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(201, json={"ok": True})

    peers = shared_directory(httpx.MockTransport(handler))
    await peers.client(NODE).probe({"schema": "x"}, idempotency_key="probe-key")
    await peers.aclose()
    assert seen["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert seen["x-ddp-peer-token"] == PEER_TOKEN
    assert seen["x-ddp-organization"] == ORG and seen["x-ddp-actor"] == ACTOR
    assert seen["x-ddp-role"] == "contributor"
    assert nc.HEADER.lower() not in seen


async def test_shared_token_mode_reads_the_catalog_over_the_internal_route():
    """共享口令档位持有对端 SERVICE_TOKEN，所以走 `/internal/…`；
    node_credential 档位没有它，走节点对节点端点。两条路都要被钉住。"""
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"collections": [], "complete": True})

    shared = shared_directory(httpx.MockTransport(handler))
    await shared.client(NODE).published_collections()
    await shared.aclose()
    node = directory(httpx.MockTransport(handler))
    await node.client(NODE).published_collections()
    await node.aclose()
    assert paths == ["/internal/federation/published-collections",
                     "/api/v1/federation/published-collections"]
