"""worker 的测试装配：SQLite in-memory，不需要 PG、不需要对象存储。

队列本身只用到 `select ... for update` 与几条 update —— SQLite 上跑得动
（`SKIP LOCKED` 在那里会被跳过，见 `queue.claim`）。真正的多副本并发领取
由真机 e2e 覆盖。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import ddp_corpus.db as db
from ddp_corpus.config import settings

settings.service_token = "test-service-token"

from ddp_corpus.models import Base  # noqa: E402


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db._engine, db._sessionmaker = eng, async_sessionmaker(eng, expire_on_commit=False)
    yield eng
    await eng.dispose()
    db.reset_engine()


@pytest.fixture
async def session(engine):
    async with db.get_sessionmaker()() as s:
        yield s
