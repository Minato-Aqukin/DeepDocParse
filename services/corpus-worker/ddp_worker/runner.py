"""任务循环：每种任务一个池，各自限并发。

## 为什么每种一个池

§10：**embedding、VLM、OCR 分别设置并发与队列，不能共用一个无量纲总并发**。
它们的形状差一个数量级：索引主要是网络等待，编译每个原子打一次 VLM
（显存是硬约束），抽取是 N 次串行调用。共用一个数字的话，要么把便宜的
卡死，要么让贵的把 GPU 打满。

## 心跳与 fencing

跑任务的同时开一个心跳协程续租。租约被接管、续租异常或外层被取消时，
都必须先取消并等待 handler 退出，不能留下继续调用模型或写库的孤儿协程。
续租异常沿用任务失败/退避路径；被接管则不再写终态。
"""
import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable

from ddp_corpus.config import settings
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Task
from ddp_corpus.queue import (
    StaleGeneration, claim, fail, heartbeat, succeed, sweep_exhausted,
)


def max_runtime_seconds(kind: str) -> float:
    """单个 handler 的 deadline（秒）：按 kind 覆盖，否则走全局默认。"""
    by_kind = settings.task_max_runtime_seconds_by_kind or {}
    return float(by_kind.get(kind, settings.task_max_runtime_seconds))

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
    """跑一条任务：起心跳 -> 跑 handler -> 落终态。

    hang 住的 handler 有 deadline 兜底：超过 `max_runtime_seconds(kind)` 后
    不再续租、取消 handler，并以 `task_timeout` 落失败/重试 —— 任务不会永远
    占着租约（活锁），而是按正常失败路径退避重试或落终态。
    """
    sessionmaker = get_sessionmaker()
    generation = task.generation
    deadline = time.monotonic() + max_runtime_seconds(task.kind)

    async def beat() -> None:
        """续租失败必须被主协程观察，不能独自停止而让 handler 继续跑。"""
        while True:
            await asyncio.sleep(settings.task_heartbeat_seconds)
            if time.monotonic() >= deadline:
                # deadline 已过：停掉续租，让主协程去取消 handler。
                # 不写任何终态 —— 终态由主协程的 task_timeout 路径统一写。
                log.warning("任务 %s 超过最大运行时，停止续租", task.id)
                return
            async with sessionmaker() as session:
                if not await heartbeat(session, task.id, generation):
                    log.warning("任务 %s 已被接管（generation 变了），停手", task.id)
                    return

    beater = asyncio.create_task(beat())
    runner = asyncio.create_task(pool.handler(task, state))
    timed_out = False
    try:
        try:
            done, _ = await asyncio.wait({runner, beater}, return_when=asyncio.FIRST_COMPLETED)
            if beater in done:
                # 正常返回表示失去租约（被接管或 deadline 到）；异常则交给既有失败路径持久化。
                beater.result()
                if runner.done():
                    # handler 已经跑完但 beater 先结束：按"被接管"处理，不再写终态。
                    return
                # handler 还没跑完：被接管则直接停手；deadline 到则走超时失败路径。
                timed_out = time.monotonic() >= deadline
                if not timed_out:
                    return
            else:
                # runner 先完成：刚压线（wait 返回时已过 deadline）的完成仍算完成，
                # 不判超时 —— deadline 只掐"还没跑完"的 handler。
                degraded = runner.result()
        finally:
            # 在落终态/重排之前收完子协程，外层取消也必须走这条清理路径。
            runner.cancel()
            beater.cancel()
            await asyncio.gather(runner, beater, return_exceptions=True)
        if timed_out:
            raise TimeoutError(
                f"task_timeout: kind={task.kind} 超过最大运行时 "
                f"{max_runtime_seconds(task.kind)}s")
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
            # 空转时顺手清扫毒丸：耗尽 max_attempts 又没人领的行落成可见 failed，
            # 而不是永远"处理中"。显式函数 + 主循环空闲路径调用，不搭别的顺风车。
            async with sessionmaker() as session:
                swept = await sweep_exhausted(session)
            if swept:
                log.warning("死信清扫：%d 条耗尽任务已标 failed", swept)
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
