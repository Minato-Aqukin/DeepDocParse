"""DeepDocParse-Web backend 入口。

鉴权边界在本层（ARCHITECTURE.md 决策 #2/#3）：
- Web 用户：JWT session
- 第三方开发者：API key（sk-xxx）
- 对 DeepDocParse：统一以 SERVICE_TOKEN 转发，service 不感知用户
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import get_sessionmaker
from app.errors import install_error_handlers
from app.metering import RateLimiter
from app.reconcile import reconcile_loop
from app.routers import apikeys, auth, files, internal, proxy, tasks, usage
from app.service_client import ServiceClient, new_http_client
from app.storage import MinioStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = new_http_client()
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MinioStorage()
    app.state.rate_limiter = RateLimiter()
    await app.state.storage.ensure_bucket()
    # 对账：启动即跑一次，补上停机期间丢掉的完成回调
    app.state.reconciler = asyncio.create_task(
        reconcile_loop(get_sessionmaker(), app.state.storage, app.state.service_client)
    )
    yield
    app.state.reconciler.cancel()
    try:
        await app.state.reconciler
    except asyncio.CancelledError:
        pass
    await app.state.http.aclose()


app = FastAPI(title="DeepDocParse-Web backend", version="0.1.0", lifespan=lifespan)

install_error_handlers(app)

# dev：Vite 跑在 5173，前后端不同源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(apikeys.router, prefix="/api/keys", tags=["apikeys"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(usage.router, prefix="/api/usage", tags=["usage"])
app.include_router(files.router, tags=["files"])       # /files/{token} 稳定文件 URL（token 即凭证）
app.include_router(internal.router, tags=["internal"]) # /internal/* service 回调
app.include_router(proxy.router, tags=["proxy"])       # /v1/* 对外 API + /mcp 反代


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
