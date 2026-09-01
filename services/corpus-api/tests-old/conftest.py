"""测试装配：SQLite in-memory + 内存对象存储 + 内存检索 + respx mock service。

不跑 lifespan（那会连真 MinIO/PG），手工注入 app.state —— 与 DeepDocParse/tests/conftest.py 同套路。
SQLite 用 StaticPool：`:memory:` 每条连接都是独立库，不共享连接就看不到建好的表。

向量检索在 SQLite 上做不了（pgvector 是 PG 扩展），所以注入 MemoryIndex：
真实 pgvector 路径由 scripts/e2e_web.py 覆盖。这样单测仍然不需要任何外部依赖。
"""
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import ddp_corpus.db as db
from ddp_corpus.config import settings

# bcrypt 成本因子降到最低：默认 12 时一次 hash+verify 要 0.37s，而几乎每个用例都要
# 过 auth_client 注册一个用户 —— 光这一项就占掉整个套件大半时间。
# 必须在 import app.main 之前设好（模块级代码可能已经开始建密码哈希）。
# 生产值在 app/config.py，这里只影响测试进程。
settings.bcrypt_rounds = 4
# 编译指纹测试必须模拟可追溯部署；另有专门用例把模型置空，验证
# provider_unresolved，不能让整套测试无意中都跑在不可比较的默认模型下。
settings.embedding_model = "test-embedding"
settings.chat_model = "test-vision"
# 既有问答用例只测试检索/回答/核对；是否检索判定由阶段 6 专门用例显式开启，
# 避免每条旧用例都被迫 mock 第二种 JSON chat 响应。
settings.qa_decision_enabled = False

from ddp_corpus.main import app  # noqa: E402
from ddp_corpus.metering import MemoryRateLimiter
from ddp_corpus.models import Base
from ddp_core.search import MemoryIndex
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import MemoryStorage

SERVICE = "http://127.0.0.1:9000"       # 与 settings.service_url 默认值一致
MCP = "http://127.0.0.1:9100"
EMBEDDINGS = f"{SERVICE}/v1/embeddings"
CHAT = f"{SERVICE}/v1/chat/completions"


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})

    # SQLite 默认**不强制外键**，而生产是 PG。不开这个 pragma，
    # "删父行时子行还挂着"这类 bug 单测全都放行，只有真机 e2e 才炸（已经被咬过一次）
    @event.listens_for(eng.sync_engine, "connect")
    def _enforce_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 直接替换 db 模块的缓存，让 get_session 依赖走测试库
    db._engine, db._sessionmaker = eng, async_sessionmaker(eng, expire_on_commit=False)
    yield eng
    await eng.dispose()
    db.reset_engine()


@pytest.fixture
async def app_state(engine):
    app.state.http = httpx.AsyncClient(timeout=5.0, trust_env=False)
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MemoryStorage()
    app.state.rate_limiter = MemoryRateLimiter()   # 每个用例一个，计数不互相污染
    app.state.search_index = MemoryIndex()
    app.state.redis = None                          # 单实例模式（多副本路径由 e2e 覆盖）
    yield app.state
    await app.state.http.aclose()


@pytest.fixture
async def client(app_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://web",
                                 trust_env=False) as c:
        yield c


@pytest.fixture
async def session(engine):
    async with db.get_sessionmaker()() as s:
        yield s


async def register(client: httpx.AsyncClient, username: str = "alice",
                   password: str = "correct horse battery") -> dict:
    resp = await client.post("/api/auth/register",
                             json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def auth_client(client):
    """已登录的前端视角客户端（JWT）。"""
    token = (await register(client))["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
async def api_key(auth_client) -> dict:
    """第三方开发者视角的 sk- key（创建后把客户端切回未登录状态）。"""
    created = (await auth_client.post("/api/keys", json={"name": "sdk"})).json()
    auth_client.headers.pop("Authorization", None)
    return created
