"""本节点联邦身份的唯一来源（计划 §2.1：一个自治中心只有一个持久 node_id）。

那个 node_id 是**控制面**由 `NODE_IDENTITY_DIR` 里的 Ed25519 公钥派生的
（`node-` + sha256(公钥) 前 48 位十六进制）。语料服务与 worker 在启动时向本节点
控制面 `GET /internal/federation/identity` 绑定它；此后 ScopeManifest 的本地成员
判定、Bundle / 发布目录的 `origin_node_id`、探测与受理回执里的节点身份、入站凭证的
audience 比对，全都读 `local_node_id()` 这一个值。

以前语料侧自己读 `BUNDLE_NODE_ID`，与控制面的密钥身份没有任何绑定：部署时两者
一旦不一致，控制面生成的 ScopeManifest 用密钥身份标本地成员，协调者却用
BUNDLE_NODE_ID 判"本地"，于是**本地目标被当成远端**，去 peer 目录里找不到，
静默记成 unreachable。现在 `BUNDLE_NODE_ID` 只作为可选的固定值核对：

- 与控制面一致 -> 正常；
- 不一致 -> `node_identity_mismatch`（503），所有联邦端点与出站都停，/readyz 可见；
- 还没绑定成功（控制面没起来）-> `node_identity_unavailable`（503），后台按
  `FEDERATION_IDENTITY_RETRY_SECONDS` 重试。

绝不"先用 BUNDLE_NODE_ID 顶着"：那正是要消灭的第二个身份源。

`FEDERATION_PEER_AUTH=shared_token_insecure`（开发档位）没有控制面，仍读
BUNDLE_NODE_ID，并在 `status()` 里报降级。
"""
from __future__ import annotations

import asyncio
import re

import httpx

from ddp_core.application import node_credentials as nc
from ddp_core.application.ports import ApplicationError

from ddp_corpus.config import settings
from ddp_corpus.errors import APIError

_NODE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
IDENTITY_PATH = "/internal/federation/identity"

#: 进程级绑定状态。只有 `bind` / `observe_authority` / 测试钩子写它。
_state: dict = {"node_id": None, "status": "unbound", "follow_configuration": False}


def _unavailable() -> APIError:
    return APIError(503, "this node has not bound its persistent identity from control-api yet",
                    "server_error", "node_identity_unavailable")


def _mismatch() -> APIError:
    return APIError(503, "configured node identity differs from the control-api node identity",
                    "server_error", "node_identity_mismatch")


def shared_token_mode() -> bool:
    return settings.federation_peer_auth == "shared_token_insecure"


def _configured() -> str:
    return (settings.bundle_node_id or "").strip()


def local_node_id() -> str:
    """本节点的联邦身份；拿不到可信的就 503，绝不编一个。"""
    configured = _configured()
    if shared_token_mode() or _state["follow_configuration"]:
        if not _NODE.fullmatch(configured):
            raise APIError(503, "configure a persistent node identity (BUNDLE_NODE_ID)",
                           "server_error", "node_identity_unconfigured")
        return configured
    if _state["status"] == "mismatch":
        raise _mismatch()
    bound = _state["node_id"]
    if _state["status"] != "bound" or not bound:
        raise _unavailable()
    if configured and configured != bound:
        # 运行中改了配置（或测试改了 settings）也不许静默换身份。
        _state["status"] = "mismatch"
        raise _mismatch()
    return bound


def observe_authority(node_id) -> None:
    """控制面在别的响应里报告了本节点身份（签发的 issuer、信任记录的 authority）。

    与绑定值不一致说明控制面换了身份（种子被替换/恢复错了备份）：立刻进入
    mismatch，而不是用新值继续跑 —— 旧 scope、旧回执里记的都是旧身份。
    """
    if shared_token_mode() or _state["follow_configuration"]:
        return
    if node_id != local_node_id():
        _state["status"] = "mismatch"
        raise _mismatch()


async def bind(http: httpx.AsyncClient | None = None) -> str:
    """向本节点控制面取持久身份并绑定。失败抛 APIError（503），状态如实记下。"""
    if shared_token_mode():
        return local_node_id()
    owned = http is None
    client = http or httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False)
    try:
        try:
            response = await client.get(
                settings.control_url.rstrip("/") + IDENTITY_PATH,
                headers={"Authorization": f"Bearer {settings.service_token}",
                         "X-DDP-Service": "corpus-api"},
                timeout=5.0, follow_redirects=False)
        except httpx.HTTPError:
            if _state["status"] != "mismatch":
                _state["status"] = "unavailable"
            raise _unavailable() from None
        if response.status_code != 200:
            if _state["status"] != "mismatch":
                _state["status"] = "unavailable"
            raise _unavailable()
        try:
            body = response.json()
            node_id = body["node_id"]
            derived = nc.node_id_for_public_key(body["public_key"])
        except (ValueError, KeyError, TypeError, ApplicationError):
            _state["status"] = "unavailable"
            raise _unavailable() from None
        if not isinstance(node_id, str) or derived != node_id:
            # 控制面报了一个不是由它公钥派生的身份：不信。
            _state["status"] = "unavailable"
            raise _unavailable()
        configured = _configured()
        if configured and configured != node_id:
            _state.update(node_id=None, status="mismatch")
            raise _mismatch()
        if _state["node_id"] not in (None, node_id):
            _state.update(status="mismatch")
            raise _mismatch()
        _state.update(node_id=node_id, status="bound")
        return node_id
    finally:
        if owned:
            await client.aclose()


async def keep_bound(http: httpx.AsyncClient | None = None) -> None:
    """启动绑定循环：直到绑定成功为止按间隔重试；mismatch 也继续核对（管理员可能
    修正了配置并重启了控制面），但**绝不因为重试成功就覆盖一个已有的不同身份**。"""
    if shared_token_mode():
        return
    while _state["status"] != "bound":
        try:
            await bind(http)
        except APIError as exc:
            print(f"[federation] node identity not bound: {exc.code}")
        if _state["status"] == "bound":
            return
        await asyncio.sleep(settings.federation_identity_retry_seconds)


def status() -> dict:
    """给 /readyz 的联邦身份与认证档位。不影响就绪（联邦是可选能力）。

    **降级就是 `peer_auth` 本身**：`shared_token_insecure` 是契约枚举
    `peer_auth_mode` 里 `severity: warn` 的那个取值，带着用户可见文案
    「共享口令（不安全，仅开发）」。这里刻意不再另起一个手写的 `degraded`
    字符串 —— 那会是同一件事的第二份真相，而且没有枚举守卫看着它。
    `node_identity` 取 unbound / unavailable / mismatch / bound，
    后三者说明联邦端点此刻为什么在 503。
    """
    if shared_token_mode():
        return {"peer_auth": "shared_token_insecure", "node_identity": "configured",
                "node_id": _configured() or None}
    identity = _state["status"]
    node_id = _state["node_id"] if identity == "bound" else None
    if _state["follow_configuration"]:
        identity, node_id = "bound", _configured() or None
    return {"peer_auth": "node_credential", "node_identity": identity, "node_id": node_id}


def ready() -> bool:
    try:
        local_node_id()
    except APIError:
        return False
    return True


# ---------------------------------------------------------------- 测试钩子

def reset() -> None:
    _state.update(node_id=None, status="unbound", follow_configuration=False)


def follow_configuration_for_tests() -> None:
    """测试替身："控制面报告的身份恰好等于 BUNDLE_NODE_ID"。

    进程内单测没有控制面；它们关心的是联邦业务逻辑，不是身份绑定。绑定本身
    （mismatch / unavailable / 派生校验）由 `test_federation_node_auth.py` 直接
    调 `bind` 对着一个严格的假控制面测，那里不用这个钩子。生产代码不调用它。
    """
    _state.update(follow_configuration=True)


def bind_static_for_tests(node_id: str) -> None:
    _state.update(node_id=node_id, status="bound", follow_configuration=False)
