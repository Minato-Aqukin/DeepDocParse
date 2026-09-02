"""任务循环：每种任务一个池，各自限并发。

## 为什么每种一个池

§10：**embedding、VLM、OCR 分别设置并发与队列，不能共用一个无量纲总并发**。
它们的形状差一个数量级：索引主要是网络等待，编译每个原子打一次 VLM
（显存是硬约束），抽取是 N 次串行调用。共用一个数字的话，要么把便宜的
卡死，要么让贵的把 GPU 打满。

## 心跳与 fencing

跑任务的同时开一个心跳协程续租。心跳返回 False（说明这条任务已经被别人
接管）时**立刻取消任务协程** —— 继续跑不会出错，但算出来的结果写不进去
（generation 对不上），纯粹白烧 GPU。
"""
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from ddp_corpus.config import settings
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Task
from ddp_corpus.queue import StaleGeneration, claim, fail, heartbeat, succeed

log = logging.getLogger("ddp.worker")

#: 一个 handler 拿到 (task, state) 并跑完它。抛异常 = 失败（会被重试）。
Handler = Callable[[Task, "WorkerState"], Awaitable[str | None]]


class WorkerState:
    """跨任务共享的资源（HTTP 客户端、对象存储、检索索引）。"""

    def __init__(self, http, storage, search_index):
        self.http = http
        self.storage = storage
        self.search_index = search_index


class Pool:
    """一种任务的执行池。"""

    def __init__(self, kind: str, handler: Handler, concurrency: int):
        self.kind = kind
        self.handler = handler
        self.concurrency = concurrency
        self._running: set[asyncio.Task] = set()

    @property
    def free(self) -> int:
        return self.concurrency - len(self._running)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def drain(self) -> None:
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)


async def run_one(task: Task, pool: Pool, state: WorkerState) -> None:
    """跑一条任务：起心跳 -> 跑 handler -> 落终态。"""
    sessionmaker = get_sessionmaker()
    generation = task.generation
    stop = asyncio.Event()

    async def beat() -> None:
        """续租。**返回 False 就停手** —— 见模块 docstring。"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), settings.task_heartbeat_seconds)
                return
            except TimeoutError:
                pass
            async with sessionmaker() as session:
                if not await heartbeat(session, task.id, generation):
                    log.warning("任务 %s 已被接管（generation 变了），停手", task.id)
                    stop.set()
                    return

    beater = asyncio.create_task(beat())
    try:
        runner = asyncio.create_task(pool.handler(task, state))
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner not in done:
            # 租约被抢走：取消任务协程，什么都不写
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
            return
        waiter.cancel()
        degraded = runner.result()
        async with sessionmaker() as session:
            await succeed(session, task.id, generation, degraded=degraded)
    except StaleGeneration:
        log.warning("任务 %s 落终态时 generation 已变，丢弃本次结果", task.id)
    except Exception as exc:                              # noqa: BLE001
        log.exception("任务 %s（%s）失败", task.id, task.kind)
        async with sessionmaker() as session:
            try:
                await fail(session, task.id, generation, f"{type(exc).__name__}: {exc}")
            except StaleGeneration:
                log.warning("任务 %s 落失败时 generation 已变，丢弃", task.id)
    finally:
        stop.set()
        beater.cancel()
        try:
            await beater
        except asyncio.CancelledError:
            pass


async def loop(pools: dict[str, Pool], state: WorkerState, stopping: asyncio.Event) -> None:
    """主循环：按各池的空位领任务。"""
    sessionmaker = get_sessionmaker()
    while not stopping.is_set():
        picked = 0
        for pool in pools.values():
            if pool.free <= 0:
                continue
            async with sessionmaker() as session:
                tasks = await claim(session, [pool.kind], limit=pool.free)
            for task in tasks:
                picked += 1
                pool.spawn(run_one(task, pool, state))
        if picked == 0:
            try:
                await asyncio.wait_for(stopping.wait(), settings.task_poll_interval)
            except TimeoutError:
                pass

    # 优雅退出：**不取消在途任务**，等它们跑完。
    # 取消的话它们会以"失败"落库并重试，而它们其实马上就要成功了
    log.info("收到停止信号，等待在途任务收尾")
    for pool in pools.values():
        await pool.drain()


def install_signal_handlers(stopping: asyncio.Event) -> None:
    loopref = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loopref.add_signal_handler(sig, stopping.set)
