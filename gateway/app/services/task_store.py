"""任务映射与结果暂存（Redis）。

键设计：
  task:{task_id}          hash: mineru_task_id / engine / status / error / callback_url /
                                created_at / doc_hash        TTL=RESULT_TTL
  result:{task_id}        json: markdown / layout_json / images   TTL=RESULT_TTL(24h)
  doc:{doc_hash}          str: task_id（同一 file_url 幂等复用任务）TTL=RESULT_TTL
  queue_depth             计数器（受理+1，完成/失败-1），水位控制用

v2 (M4)：
  chunk:{doc_hash}:{i}    hash: doc_hash(TAG) / text / page_idx / bbox / page_size / vec(FLOAT32)
                          TTL=RESULT_TTL（可重建缓存，源数据在 backend）
  chunks_idx_d{dim}       Redis Stack FT 向量索引（FLAT/COSINE，惰性建）
"""
import json
import struct

import redis.asyncio as redis


def chunk_index_name(dim: int) -> str:
    """索引名带维度：换 embedding 模型（维度变化）会走新索引，而不是往旧索引里
    写维度不符的向量被 RediSearch 静默丢弃、导致永久零命中（M4 验收发现）。
    检索侧 mcp_server.chunk_index_name 必须与此保持同一命名规则。"""
    return f"chunks_idx_d{dim}"


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


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

    async def link_alias(self, alias_hash: str, task_id: str) -> None:
        """给同一任务再挂一个身份别名。

        用途：调用方带 doc_id 提交时，任务身份是 sha256(doc_id)，而只拿得到裸 URL 的
        调用方（mcp_server 的 ask_document）会按 sha256(file_url) 来找 —— 没有别名就
        找不到，于是重复解析一遍。别名让两条路命中同一个任务。
        """
        await self._r.set(f"doc:{alias_hash}", task_id, ex=self._ttl)

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

    # ---------- v2 (M4)：向量索引（Redis Stack RediSearch） ----------

    async def ensure_chunk_index(self, dim: int) -> bool:
        """惰性建 FT 索引；无 RediSearch 模块（如裸 redis/fakeredis）时返回 False，
        分块数据照存（可重建缓存），检索方自行回退 BM25。"""
        try:
            await self._r.execute_command(
                "FT.CREATE", chunk_index_name(dim), "ON", "HASH", "PREFIX", "1", "chunk:",
                "SCHEMA",
                "doc_hash", "TAG",
                "page_idx", "NUMERIC",
                "vec", "VECTOR", "FLAT", "6",
                "TYPE", "FLOAT32", "DIM", str(dim), "DISTANCE_METRIC", "COSINE",
            )
            return True
        except redis.ResponseError as exc:
            return "already exists" in str(exc).lower()
        except Exception:
            return False

    async def save_chunks(self, doc_hash: str, chunks: list[dict],
                          vectors: list[list[float]]) -> None:
        # 先清同文档旧分块：重解析后 chunk 数变少时，残留的高下标键仍会被检索命中。
        # count 给大：默认 COUNT=10 会把整个键空间按 10 个一批扫完，24h TTL 下往返很可观
        stale = [k async for k in self._r.scan_iter(match=f"chunk:{doc_hash}:*", count=1000)]
        pipe = self._r.pipeline()
        if stale:
            pipe.delete(*stale)
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            key = f"chunk:{doc_hash}:{i}"
            pipe.hset(key, mapping={
                "doc_hash": doc_hash,
                "text": chunk["text"],
                "page_idx": chunk["page_idx"],
                "bbox": json.dumps(chunk.get("bbox")),
                # 裁剪出处区域要按 layout 的页尺寸换算，缺它遇到 CropBox 偏移/旋转页会裁错
                "page_size": json.dumps(chunk.get("page_size")),
                "vec": _pack_vector(vec),
            })
            pipe.expire(key, self._ttl)
        await pipe.execute()
