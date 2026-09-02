"""调用者身份 —— **本服务不认识密码、不签 JWT、不验 API key。**

那些全在 `services/control-api`（Go）。语料 API 只信任入口下发的一组内部头：

    X-DDP-Organization   组织 ID
    X-DDP-Actor          user id 或 api key id
    X-DDP-Actor-Kind     user | api_key | service
    X-DDP-Role           该 actor 在该组织内的角色
    X-DDP-Api-Key        api_key 时的 key id（可选）
    X-Request-Id         请求 ID

这是旧系统「service 不感知用户」那条边界的直接继承 —— 变的只是从单一
`SERVICE_TOKEN` 升级成带 actor 上下文的服务身份。少一份密码学实现，
就少一处出错面。

## 为什么这样是安全的

两个前提，缺一不可：

1. **入口无条件剥掉客户端传来的同名头**（`httpx.StripInboundIdentity`，
   Go 侧有 `TestClientCannotForgeIdentity` 钉着）。
2. **本服务只对内网开放，且要求服务凭据**（`SERVICE_TOKEN`）。
   没有它的话，任何能连到本服务端口的人都能自称 admin。

第 2 条由 `require_gateway_credentials` 强制。它是这套设计的承重墙，
所以**没有"本地调试就先关掉吧"的开关** —— 要跳过只能整个换掉配置里的
`ALLOW_INSECURE_DEFAULTS`，而那一项启动时会打印警告。
"""
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from ddp_contracts import ROLE_VALUES
from ddp_corpus.config import settings
from ddp_corpus.errors import APIError

# 角色的**顺序即权限高低**，取自契约（`packages/contracts/enums.yaml` 的
# role 段按 viewer -> admin 声明）。Go 侧的 rbac.rank 与迁移里的
# control.roles 都必须与它一致 —— 三处各写一份的表现是
# "数据库允许的角色在代码里判成未知，然后所有请求 403"。
_ROLE_RANK = {name: index for index, name in enumerate(ROLE_VALUES)}


@dataclass(frozen=True)
class Actor:
    """一次请求的调用者。"""

    id: str
    kind: str
    organization_id: str
    role: str
    api_key_id: str | None = None
    request_id: str = ""

    def at_least(self, need: str) -> bool:
        """角色比大小。未知角色一律无权 —— 默认拒绝。

        **不要在别处写 `if actor.role == "admin"`**：那种写法在加了新角色
        之后会静默地把新角色挡在外面。问能力，不要问角色名。
        """
        have = _ROLE_RANK.get(self.role)
        want = _ROLE_RANK.get(need)
        return have is not None and want is not None and have >= want

    # ---- 能力。加一种能力时这里是唯一要改的地方 ----
    @property
    def can_upload(self) -> bool:
        return self.at_least("contributor")

    @property
    def can_delete_document(self) -> bool:
        return self.at_least("reviewer")

    @property
    def can_review(self) -> bool:
        return self.at_least("reviewer")

    @property
    def can_manage(self) -> bool:
        return self.at_least("admin")

    def require(self, ok: bool, what: str) -> None:
        if not ok:
            raise APIError(403, f"角色 {self.role} 不能{what}",
                           "permission_error", "insufficient_role")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "missing bearer credentials", "authentication_error", "missing_token")
    return authorization[7:].strip()


async def require_gateway_credentials(authorization: str | None = Header(default=None)) -> None:
    """本服务只接受带服务凭据的调用。

    **这是 actor 上下文可信的前提**：没有它，任何能连到本端口的人都能
    自称 admin。所以它挂在每一个业务路由上，而不是"重要的那几个"。
    """
    if not secrets.compare_digest(_bearer(authorization), settings.service_token):
        raise APIError(401, "invalid service credentials",
                       "authentication_error", "invalid_service_token")


async def current_actor(
    _: None = Depends(require_gateway_credentials),
    x_ddp_organization: str | None = Header(default=None),
    x_ddp_actor: str | None = Header(default=None),
    x_ddp_actor_kind: str | None = Header(default=None),
    x_ddp_role: str | None = Header(default=None),
    x_ddp_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> Actor:
    """从内部头组装 actor。

    **缺头一律 401，不给默认值**：给 `role` 一个 viewer 默认值看起来更
    宽容，实际后果是"入口挂错了中间件"表现为"这个人突然变成只读了"，
    而不是一个能一眼看出的鉴权失败。
    """
    missing = [name for name, value in (
        ("X-DDP-Organization", x_ddp_organization),
        ("X-DDP-Actor", x_ddp_actor),
        ("X-DDP-Actor-Kind", x_ddp_actor_kind),
        ("X-DDP-Role", x_ddp_role),
    ) if not value]
    if missing:
        raise APIError(401, f"缺少 actor 上下文头：{', '.join(missing)}"
                            f"（这些头由 control-api 下发，不接受客户端传入）",
                       "authentication_error", "missing_actor_context")
    if x_ddp_role not in _ROLE_RANK:
        raise APIError(403, f"未知角色 {x_ddp_role}（已知：{list(_ROLE_RANK)}）",
                       "permission_error", "unknown_role")
    if x_ddp_actor_kind not in ("user", "api_key", "service"):
        raise APIError(401, f"未知的 actor 类型 {x_ddp_actor_kind}",
                       "authentication_error", "unknown_actor_kind")

    return Actor(
        id=x_ddp_actor,
        kind=x_ddp_actor_kind,
        organization_id=x_ddp_organization,
        role=x_ddp_role,
        api_key_id=x_ddp_api_key,
        request_id=x_request_id or "",
    )


async def require_service_actor(actor: Actor = Depends(current_actor)) -> Actor:
    """只接受服务身份的端点（`/internal/*`：解析回调、outbox 事件消费）。"""
    if actor.kind != "service":
        raise APIError(403, "该端点只接受服务身份调用",
                       "permission_error", "service_only")
    return actor


def get_storage(request: Request):
    return request.app.state.storage


def get_service_client(request: Request):
    return request.app.state.service_client
