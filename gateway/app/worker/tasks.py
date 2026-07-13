"""ARQ 后处理链 —— gateway 的编排主战场。

原则：不双重排队。推理级排队/多卡调度归 mineru 自己；
ARQ 只负责推理完成后的编排：

  poll_and_archive(task_id):
    1. 轮询 mineru status 直到 succeeded/failed（指数退避）
    2. fetch_result -> task_store.save_result（暂存 24h）
    3. callback_url 存在则 POST 通知 backend 取件
    4. queue_depth -1
    v2(M4) 追加: 5. chunk_and_index(task_id)  # 结构感知分块 -> bge-m3 向量化 -> Redis 索引

启动：arq app.worker.tasks.WorkerSettings
"""


async def poll_and_archive(ctx: dict, task_id: str) -> None:
    raise NotImplementedError  # TODO(M1)


async def chunk_and_index(ctx: dict, task_id: str) -> None:
    """v2 (M4)：按 layout_json 结构分块（chunk 携带页码+bbox）-> embedding -> 写向量索引。"""
    raise NotImplementedError  # TODO(M4)


class WorkerSettings:
    functions = [poll_and_archive]  # M4: + chunk_and_index
    # TODO(M1): redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # TODO(M1): on_startup 初始化 httpx client / TaskStore 注入 ctx
