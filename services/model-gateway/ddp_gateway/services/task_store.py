"""任务映射与结果暂存（Redis）。

键设计：
  task:{task_id}          hash: native_task_id / engine / status / error / callback_url /
                                created_at / doc_hash        TTL=RESULT_TTL
                          （native_task_id = 引擎侧的任务标识；mineru 是它的 task_id，
                            borndigital 没有远端任务，存的是 file_url。旧键名
                            mineru_task_id 仍可读，在途任务跨版本部署不会断）
  result:{task_id}        json: markdown / layout_json / images   TTL=RESULT_TTL(24h)
  doc:{doc_hash}          str: task_id（同一 file_url 幂等复用任务）TTL=RESULT_TTL
  queue:inflight          zset: task_id -> 受理时刻，水位控制用（见 QUEUE_INFLIGHT_KEY）

v2 (M4)：
  chunk:{doc_hash}:{i}    hash: doc_hash(TAG) / text / page_idx / bbox / page_size /
                                block_type / table_html / vec(FLOAT32)
                          TTL=RESULT_TTL（可重建缓存，源数据在 backend）
                          **键名里的 i 就是 seq**：(doc_hash, seq) 是出处的稳定定位键，
                          检索时必须连键名一起取回来（见 search_chunks）
  extract:{task_id}       hash: doc_hash / status / error / callback_url / progress
                          TTL=RESULT_TTL —— 抽取平面的任务态（v1.1）
  extract_result:{task_id} json: DDP-Extract v1 结果   TTL=RESULT_TTL
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


# 命名规则住在 ddp_core —— 写入侧（这里）与检索侧（MCP）共用同一份，
# 物理上不可能再漂。原地再导出，调用方一字不用改
from ddp_core.vector_index import chunk_index_name  # noqa: E402,F401


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class TaskStore:
    def __init__(self, r: redis.Redis, result_ttl: int, inflight_ttl: float | None = None):
        self._r = r
        self._ttl = result_ttl
        # 在途成员的存活上限：超过它一定已经落终态（worker 最迟在 poll_timeout 判超时），
        # 没被释放就说明那一路挂了 —— 直接淘汰，这就是水位的自愈机制
        self._inflight_ttl = inflight_ttl if inflight_ttl is not None else float(result_ttl)

    async def create(self, task_id: str, native_task_id: str, engine: str,
                     callback_url: str | None, doc_hash: str) -> None:
        await self._r.hset(f"task:{task_id}", mapping={
            "native_task_id": native_task_id,
            "engine": engine,
            "status": "pending",
            "error": "",
            "callback_url": callback_url or "",
            "doc_hash": doc_hash,
        })
        await self._r.expire(f"task:{task_id}", self._ttl)
        await self._r.set(f"doc:{doc_hash}", task_id, ex=self._ttl)
        await self._r.zadd(QUEUE_INFLIGHT_KEY, {task_id: time.time()})

    @staticmethod
    def native_id_of(task: dict) -> str:
        """引擎侧任务标识。兼容改名之前落库的任务（TTL 24h，跨版本部署时还在）。"""
        return task.get("native_task_id") or task.get("mineru_task_id") or ""

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

    # ---------- v1.1：抽取平面的任务态 ----------

    async def create_extract(self, task_id: str, *, doc_hash: str, payload: dict,
                             callback_url: str | None) -> None:
        """抽取任务与解析任务**分开存**（extract:* 而不是 task:*）。

        合在一张 hash 里会让 /v1/parse/{id} 和 /v1/extract/{id} 互相查得到对方的任务，
        进而返回一个字段对不上的状态体 —— 两个平面的状态机不一样（抽取有 progress，
        解析没有），共用一个键迟早出岔子。
        """
        await self._r.hset(f"extract:{task_id}", mapping={
            "doc_hash": doc_hash,
            "status": "pending",
            "error": "",
            "progress": "0",
            "callback_url": callback_url or "",
            "payload": json.dumps(payload, ensure_ascii=False),
        })
        await self._r.expire(f"extract:{task_id}", self._ttl)
        await self._r.zadd(QUEUE_INFLIGHT_KEY, {task_id: time.time()})

    async def get_extract(self, task_id: str) -> dict | None:
        data = await self._r.hgetall(f"extract:{task_id}")
        if not data:
            return None
        return {k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v for k, v in data.items()}

    async def set_extract_status(self, task_id: str, status: str, *,
                                 error: str | None = None,
                                 progress: float | None = None) -> None:
        mapping: dict = {"status": status, "error": error or ""}
        if progress is not None:
            mapping["progress"] = str(round(progress, 3))
        await self._r.hset(f"extract:{task_id}", mapping=mapping)

    async def save_extract_result(self, task_id: str, result: dict) -> None:
        await self._r.set(f"extract_result:{task_id}",
                          json.dumps(result, ensure_ascii=False), ex=self._ttl)

    async def load_extract_result(self, task_id: str) -> dict | None:
        raw = await self._r.get(f"extract_result:{task_id}")
        return json.loads(raw) if raw is not None else None

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
                # v1.1：块类型进了版面契约，索引里也带上 —— 抽取平面按它优先看表格块，
                # 没有它就只能把整份文档一视同仁，表格里的记录抽不准
                "block_type": chunk.get("block_type") or "text",
                # 表格结构的唯一载体。不存的话 service 侧的多记录抽取拿到的
                # 只是拍平的单元格文字，行列关系早没了 —— 而"抽取平面靠它把表格
                # 映射成记录数组"正是块类型进契约的核心论据
                "table_html": chunk.get("table_html") or "",
                # 裁剪出处区域要按 layout 的页尺寸换算，缺它遇到 CropBox 偏移/旋转页会裁错
                "page_size": json.dumps(chunk.get("page_size")),
                "vec": _pack_vector(vec),
            })
            pipe.expire(key, self._ttl)
        await pipe.execute()

    async def search_chunks(self, doc_hash: str, vector: list[float],
                            k: int) -> list[dict] | None:
        """向量检索（FT KNN）。任何一环不可用都返回 None，让调用方回退关键词路。

        **必须把距离带回来**（`AS dist`）。mcp_server 那份老实现只 RETURN 了文本与坐标，
        于是检索结果没有量纲：无关问题照样返回 top-k，调用方无从判断该不该信。
        问答平面为此专门有一条 `qa_min_similarity` 下限（实测无关问题相似度
        0.246~0.381、真实命中 0.725~0.786），抽取平面必须用同一把尺子 ——
        否则每个抽不到的字段都会被硬塞一个最相似的噪声块当"出处"。
        """
        try:
            blob = _pack_vector(vector)
            reply = await self._r.execute_command(
                "FT.SEARCH", chunk_index_name(len(vector)),
                f"(@doc_hash:{{{doc_hash}}})=>[KNN {k} @vec $BLOB AS dist]",
                "PARAMS", "2", "BLOB", blob,
                "SORTBY", "dist",
                "RETURN", "7", "text", "page_idx", "bbox", "page_size", "dist",
                "block_type", "table_html",
                "DIALECT", "2",
            )
        except Exception:
            return None

        hits: list[dict] = []
        # FT.SEARCH 的回复是 [总数, key1, [字段...], key2, [字段...], ...]
        # **key 必须一起取**：seq 只存在于键名里（chunk:{doc_hash}:{seq}），
        # 而 (doc_hash, seq) 是出处的稳定定位键 —— 丢了它，抽取结果一过 24h
        # 就再也接不回原文，正是 P0 那条教训的抽取版
        for key, item in zip(reply[1::2], reply[2::2]):
            raw_key = key.decode() if isinstance(key, bytes) else key
            try:
                seq = int(str(raw_key).rsplit(":", 1)[1])
            except (IndexError, ValueError):
                seq = 0
            fields = {}
            for name, value in zip(item[::2], item[1::2]):
                name = name.decode() if isinstance(name, bytes) else name
                value = value.decode() if isinstance(value, bytes) else value
                fields[name] = value
            try:
                # FT 的 KNN 距离字段就是余弦距离；相似度 = 1 - 距离
                distance = float(fields.get("dist", 1.0))
            except ValueError:
                distance = 1.0
            hits.append({
                "seq": seq,
                "text": fields.get("text", ""),
                "page_idx": int(fields.get("page_idx", 0)),
                "bbox": json.loads(fields["bbox"]) if fields.get("bbox") else None,
                "page_size": (json.loads(fields["page_size"])
                              if fields.get("page_size") else None),
                "similarity": round(1.0 - distance, 4),
                "block_type": fields.get("block_type") or "text",
                "table_html": fields.get("table_html") or None,
            })
        return hits or None

    async def load_chunks(self, doc_hash: str) -> list[dict]:
        """取一份文档的全部分块（关键词路兜底用，按 seq 排序）。

        走 scan 而不是 FT.SEARCH：这条路存在的意义就是"没有 RediSearch 时也能用"，
        用 FT 去取会让兜底路径和主路径一起挂掉。
        """
        keys = [k async for k in self._r.scan_iter(match=f"chunk:{doc_hash}:*", count=1000)]
        if not keys:
            return []

        def seq_of(key) -> int:
            raw = key.decode() if isinstance(key, bytes) else key
            try:
                return int(raw.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                return 0

        keys.sort(key=seq_of)
        pipe = self._r.pipeline()
        for key in keys:
            # 不取 vec：它是二进制且体积大，关键词路一个字节都用不上
            pipe.hmget(key, "text", "page_idx", "bbox", "page_size", "block_type",
                       "table_html")
        rows = await pipe.execute()

        chunks: list[dict] = []
        for key, row in zip(keys, rows):
            text, page_idx, bbox, page_size, block_type, table_html = (
                v.decode() if isinstance(v, bytes) else v for v in row)
            if not text:
                continue
            chunks.append({
                "seq": seq_of(key),
                "text": text,
                "page_idx": int(page_idx or 0),
                "bbox": json.loads(bbox) if bbox else None,
                "page_size": json.loads(page_size) if page_size else None,
                "block_type": block_type or "text",
                "table_html": table_html or None,
                "similarity": None,     # 关键词路量不出相似度 —— 如实留空，不许伪造
            })
        return chunks
