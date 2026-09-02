"""持久任务队列 —— 入队 / 领取 / 续租 / 落终态。

## 为什么是 PostgreSQL 而不是 Kafka

§10 说得很直白：**没有容量证据前不引 Kafka**。这个队列的全部需求是
"几十到几千个任务、要能被多副本安全领取、崩溃后能接管"，
而 `FOR UPDATE SKIP LOCKED` 恰好把这件事做对，还顺带白拿了：

- 任务状态与业务数据在**同一个数据库**里，可以在一个事务里一起改
- 失败原因、重试次数、领取者都能直接 SQL 查出来（运维不用另学一套工具）
- 不用维护第二套存储的可用性

## 三个必须一起出现的机制

1. **claim**（`FOR UPDATE SKIP LOCKED`）：多副本并行领取不会撞车
2. **lease**：领取者崩溃后，租约到期任务可被接管
3. **generation fencing**：被判死的旧 worker 醒过来之后**不能覆盖新结果**

前两个是常识，第三个最容易漏 —— 而漏掉它的表现是"偶尔出现一份旧结果"，
几乎查不出来。所以本模块里所有写结果的入口都强制要求传 `generation`。
"""
import socket
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_contracts import TASK_KIND_VALUES, TASK_STATUS_VALUES
from ddp_corpus.config import settings
from ddp_corpus.models import Task, as_aware, new_id, utcnow

#: 领取者身份。带上主机名与进程号，排查"是哪个副本卡住了"时有用。
WORKER_ID = f"{socket.gethostname()}:{__import__('os').getpid()}"


class StaleGeneration(RuntimeError):
    """写结果时 generation 对不上 —— 这条任务已经被别人接管了。

    **必须是异常而不是返回 False**：调用方漏判返回值的后果是旧结果覆盖新结果，
    而那不会报错。异常至少会在日志里留下痕迹。
    """


async def enqueue(session: AsyncSession, *, kind: str, payload: dict,
                  organization_id: str = "", dedupe_key: str | None = None,
                  max_attempts: int = 3, delay_seconds: float = 0) -> Task | None:
    """排一个任务。已有同 `(kind, dedupe_key)` 的未完成任务则返回 None（幂等）。

    **不 commit** —— 由调用方连同业务写入一起提交。这样"改了状态但没排上队"
    与"排上队了但状态没改"两种半截状态都不可能出现。
    """
    if kind not in TASK_KIND_VALUES:
        raise ValueError(f"未知任务种类 {kind!r}（契约允许：{TASK_KIND_VALUES}）")

    task = Task(id=new_id(), kind=kind, payload=payload,
                organization_id=organization_id, dedupe_key=dedupe_key,
                max_attempts=max_attempts,
                run_after=utcnow() + timedelta(seconds=delay_seconds))
    try:
        async with session.begin_nested():
            session.add(task)
        return task
    except IntegrityError:
        # 同 dedupe_key 的任务已经在队列里 —— 用户连点三次"重建索引"
        # 不该跑三遍
        return None


async def claim(session: AsyncSession, kinds: list[str], *, limit: int = 1,
                lease_seconds: int | None = None) -> list[Task]:
    """领取一批可执行的任务。

    `FOR UPDATE SKIP LOCKED` 让多个副本并行领取而不互相阻塞，也不会把同一条
    领两次。**SQLite 不支持 SKIP LOCKED**，单测走的是同一段 SQL 的退化版本
    （单进程里没有并发领取，行为等价）。
    """
    lease = lease_seconds or settings.task_lease_seconds
    now = utcnow()
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"

    stmt = (
        select(Task)
        .where(Task.kind.in_(kinds), Task.run_after <= now)
        .where(
            # 还没人领 / 租约已过期（领取者崩了）
            (Task.status == "queued")
            | ((Task.status.in_(("claimed", "running"))) & (Task.lease_until < now))
        )
        .order_by(Task.run_after)
        .limit(limit)
    )
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    rows = (await session.execute(stmt)).scalars().all()
    claimed: list[Task] = []
    for task in rows:
        # **每次接管 generation +1** —— 这是 fencing token 的全部机制
        task.generation += 1
        task.status = "claimed"
        task.claimed_by = WORKER_ID
        task.lease_until = now + timedelta(seconds=lease)
        task.attempts += 1
        task.updated_at = now
        claimed.append(task)
    await session.commit()
    return claimed


async def heartbeat(session: AsyncSession, task_id: str, generation: int,
                    lease_seconds: int | None = None) -> bool:
    """续租。返回 False 说明这条任务已经被别人接管，**调用方应当立刻停手**。

    继续跑下去不会出错，但算出来的结果写不进去（generation 对不上），
    白烧 GPU。所以 worker 的长循环里要看这个返回值。
    """
    lease = lease_seconds or settings.task_lease_seconds
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.generation == generation)
        .values(status="running", lease_until=utcnow() + timedelta(seconds=lease),
                updated_at=utcnow())
    )
    await session.commit()
    return done.rowcount > 0


async def succeed(session: AsyncSession, task_id: str, generation: int,
                  degraded: str | None = None) -> None:
    """落成功。generation 对不上直接抛 —— 见 StaleGeneration 的说明。"""
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.generation == generation)
        .values(status="succeeded", degraded=degraded, error=None,
                dedupe_key=None,      # 腾出幂等键，下次同样的任务能再排
                finished_at=utcnow(), updated_at=utcnow())
    )
    await session.commit()
    if done.rowcount == 0:
        raise StaleGeneration(f"任务 {task_id} 的 generation 已经不是 {generation}")


async def fail(session: AsyncSession, task_id: str, generation: int, error: str,
               *, retry: bool = True) -> None:
    """落失败或安排重试。

    **失败原因必须持久化**（§10）：只写日志的话，用户看到的是"一直在处理中"，
    而运维看到的是一堆没人关联得起来的报错。

    重试用指数退避，超过 `max_attempts` 就落 failed 并停手 —— 无限重试会让
    一个必然失败的任务永远占着 worker。
    """
    task = await session.get(Task, task_id)
    if task is None or task.generation != generation:
        raise StaleGeneration(f"任务 {task_id} 的 generation 已经不是 {generation}")

    if retry and task.attempts < task.max_attempts:
        backoff = min(2 ** task.attempts, 300)
        task.status = "queued"
        task.error = error          # 留着：排查时要看得到"它试过几次、每次为什么失败"
        task.lease_until = None
        task.claimed_by = None
        task.run_after = utcnow() + timedelta(seconds=backoff)
    else:
        task.status = "failed"
        task.error = error
        task.lease_until = None
        task.dedupe_key = None      # 允许人工重排
        task.finished_at = utcnow()
    task.updated_at = utcnow()
    await session.commit()


async def backlog(session: AsyncSession) -> dict[str, dict]:
    """队列水位：每种任务的积压数与最老任务年龄。

    **最老任务年龄比积压数更能说明问题**：积压 100 可能只是刚来一批，
    最老一条 20 分钟没被领走才是故障（§13 的核心指标之一）。
    """
    out: dict[str, dict] = {}
    for kind in TASK_KIND_VALUES:
        rows = (await session.execute(
            select(Task.created_at).where(Task.kind == kind,
                                          Task.status.in_(("queued", "claimed", "running")))
            .order_by(Task.created_at).limit(1000)
        )).scalars().all()
        oldest = (utcnow() - as_aware(rows[0])).total_seconds() if rows else 0.0
        out[kind] = {"pending": len(rows), "oldest_seconds": oldest}
    return out


def is_terminal(status: str) -> bool:
    if status not in TASK_STATUS_VALUES:
        raise ValueError(f"未知任务状态 {status!r}")
    return status in ("succeeded", "failed")
