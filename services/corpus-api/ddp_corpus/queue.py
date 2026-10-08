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


def _is_dedupe_conflict(exc: IntegrityError) -> bool:
    """判断 IntegrityError 是否来自 `uq_tasks_kind_dedupe`。

    PG 报 `duplicate key value violates unique constraint "uq_tasks_kind_dedupe"`，
    SQLite 报 `UNIQUE constraint failed: tasks.kind, tasks.dedupe_key`。
    任何其它约束（受理行、事件序号、主键……）都必须回到调用方。
    """
    text = str(exc.orig) if exc.orig is not None else str(exc)
    if "uq_tasks_kind_dedupe" in text:
        return True
    lowered = text.lower()
    return ("unique constraint failed" in lowered and "tasks.kind" in lowered
            and "tasks.dedupe_key" in lowered)


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
    # 调用方自己的待写行先在 try 外刷出去：`begin_nested` 进入前会自动 flush
    # 会话里**全部**待写行，那一刷若落在下面的 try 里，调用方撞上的唯一约束
    # （受理行、事件序号……）会被当成"同 dedupe_key 已在队列里"返回 None，
    # 会话随后在 commit 处变成 PendingRollbackError。这里的 IntegrityError
    # 原样回到调用方，由它自己的冲突仲裁处理。
    await session.flush()
    try:
        async with session.begin_nested():
            session.add(task)
        return task
    except IntegrityError as exc:
        # 只认真正的 (kind, dedupe_key) 冲突才返回 None（幂等去重）。
        # 调用方自己的行若撞上别的约束，必须以 IntegrityError 回到调用方做它
        # 自己的冲突仲裁 —— 吞掉的话调用方会以为任务排上了。
        if _is_dedupe_conflict(exc):
            # 同 dedupe_key 的任务已经在队列里 —— 用户连点三次"重建索引"
            # 不该跑三遍
            return None
        raise


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
        .where(Task.kind.in_(kinds), Task.run_after <= now,
               Task.attempts < Task.max_attempts)
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
    """续租。返回 False 说明这条任务已经被别人接管或已被取消，**调用方应当立刻停手**。

    继续跑下去不会出错，但算出来的结果写不进去（generation 对不上），
    白烧 GPU。所以 worker 的长循环里要看这个返回值。

    状态守卫同样是必需的：没有它时，一次恰好在 cancel 之后到达的心跳会把
    cancelled 行**复活**成 running（generation 若因任何原因仍相等）——
    终态被"心跳复活"是比迟到结果更难查的一类错误。
    """
    lease = lease_seconds or settings.task_lease_seconds
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.generation == generation,
               Task.status.in_(("claimed", "running")))
        .values(status="running", lease_until=utcnow() + timedelta(seconds=lease),
                updated_at=utcnow())
    )
    await session.commit()
    return done.rowcount > 0


async def cancel(session: AsyncSession, task_id: str) -> bool:
    """显式取消：queued/claimed/running -> cancelled（终态）。返回是否发生转移。

    **幂等**：已经落终态（succeeded/failed/cancelled）的任务原样不动，返回 False。
    与其它终态写入同一套围栏：

    - UPDATE 只命中活跃状态 —— 一个已被取消的任务不可能被这里"取消第二次"；
    - **generation +1** —— 迟到的 heartbeat/succeed/fail 拿着旧代次会被拒绝，
      旧 worker 醒过来写不进任何东西（只有 lease 没有这道围栏的队列不安全）。

    清 `dedupe_key`（与 succeed/fail 同一口径）：腾出幂等键，用户重新发起同一件
    事时能再排一次，而不是被一条永远不会被领取的 cancelled 行挡住。
    """
    changed = await session.execute(
        update(Task)
        .where(Task.id == task_id,
               Task.status.in_(("queued", "claimed", "running")))
        .values(status="cancelled", error="cancelled", claimed_by=None,
                lease_until=None, dedupe_key=None,
                generation=Task.generation + 1,
                finished_at=utcnow(), updated_at=utcnow())
    )
    await session.commit()
    return changed.rowcount == 1


async def cancel_by_dedupe(session: AsyncSession, *, kind: str, dedupe_key: str) -> bool:
    """按 `(kind, dedupe_key)` 找活跃任务并取消。联邦的 cancel 走这条路径 ——
    执行行/协调行按业务键找队列任务，不必把 Task.id 抄进业务表。"""
    task_id = await session.scalar(
        select(Task.id).where(Task.kind == kind, Task.dedupe_key == dedupe_key,
                              Task.status.in_(("queued", "claimed", "running")))
        .limit(1))
    if task_id is None:
        return False
    return await cancel(session, task_id)


async def succeed(session: AsyncSession, task_id: str, generation: int,
                  degraded: str | None = None) -> None:
    """落成功。generation 对不上或已成终态直接抛 —— 见 StaleGeneration 的说明。

    **状态守卫与代次围栏缺一不可**：取消会把 generation +1，所以迟到的成功
    通常已经对不上；但即使有人拿着取消后的新代次来写（理论上做不到，除非
    调用方主动重读），`status.in_` 也保证不会把 cancelled 改回 succeeded。
    写最终态的入口必须同时具备这两道闸，只留一道时另一道的变异不会被任何测试
    注意到 —— 那正是"旧结果覆盖新结果"这类几乎查不出来的 bug 的温床。
    """
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.generation == generation,
               Task.status.in_(("claimed", "running")))
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

    **单条条件 UPDATE**：`generation` + 活跃状态都在 WHERE 里，cancel 插在
    "读行"与"写失败"之间时 cancel 赢 —— 迟到的失败写不进终态（StaleGeneration），
    而不是把 cancelled 复活成 queued/failed。
    """
    row = await session.execute(
        select(Task.attempts, Task.max_attempts)
        .where(Task.id == task_id, Task.generation == generation,
               Task.status.in_(("queued", "claimed", "running")))
    )
    current = row.one_or_none()
    if current is None:
        raise StaleGeneration(f"任务 {task_id} 的 generation 已经不是 {generation}")
    attempts, max_attempts = current

    now = utcnow()
    if retry and attempts < max_attempts:
        backoff = min(2 ** attempts, 300)
        done = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.generation == generation,
                   Task.status.in_(("queued", "claimed", "running")))
            .values(status="queued", error=error,      # 留着：排查时要看得到每次为什么失败
                    lease_until=None, claimed_by=None,
                    run_after=now + timedelta(seconds=backoff), updated_at=now)
        )
    else:
        done = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.generation == generation,
                   Task.status.in_(("queued", "claimed", "running")))
            .values(status="failed", error=error,
                    lease_until=None, dedupe_key=None,      # 允许人工重排
                    finished_at=now, updated_at=now)
        )
    await session.commit()
    if done.rowcount == 0:
        # 第二个 UPDATE 没命中：cancel（或接管）插在了"读行"与"写失败"之间，
        # cancel 赢 —— 统一报 StaleGeneration，调用方丢弃本次失败写入。
        raise StaleGeneration(f"任务 {task_id} 的 generation 已经不是 {generation}")


async def sweep_exhausted(session: AsyncSession, *, limit: int = 100) -> int:
    """把耗尽重试次数的残留行落成可见的 failed（poison-pill 死信）。

    claim 永远不领 `attempts >= max_attempts` 的行（毒丸不该反复占 worker），
    而 `fail(retry=True)` 只在被调用时才把超限行落终态 —— 从没再被领起的行
    会永远停在 queued/claimed/running，对用户是"一直在处理中"。清扫把这类
    租约已过期（或无租约）的超限行标成 `failed`（error 形如 `poison: …`），
    并清空 `dedupe_key`（与 fail 同一口径：腾出幂等键，允许人工重排）。

    只动租约已过期或无租约的行：还在租约内的 claimed/running 正在被 worker
    处理，清扫不得抢它的活。
    """
    now = utcnow()
    # UPDATE…LIMIT 不是可移植语法：先按主键查出一批，再按主键圈定更新。
    ids = (await session.execute(
        select(Task.id)
        .where(Task.status.in_(("queued", "claimed", "running")),
               Task.attempts >= Task.max_attempts,
               (Task.lease_until.is_(None)) | (Task.lease_until < now))
        .order_by(Task.run_after)
        .limit(limit)
    )).scalars().all()
    if not ids:
        return 0
    marked = await session.execute(
        update(Task)
        .where(Task.id.in_(ids),
               Task.status.in_(("queued", "claimed", "running")),
               Task.attempts >= Task.max_attempts)
        .values(status="failed",
                error="poison: attempts exhausted (max_attempts reached)",
                lease_until=None, claimed_by=None, dedupe_key=None,
                finished_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return marked.rowcount

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
    return status in ("succeeded", "failed", "cancelled")
