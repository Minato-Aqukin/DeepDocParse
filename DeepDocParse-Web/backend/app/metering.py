"""计量与额度。

口径：按页计量（解析）、按次限速（所有平面）。
额度只在提交时做"是否已耗尽"的粗检 —— 提交时无法预知页数，预扣会误杀，
因此允许最后一次任务超额，归档拿到真实页数后再扣。
"""
import time
from collections import defaultdict, deque

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import APIError
from app.models import ApiKey, UsageRecord


async def record_usage(session: AsyncSession, *, user_id: str, kind: str,
                       api_key_id: str | None = None, task_id: str | None = None,
                       pages: int = 0, requests: int = 1) -> None:
    """记一条流水；解析类同时把页数扣到 key 的 used_pages 上。"""
    session.add(UsageRecord(user_id=user_id, api_key_id=api_key_id, task_id=task_id,
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


class RateLimiter:
    """按 key 的滑动窗口限速（每分钟请求数）。

    TODO(prod)：进程内计数只在单实例正确；多副本部署时换成 Redis 计数器
    （接口保持 allow(key_id, limit) 不变，替换实现即可）。
    """

    def __init__(self, window_seconds: int = 60):
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key_id: str, limit_per_min: int) -> None:
        now = time.monotonic()
        hits = self._hits[key_id]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= limit_per_min:
            retry_after = max(1, int(self._window - (now - hits[0])))
            raise APIError(429, f"rate limit exceeded ({limit_per_min}/min)",
                           "rate_limit_error", "rate_limited",
                           headers={"Retry-After": str(retry_after)})
        hits.append(now)
