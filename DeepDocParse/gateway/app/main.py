"""gateway 入口。

薄适配层四职责（见 ARCHITECTURE.md §8）：
1. 协议转换：openapi.yaml 契约 <-> mineru /tasks
2. 鉴权：service token（mineru-api 自身无鉴权）
3. 结果后处理：ARQ 链（取结果 -> 归档 -> 通知 backend）
4. 可观测：Prometheus metrics、统一错误格式
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings, load_registry
from app.routers import parse, chat, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # TODO(M1): 初始化 Redis 连接池、ARQ 连接、httpx.AsyncClient（复用连接）
    app.state.registry = load_registry(settings.models_config)
    yield
    # TODO(M1): 优雅关闭连接


app = FastAPI(
    title="DeepDocParse gateway",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(parse.router, prefix="/v1")
app.include_router(chat.router, prefix="/v1")

# TODO(M4): Prometheus instrumentator
# from prometheus_fastapi_instrumentator import Instrumentator
# Instrumentator().instrument(app).expose(app)
