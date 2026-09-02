"""worker 进程入口。

    ddp-corpus-worker            # 跑全部种类
    ddp-corpus-worker --kinds index,extract

按种类分池、各自限并发（§10）。多副本安全：领取走
`FOR UPDATE SKIP LOCKED`，写结果比 generation。
"""
import argparse
import asyncio
import logging
import sys

import httpx

from ddp_corpus.config import assert_secrets_configured, settings
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.queue import backlog
from ddp_corpus.service_client import new_http_client
from ddp_corpus.storage import MinioStorage
from ddp_core.search import PgVectorIndex

from ddp_worker.handlers import HANDLERS
from ddp_worker.runner import Pool, WorkerState, install_signal_handlers, loop

log = logging.getLogger("ddp.worker")


def _concurrency(kind: str) -> int:
    return {
        "index": settings.task_concurrency_index,
        "compile": settings.task_concurrency_compile,
        "extract": settings.task_concurrency_extract,
    }.get(kind, settings.task_concurrency_default)


async def run(kinds: list[str]) -> None:
    # 与 API 同一条规矩：占位密钥拒绝启动。worker 也带着服务凭据出网
    assert_secrets_configured()

    http: httpx.AsyncClient = new_http_client()
    state = WorkerState(http=http, storage=MinioStorage(), search_index=PgVectorIndex())
    await state.storage.ensure_bucket()

    pools = {k: Pool(k, HANDLERS[k], _concurrency(k)) for k in kinds}
    log.info("worker 启动：%s", {k: p.concurrency for k, p in pools.items()})

    async with get_sessionmaker()() as session:
        log.info("当前队列水位：%s", await backlog(session))

    stopping = asyncio.Event()
    install_signal_handlers(stopping)
    try:
        await loop(pools, state, stopping)
    finally:
        await http.aclose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--kinds", default=",".join(HANDLERS),
                        help="只跑这几种任务（逗号分隔）")
    args = parser.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in HANDLERS]
    if unknown:
        # **不静默跳过**：拼错一个种类的后果是那类任务永远没人跑，
        # 而队列水位要过一阵子才有人看
        print(f"::error::未知任务种类 {unknown}，已知：{sorted(HANDLERS)}", file=sys.stderr)
        return 1

    asyncio.run(run(kinds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
