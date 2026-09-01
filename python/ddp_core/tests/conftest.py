"""ddp_core 的测试装配。

这些用例验的是**纯逻辑**（分块 / 块类型 / 编译 / 图谱 / 抽取 schema / 裁图串行化），
一律进程内跑完：不需要 PG、不需要 Redis、不需要模型运行时。
需要 ORM 的用例走 SQLite in-memory（`ddp_core.types.Vector` 在非 PG 方言上退回 JSON）。
"""
import pytest


@pytest.fixture
def sqlite_sessionmaker():
    """SQLite in-memory 的 async sessionmaker —— 建表用共用的 Base.metadata。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ddp_core.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _make():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    return _make
