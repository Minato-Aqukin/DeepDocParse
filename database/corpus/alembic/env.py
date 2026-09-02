"""Alembic 环境：连接串取自 ddp_corpus.config.settings（与运行时同一份配置，避免两处漂移）。

**这里只管 corpus schema。** control schema 由 Go 自己的迁移器管
（`database/control/`）—— 让 Go 的表由 Python 的迁移工具管理，
就等于让 Python 有权改 Go 的 schema，而"一个数据对象只能有一个写入所有者"
正是这次重构的第五条边界。两套迁移各管各的，**没有跨 schema 外键**，
因此两边的发布顺序互不依赖。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from ddp_corpus.config import settings
from ddp_corpus.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
