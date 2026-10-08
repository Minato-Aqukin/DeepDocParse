"""ddp_core 的测试装配。

这些用例验的是**纯逻辑**（分块 / 块类型 / 编译 / 图谱 / 抽取 schema / 裁图串行化），
一律进程内跑完：不需要 PG、不需要 Redis、不需要模型运行时。
需要 ORM 的用例走 SQLite in-memory（`ddp_core.types.Vector` 在非 PG 方言上退回 JSON）。
"""
import pytest


@pytest.fixture
def sqlite_sessionmaker():
    """SQLite in-memory 的 async sessionmaker，只建这些用例要的三张表。

    Base 是跨包共享的 registry：ddp_core 里另有模型引用 corpus-api 才定义的
    表（例如 messages）。整份 create_all 会要求 corpus-api 也装着，而 ddp_core
    的 CI 作业只装 ddp_core 自己 —— 所以这里显式点名表，不让叶子包的测试
    依赖上层服务包。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ddp_core.models import Base, Chunk, Document, ParseJob

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [Document.__table__, ParseJob.__table__, Chunk.__table__]

    async def _make():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        return async_sessionmaker(engine, expire_on_commit=False)

    return _make
