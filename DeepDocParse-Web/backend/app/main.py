"""DeepDocParse-Web backend 入口。

鉴权边界在本层（ARCHITECTURE.md 决策 #2/#3）：
- Web 用户：JWT session
- 第三方开发者：API key（sk-xxx）
- 对 DeepDocParse：统一以 SERVICE_TOKEN 转发，service 不感知用户

对 service 的耦合面只有两处：解析契约（openapi.yaml）与 OpenAI 兼容的 embedding/chat
端点（后者可配置成任意兼容服务，见 ADR #17）。检索索引、分块、问答编排全在本层。
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import assert_secrets_configured, settings
from app.db import get_engine, get_sessionmaker
from app.errors import install_error_handlers
from app.metering import MemoryRateLimiter, RedisRateLimiter
from app.reconcile import reconcile_loop
from app.routers import (
    apikeys, auth, conversations, documents, extractions, files, internal, proxy,
    search, usage,
)
from app.search import PgVectorIndex
from ddp_core.tokenize import backend as tokenize_backend
from app.service_client import ServiceClient, new_http_client
from app.storage import MinioStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 第一件事：占位密钥直接拒绝启动。带着 change-me 跑起来的话，
    # 鉴权是形同虚设的，而且运行时不会有任何报错
    assert_secrets_configured()
    app.state.http = new_http_client()
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MinioStorage()
    app.state.search_index = PgVectorIndex()

    # 多副本：限速计数与对账选主都要跨进程共享，否则每个副本各限各的（等于限速×副本数）
    app.state.redis = None
    if settings.redis_url:
        import redis.asyncio as aioredis
        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.rate_limiter = RedisRateLimiter(app.state.redis)
    else:
        app.state.rate_limiter = MemoryRateLimiter()

    # 分词器实现进启动日志。**换 tokenizer 会静默毁掉关键词路**：
    # text_tokenized 是索引时用当时的 backend 切好存的，查询用现在的 backend 切；
    # jieba 从"装着"变成"没装"（或反过来）之后两边词面零交集 -> 关键词召回归零，
    # 而 index_status 不会变、没有任何报错。至少让它在日志里留一行
    print(f"[startup] tokenizer backend = {tokenize_backend()}"
          f"（换实现后必须 reindex，否则关键词检索会静默失效）")

    await app.state.storage.ensure_bucket()
    # 抽取的后台任务活在进程内存里，重启就没了 —— 把卡住的 run 如实标成失败，
    # 否则界面上会永远转圈（抽取没有远端可对账，只能这样兜）
    await extractions.reset_orphaned_runs()
    # 对账：启动即跑一次，补上停机期间丢掉的完成回调与索引投递
    app.state.reconciler = asyncio.create_task(
        reconcile_loop(get_sessionmaker(), app.state.storage, app.state.service_client,
                       app.state.http, app.state.redis)
    )
    yield
    app.state.reconciler.cancel()
    try:
        await app.state.reconciler
    except asyncio.CancelledError:
        pass
    await app.state.http.aclose()
    if app.state.redis is not None:
        await app.state.redis.aclose()


app = FastAPI(title="DeepDocParse-Web backend", version="0.2.0", lifespan=lifespan)

install_error_handlers(app)

# dev：Vite 跑在 5173，前后端不同源。生产换域名只改 CORS_ORIGINS，不动代码。
# 不用 allow_origins=["*"]：配合 allow_credentials=True 时浏览器会直接拒绝，
# 而且那等于放弃同源保护
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(apikeys.router, prefix="/api/keys", tags=["apikeys"])
app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
app.include_router(conversations.router, prefix="/api", tags=["qa"])
app.include_router(search.router, prefix="/api", tags=["search"])
app.include_router(extractions.router, prefix="/api", tags=["extractions"])
app.include_router(usage.router, prefix="/api/usage", tags=["usage"])
app.include_router(files.router, tags=["files"])       # /files/{token} 稳定文件 URL（token 即凭证）
app.include_router(internal.router, tags=["internal"]) # /internal/* service 回调
app.include_router(proxy.router, tags=["proxy"])       # /v1/* 对外 API + /mcp 反代


Instrumentator().instrument(app).expose(app)   # /metrics，与 service 侧口径一致


@app.get("/healthz")
async def healthz():
    """存活探针：进程还在就算活着，不查依赖（依赖挂了不该被重启）。"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """就绪探针：依赖不通就别往这个副本上导流量。"""
    from sqlalchemy import text

    checks: dict[str, str] = {}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await app.state.storage.exists("readyz-probe")
        checks["minio"] = "ok"
    except Exception as exc:
        checks["minio"] = f"error: {type(exc).__name__}"
    try:
        resp = await app.state.http.get(f"{settings.service_url}/healthz")
        checks["service"] = "ok" if resp.status_code == 200 else f"status {resp.status_code}"
    except Exception as exc:
        checks["service"] = f"error: {type(exc).__name__}"
    if app.state.redis is not None:
        try:
            await app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ready else 503,
                        content={"ready": ready, "checks": checks})
