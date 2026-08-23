"""gateway 入口。

薄适配层四职责（见 ARCHITECTURE.md §8）：
1. 协议转换：openapi.yaml 契约 <-> mineru /tasks
2. 鉴权：service token（mineru-api 自身无鉴权）
3. 结果后处理：ARQ 链（取结果 -> 归档 -> 通知 backend；抽取链见 worker/tasks.py）
4. 可观测：Prometheus metrics、统一错误格式

v1.1 起是**四个平面**：解析 /v1/parse、VQA /v1/chat/completions、
向量 /v1/embeddings+/v1/rerank、抽取 /v1/extract。
抽取平面复用解析平面建好的分块索引，自己不存任何东西（无状态原则不变）。
"""
import asyncio
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.config import (
    assert_secrets_configured, assert_thresholds_sane, load_registry, settings,
)
from app.errors import install_error_handlers
from app.routers import chat, embeddings, extract, health, parse, rerank
from app.services.mineru_client import MineruClient
from app.services.task_store import TaskStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 第一件事：占位 token 直接拒绝启动。带着 change-me 跑起来的话
    # 所有 /v1/* 等于无鉴权开放，且运行时不会有任何报错
    assert_secrets_configured()
    # 阈值配反了不会报错，只会让低相关提示永远不触发（又一个静默失效的功能）
    assert_thresholds_sane()
    app.state.registry = load_registry(settings.models_config)
    # 下载 file_url / 转传 mineru 可能是大文件，读超时放宽
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    # VQA 同步通道的并发闸（满载 429，见 chat.py）
    app.state.vqa_semaphore = asyncio.Semaphore(settings.vqa_max_concurrency)
    app.state.redis = redis.from_url(settings.redis_url)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.task_store = TaskStore(app.state.redis, settings.result_ttl,
                                     settings.queue_inflight_ttl)
    app.state.mineru_client = MineruClient(app.state.http)
    yield
    await app.state.http.aclose()
    await app.state.arq.aclose()
    await app.state.redis.aclose()


app = FastAPI(
    title="DeepDocParse gateway",
    version="0.1.0",
    lifespan=lifespan,
)

install_error_handlers(app)

app.include_router(health.router)
app.include_router(parse.router, prefix="/v1")
app.include_router(chat.router, prefix="/v1")
app.include_router(embeddings.router, prefix="/v1")
app.include_router(extract.router, prefix="/v1")
app.include_router(rerank.router, prefix="/v1")

# 可观测（M4）：/metrics 无鉴权（内网 Prometheus 抓取；对外由 backend 隔离）
from prometheus_fastapi_instrumentator import Instrumentator  # noqa: E402

Instrumentator().instrument(app).expose(app)
