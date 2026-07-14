"""任务映射与结果暂存（Redis）。

键设计：
  task:{task_id}          hash: mineru_task_id / engine / status / error / callback_url /
                                created_at / doc_hash        TTL=RESULT_TTL
  result:{task_id}        json: markdown / layout_json / images   TTL=RESULT_TTL(24h)
  doc:{doc_hash}          str: task_id（同一 file_url 幂等复用任务）TTL=RESULT_TTL
  queue_depth             计数器（受理+1，完成/失败-1），水位控制用

v2 (M4) 追加：
  chunks:{doc_hash}       结构感知分块（含页码+bbox）
  向量索引               Redis Stack FT.CREATE ... VECTOR（可重建缓存，源数据在 backend）
"""
import json

import redis.asyncio as redis


class TaskStore:
    def __init__(self, r: redis.Redis, result_ttl: int):
        self._r = r
        self._ttl = result_ttl

    async def create(self, task_id: str, mineru_task_id: str, engine: str,
                     callback_url: str | None, doc_hash: str) -> None:
        await self._r.hset(f"task:{task_id}", mapping={
            "mineru_task_id": mineru_task_id,
            "engine": engine,
            "status": "pending",
            "error": "",
            "callback_url": callback_url or "",
            "doc_hash": doc_hash,
        })
        await self._r.expire(f"task:{task_id}", self._ttl)
        await self._r.set(f"doc:{doc_hash}", task_id, ex=self._ttl)
        await self._r.incr("queue_depth")

    async def get(self, task_id: str) -> dict | None:
        data = await self._r.hgetall(f"task:{task_id}")
        return {k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v
                for k, v in data.items()} or None

    async def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        await self._r.hset(f"task:{task_id}", mapping={"status": status, "error": error or ""})

    async def find_by_doc_hash(self, doc_hash: str) -> str | None:
        task_id = await self._r.get(f"doc:{doc_hash}")
        if task_id is None:
            return None
        return task_id.decode() if isinstance(task_id, bytes) else task_id

    async def save_result(self, task_id: str, result: dict) -> None:
        await self._r.set(f"result:{task_id}", json.dumps(result, ensure_ascii=False), ex=self._ttl)

    async def load_result(self, task_id: str) -> dict | None:
        raw = await self._r.get(f"result:{task_id}")
        return json.loads(raw) if raw is not None else None

    async def queue_depth(self) -> int:
        depth = await self._r.get("queue_depth")
        return int(depth) if depth is not None else 0

    async def dec_queue_depth(self) -> None:
        # 完成/失败各调一次；DECR 到负数说明计数漂移，钳回 0
        depth = await self._r.decr("queue_depth")
        if depth < 0:
            await self._r.set("queue_depth", 0)
