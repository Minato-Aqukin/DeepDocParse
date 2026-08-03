"""任务映射与结果暂存（Redis）。

键设计：
  task:{task_id}          hash: mineru_task_id / engine / status / error / callback_url /
                                created_at / doc_hash        TTL=RESULT_TTL
  result:{task_id}        json: markdown / layout_json / images   TTL=RESULT_TTL(24h)
  doc:{doc_hash}          str: task_id（同一 file_url 幂等复用任务）TTL=RESULT_TTL
  queue:inflight          zset: task_id -> 受理时刻，水位控制用（见 QUEUE_INFLIGHT_KEY）

v2 (M4)：
  chunk:{doc_hash}:{i}    hash: doc_hash(TAG) / text / page_idx / bbox / page_size / vec(FLOAT32)
                          TTL=RESULT_TTL（可重建缓存，源数据在 backend）
  chunks_idx_d{dim}       Redis Stack FT 向量索引（FLAT/COSINE，惰性建）
"""
import json
import struct
import time

import redis.asyncio as redis

# 在途任务集合。**刻意不用裸计数器**：INCR/DECR 只能靠"每个任务都恰好走到 _finish"
# 维持正确，而 worker 被杀、Redis 重启、poll_and_archive 中途异常都会让计数只增不减。
# 计数漂到 PARSE_QUEUE_MAX 之后 /v1/parse 会永久 429 且无法自愈（只能手工 DEL）。
# 改成按 task_id 记名的 zset（score=受理时刻）后：
#   - 释放是幂等的（ZREM 重复调用无副作用），受理也是（ZADD 同 id 只更新 score）
#   - 读水位时先按时间淘汰陈旧成员，漏掉的释放会自己过期，不再需要人工干预
QUEUE_INFLIGHT_KEY = "queue:inflight"


def chunk_index_name(dim: int) -> str:
    """索引名带维度：换 embedding 模型（维度变化）会走新索引，而不是往旧索引里
    写维度不符的向量被 RediSearch 静默丢弃、导致永久零命中（M4 验收发现）。
    检索侧 mcp_server.chunk_index_name 必须与此保持同一命名规则。"""
    return f"chunks_idx_d{dim}"


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class TaskStore:
    def __init__(self, r: redis.Redis, result_ttl: int, inflight_ttl: float | None = None):
        self._r = r
        self._ttl = result_ttl
        # 在途成员的存活上限：超过它一定已经落终态（worker 最迟在 poll_timeout 判超时），
        # 没被释放就说明那一路挂了 —— 直接淘汰，这就是水位的自愈机制
        self._inflight_ttl = inflight_ttl if inflight_ttl is not None else float(result_ttl)

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
        await self._r.zadd(QUEUE_INFLIGHT_KEY, {task_id: time.time()})

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
        """当前在途任务数。读之前先淘汰陈旧成员 —— 这是水位的自愈点：
        worker 被杀/Redis 重启导致漏掉的释放，会在 inflight_ttl 之后自动消失，
        而不是把水位永久顶在 PARSE_QUEUE_MAX 上把整个解析平面锁死。"""
        await self._r.zremrangebyscore(
            QUEUE_INFLIGHT_KEY, "-inf", time.time() - self._inflight_ttl)
        return int(await self._r.zcard(QUEUE_INFLIGHT_KEY))

    async def release_slot(self, task_id: str) -> None:
        """任务落终态时归还水位。按 id 记名 ⇒ 幂等：重复调用（ARQ 重试同一任务）
        不会像 DECR 那样把水位越减越低。"""
        await self._r.zrem(QUEUE_INFLIGHT_KEY, task_id)

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
