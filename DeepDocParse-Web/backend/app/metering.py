"""计量、额度与限速。

口径：按页计量（解析）、按次限速（所有平面）。
额度只在提交时做"是否已耗尽"的粗检 —— 提交时无法预知页数，预扣会误杀，
因此允许最后一次任务超额，归档拿到真实页数后再扣。

限速有两种实现：进程内滑窗（单实例）与 Redis 令牌桶（多副本）。
选哪个由 settings.redis_url 决定，调用方只认 `await check(key, limit)` 这一个接口。
"""
import time
from collections import defaultdict, deque
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import APIError
from app.models import ApiKey, UsageRecord


async def record_usage(session: AsyncSession, *, user_id: str, kind: str,
                       api_key_id: str | None = None, parse_job_id: str | None = None,
                       pages: int = 0, requests: int = 1) -> None:
    """记一条流水；解析类同时把页数扣到 key 的 used_pages 上。"""
    session.add(UsageRecord(user_id=user_id, api_key_id=api_key_id, parse_job_id=parse_job_id,
                            kind=kind, pages=pages, requests=requests))
    if api_key_id and pages:
        key = await session.get(ApiKey, api_key_id)
        if key is not None:
            key.used_pages += pages


def check_quota(key: ApiKey) -> None:
    """额度耗尽 -> 402。unlimited(quota_pages=None) 直接放行。"""
    if key.quota_pages is not None and key.used_pages >= key.quota_pages:
        raise APIError(402, f"page quota exhausted ({key.used_pages}/{key.quota_pages})",
                       "quota_error", "quota_exhausted")


def _rate_limited(retry_after: int, limit: int) -> APIError:
    return APIError(429, f"rate limit exceeded ({limit}/min)", "rate_limit_error", "rate_limited",
                    headers={"Retry-After": str(max(1, retry_after))})


class Limiter(Protocol):
    async def check(self, key: str, limit_per_min: int) -> None: ...


class MemoryRateLimiter:
    """进程内滑动窗口。**只在单实例下正确** —— 多副本部署必须换 RedisRateLimiter。"""

    def __init__(self, window_seconds: int = 60):
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def check(self, key: str, limit_per_min: int) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= limit_per_min:
            raise _rate_limited(int(self._window - (now - hits[0])), limit_per_min)
        hits.append(now)


# 令牌桶：容量 = 每分钟上限，按秒匀速回填。
#
# 必须是 Lua 脚本：读-改-写分三次往返的话，多副本并发下会同时读到同一个余量而双双放行。
# 时间取 Redis 自己的 TIME 而不是客户端的 time.time()：多副本的时钟不一定同步，
# 用客户端时间时 (now - ts) 可能为负，反向扣减令牌造成过度限流。
_TOKEN_BUCKET = """
local key, capacity, refill = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens, ts = tonumber(data[1]), tonumber(data[2])
if tokens == nil then tokens, ts = capacity, now end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * refill)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 120)
return {allowed, tostring((1 - tokens) / refill)}
"""


class RedisRateLimiter:
    """跨副本共享的令牌桶。"""

    def __init__(self, redis):
        self._redis = redis
        self._script = redis.register_script(_TOKEN_BUCKET)

    async def check(self, key: str, limit_per_min: int) -> None:
        try:
            allowed, retry_after = await self._script(
                keys=[f"rl:{key}"], args=[limit_per_min, limit_per_min / 60.0])
        except Exception:
            return          # Redis 抖动不该把正常请求挡在门外（限速是保护，不是鉴权）
        if not int(allowed):
            raise _rate_limited(int(float(retry_after)) + 1, limit_per_min)
