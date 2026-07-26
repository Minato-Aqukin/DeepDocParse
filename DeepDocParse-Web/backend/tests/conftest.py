"""测试装配：SQLite in-memory + 内存对象存储 + respx mock service。

不跑 lifespan（那会连真 MinIO），手工注入 app.state —— 与 DeepDocParse/tests/conftest.py 同一套路。
SQLite 用 StaticPool：`:memory:` 每条连接都是独立库，不共享连接就看不到建好的表。
"""
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db as db
from app.main import app
from app.metering import RateLimiter
from app.models import Base
from app.service_client import ServiceClient
from app.storage import MemoryStorage

SERVICE = "http://127.0.0.1:9000"       # 与 settings.service_url 默认值一致
MCP = "http://127.0.0.1:9100"


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
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
    app.state.rate_limiter = RateLimiter()      # 每个用例一个，计数不互相污染
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
