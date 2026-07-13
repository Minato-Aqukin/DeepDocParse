"""任务映射与结果暂存（Redis）。

键设计（建议）：
  task:{task_id}          hash: mineru_task_id / engine / status / callback_url / created_at
  result:{task_id}        json: markdown / layout_json / images   TTL=RESULT_TTL(24h)
  queue_depth             计数器（受理+1，完成/失败-1），水位控制用

v2 (M4) 追加：
  chunks:{doc_hash}       结构感知分块（含页码+bbox）
  向量索引               Redis Stack FT.CREATE ... VECTOR（可重建缓存，源数据在 backend）
"""
import redis.asyncio as redis


class TaskStore:
    def __init__(self, r: redis.Redis, result_ttl: int):
        self._r = r
        self._ttl = result_ttl

    async def create(self, task_id: str, mineru_task_id: str, engine: str,
                     callback_url: str | None) -> None:
        raise NotImplementedError  # TODO(M1)

    async def get(self, task_id: str) -> dict | None:
        raise NotImplementedError  # TODO(M1)

    async def save_result(self, task_id: str, result: dict) -> None:
        raise NotImplementedError  # TODO(M1): setex, TTL=self._ttl

    async def load_result(self, task_id: str) -> dict | None:
        raise NotImplementedError  # TODO(M1)

    async def queue_depth(self) -> int:
        raise NotImplementedError  # TODO(M1)
