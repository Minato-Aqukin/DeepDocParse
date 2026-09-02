# 任务与并发模型（§10 的落地）

## 现在有两套队列，这是有意的

| 队列 | 谁的 | 真相在哪 | 跑什么 |
|---|---|---|---|
| `corpus.tasks`（PG） | corpus-worker | **PostgreSQL** | 索引 · 抽取 · 对象回收 |
| ARQ（Redis） | model-gateway | **PostgreSQL**（`corpus.parse_jobs`） | 解析轮询 · 归档 · 网关侧向量缓存 |

§10 明写允许后者：「第一版可使用 PostgreSQL job 表和 `FOR UPDATE SKIP LOCKED`，
**或复用 Redis/ARQ 但把 PostgreSQL 作为任务真相**」。

网关保持 ARQ 的理由是硬的：**它是无状态的**（铁律 5），没有也不该有数据库。
给它一个 PG 连接等于把语料的 schema 变更传染到一个本来只做协议转换的服务。
而"任务真相在 PG"这条由 corpus 侧满足：每个网关任务都对应一行
`corpus.parse_jobs`，`ddp_corpus/reconcile.py` 按它对账 —— Redis 整个丢了，
对账也能把任务重新驱动起来。

## 已经消掉的那一类：进程内任务

合仓前索引、编译、抽取跑在 FastAPI 的 `BackgroundTasks` / `asyncio.create_task`
里，也就是 **API 进程的内存**。滚动发布把进程换掉的那一刻它们静默消失
（企业边界 7）。抽取更糟：它连对账都没有，只能靠启动时把孤儿 run
标成失败（`reset_orphaned_runs`）—— 用户白等一场。

这一类现在**一个都不剩**：`corpus.tasks` 是持久的，进程换掉之后由别的
worker 按租约接管。`reset_orphaned_runs` 随之删除 —— 不需要收尸，
因为没有尸体。

## 状态机与三个机制

```
queued → claimed(generation, lease_until) → running → succeeded / failed
                │
                └─ lease 过期后可被新 worker 接管
```

1. **claim**（`FOR UPDATE SKIP LOCKED`）—— 多副本并行领取不撞车
2. **lease + heartbeat** —— 领取者崩溃后任务可被接管
3. **generation fencing** —— 被判死的旧 worker 醒过来**不能覆盖新结果**

第三条最容易漏，而漏掉它的表现是"偶尔出现一份旧结果"，日志上什么都看不出来。
`services/corpus-worker/tests/test_queue.py::test_stale_worker_cannot_overwrite_a_newer_result`
钉着它（做过变异确认：去掉 `generation` 条件必红）。

## 两层 claim 不是重复

索引任务上有两层：

- **任务级**（`tasks.generation`）—— 哪个 worker 跑这条任务
- **文档级**（`documents.index_generation`）—— 哪次索引的结果能落库

同一份文档可能被"重建索引"、"重解析"、"删了又传回来"各触发一次，
它们是三条不同的任务，但只有最新那次的结果该留下。

## 每种任务分别限并发

§10：**不能共用一个无量纲总并发**。三者的形状差一个数量级：

| 种类 | 默认并发 | 为什么 |
|---|---:|---|
| `index` | 2 | 分块 + 批量向量化，主要是网络等待 |
| `compile` | 1 | 每个视觉原子打一次 VLM，**显存是硬约束** |
| `extract` | 2 | 一次 = N 个字段 × (检索 + 模型调用)，本身已是长任务 |

## 还没做的

- `compile` 与 `knowledge` 两种任务还没有独立的 handler：编译目前跟在索引
  里一起跑（`index_document` 内部调 `compile`），知识生成仍是同步的。
  拆开的收益是能分别限并发与重试 —— 但拆之前要先有并发数字，
  而那要等压测（§12.3）。
- 网关侧 ARQ 链的任务水位没有进 `/metrics`。corpus 侧的 `queue.backlog()`
  已经能报"每种任务的积压数与最老任务年龄"，网关侧还欠一份对应的。
