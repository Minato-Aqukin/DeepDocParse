"""语料 API 入口。

## 它不做鉴权

账号、API key、配额、限速全在 `services/control-api`（Go）。本服务只信任
入口下发的 actor 上下文头，且要求服务凭据 —— 见 `ddp_corpus/deps.py`。
**只对内网开放**：暴露到公网等于任何人都能自称 admin。

## 对外的三个耦合面

1. 模型网关的契约 `packages/contracts/openapi/gateway-v1.yaml`
2. OpenAI 兼容的 embedding / chat 端点（可配成任意兼容服务）
3. control-api 的两个内部端点（稳定文件 URL、actor 显示名）

检索索引、分块、证据、问答编排全在本服务 —— **evidence/citation 的唯一实现
留在 Python**（风险台账：Go 重写证据规则 -> 假出处）。
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from ddp_corpus.config import assert_secrets_configured, settings
from ddp_corpus.db import get_engine, get_sessionmaker
from ddp_corpus.errors import install_error_handlers
from ddp_corpus.reconcile import reconcile_loop
from ddp_corpus.routers import (
    conversations, documents, external, extractions, internal, knowledge, search,
)
from ddp_core.search import PgVectorIndex
from ddp_core.tokenize import backend as tokenize_backend
from ddp_corpus.service_client import ServiceClient, new_http_client
from ddp_corpus.storage import MinioStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 第一件事：占位密钥直接拒绝启动。带着 change-me 跑起来的话，
    # 鉴权是形同虚设的，而且运行时不会有任何报错
    assert_secrets_configured()
    app.state.http = new_http_client()
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MinioStorage()
    app.state.search_index = PgVectorIndex()

    # 多副本：对账选主要跨进程共享。
    # **限速不在这里** —— 它整体迁去了 control-api（入口按 key 限速 +
    # 按路由类别的领域限速）。两处各限一次只会让"到底是谁把我限了"
    # 变成一个没人答得上的问题
    app.state.redis = None
    if settings.redis_url:
        import redis.asyncio as aioredis
        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    # 分词器实现进启动日志。**换 tokenizer 会静默毁掉关键词路**：
    # text_tokenized 是索引时用当时的 backend 切好存的，查询用现在的 backend 切；
    # jieba 从"装着"变成"没装"（或反过来）之后两边词面零交集 -> 关键词召回归零，
    # 而 index_status 不会变、没有任何报错。至少让它在日志里留一行
    print(f"[startup] tokenizer backend = {tokenize_backend()}"
          f"（换实现后必须 reindex，否则关键词检索会静默失效）")

    await app.state.storage.ensure_bucket()
    # **没有"启动时给孤儿任务收尸"这一步了。** 索引与抽取都排在持久队列里
    # （corpus.tasks），进程换掉之后由别的 worker 按租约接管 —— 不需要收尸，
    # 因为没有尸体（企业边界 7）。
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


app = FastAPI(title="DeepDocParse Corpus API", version="1.0.0", lifespan=lifespan)

install_error_handlers(app)

# **没有 CORS 中间件。** 浏览器不直接访问本服务 —— 它只对内网开放，
# 前端一律经 control-api。加了 CORS 反而是个信号，说明有人打算把它暴露出去。

app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
app.include_router(conversations.router, prefix="/api", tags=["qa"])
app.include_router(search.router, prefix="/api", tags=["search"])
app.include_router(extractions.router, prefix="/api", tags=["extractions"])
app.include_router(knowledge.router, prefix="/api", tags=["knowledge"])
app.include_router(internal.router, tags=["internal"]) # /internal/* 回调与事件
# 对外解析平面在语料侧的那一半：它会在语料里留下 Document 与 ParseJob，
# 而那两张表 Go 一个字都写不了。**只有 /v1/parse\*** —— 其余 /v1/* 是纯算力，
# 入口直接代给模型网关（见 routers/external.py 的模块说明）
app.include_router(external.router, tags=["external"])

# **没有 /api/auth、/api/keys、/api/usage、/files、/mcp。**
# 它们全在 control-api：账号与计量归控制面，稳定文件 URL 的凭证住在
# control schema，MCP 由入口统一鉴权后反代。


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
