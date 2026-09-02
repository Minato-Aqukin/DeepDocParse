"""把 actor_id 渲染成人能看懂的名字。

## 为什么不直接查 control.users

那需要给 `ddp_corpus` 角色开 control schema 的读权限，而每多一条只读依赖，
就多一条把 Go 的 schema 变更传染到 Python 的路径 —— 加一列、改个类型，
Python 侧就跟着红。走 HTTP 的话，两边只通过契约耦合。

**代价是一次内部调用**，所以这里有两层省法：
  1. 批量：一次列表请求只问一次，绝不在循环里查
  2. 进程内 TTL 缓存：显示名变化不需要实时

## 拿不到名字时显示什么

**显示 actor_id 的前 8 位，不显示空白。** 空白会让人以为是"没有上传者"，
而事实是"名字暂时查不到"—— 那是两件完全不同的事（不变式 2 的同一条逻辑：
不确定必须说出来，不能长得像确定）。
"""
import time

import httpx

from ddp_corpus.config import settings

_CACHE: dict[str, tuple[float, str]] = {}
_TTL = 300.0


def _fallback(actor_id: str) -> str:
    return f"用户 {actor_id[:8]}" if actor_id else "未知"


def _cached(actor_id: str) -> str | None:
    hit = _CACHE.get(actor_id)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    return None


async def display_names(client: httpx.AsyncClient, actor_ids: list[str]) -> dict[str, str]:
    """批量解析 actor_id -> 显示名。**任何失败都退回占位名，不抛异常** ——
    查不到名字不该让整个文档列表 500。"""
    wanted = sorted({a for a in actor_ids if a})
    out: dict[str, str] = {}
    missing: list[str] = []
    for actor_id in wanted:
        name = _cached(actor_id)
        if name is not None:
            out[actor_id] = name
        else:
            missing.append(actor_id)

    if missing and settings.control_url:
        try:
            resp = await client.get(
                f"{settings.control_url}/internal/actors",
                params={"ids": ",".join(missing)},
                headers={"Authorization": f"Bearer {settings.service_token}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                now = time.monotonic()
                for actor_id, name in (resp.json() or {}).items():
                    _CACHE[actor_id] = (now, name)
                    out[actor_id] = name
        except httpx.HTTPError:
            pass  # 退回占位名，见模块 docstring

    for actor_id in wanted:
        out.setdefault(actor_id, _fallback(actor_id))
    return out


def reset_cache() -> None:
    """单测用：缓存跨用例泄漏会让"改了名字没生效"这类断言飘。"""
    _CACHE.clear()
