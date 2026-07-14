"""gateway 入口。

薄适配层四职责（见 ARCHITECTURE.md §8）：
1. 协议转换：openapi.yaml 契约 <-> mineru /tasks
2. 鉴权：service token（mineru-api 自身无鉴权）
3. 结果后处理：ARQ 链（取结果 -> 归档 -> 通知 backend）
4. 可观测：Prometheus metrics、统一错误格式
"""
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.config import load_registry, settings
from app.errors import install_error_handlers
from app.routers import chat, health, parse
from app.services.mineru_client import MineruClient
from app.services.task_store import TaskStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.registry = load_registry(settings.models_config)
    # 下载 file_url / 转传 mineru 可能是大文件，读超时放宽
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    app.state.redis = redis.from_url(settings.redis_url)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.task_store = TaskStore(app.state.redis, settings.result_ttl)
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

# TODO(M4): Prometheus instrumentator
# from prometheus_fastapi_instrumentator import Instrumentator
# Instrumentator().instrument(app).expose(app)
