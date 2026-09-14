# P5 联邦平面真 PostgreSQL 验证记录（v3）

> 2026-09-14。范围：P5 队列/受理/执行/交付与 P6 有界缓存在**真 PostgreSQL**
> （一次性 `pgvector/pgvector:pg16` 容器）上的验证 —— SQLite 单测结构上测不到的
> 迁移链、JSON 往返、FK/唯一约束仲裁、跨会话并发与清扫竞态。
>
> 工作树未 commit，因此本文件是**自验记录**，不构成 commit 前的独立验收。
> 2026-09-14 第二轮独立复核修复后已复跑（见 §8）。复跑脚本：
> `bash scripts/check_federation_pg.sh`。

## 1. 环境与结果

| 项 | 值 |
|---|---|
| 容器 | `ddp-r2c-pg`（`pgvector/pgvector:pg16`，PG **16.15**），回环 `127.0.0.1:15467`，退出即删 |
| 数据库 | `deepdocparse`（用户 `ddp`；不使用/不触碰 dev 卷与 15432/15439/15450/15455） |
| 迁移 head | **0032**（单 head；链上 `0030 → 0032`，**没有 0031**，与 P6 文档一致） |
| 新套件 | `test_federation_pg.py` **2 passed** + `test_federation_concurrency_pg.py` **10 passed**（第二轮复核新增队列死任务清扫场景） |
| 回归（同一 scratch 库） | collection catalog 2 + client projection 1 + cache 2 = **5 passed** |
| 默认套件（无 env var） | **749 passed, 17 skipped**（第二轮复核后的数字；本切片开始时基线 732/5） |
| ruff（F,B） | 全绿 |

一条命令跑完并打印逐条结果：`bash scripts/check_federation_pg.sh`
（容器创建 → `alembic upgrade head` → 两个新套件 → 三个既有 opt-in PG 套件 →
删除容器；脚本可重复执行）。

### 关于 JSON 列的真实类型

任务书写的是 "JSONB round-trips"；实测**列类型是 PostgreSQL `json`，不是 `jsonb`**
（`select pg_typeof(counts_json)` → `json`）。迁移 0027/0032 用的是 `sa.JSON`，
ORM 也没换 PG 专有类型。往返验证因此断言 `json`（并接受 `jsonb`），并额外做
**摘要级**验证：交付文档从库里读回后重算 `plans.digest(document)` 必须等于
`result_manifest_digest` —— 任何截断/类型强转都会让摘要对不上。

## 2. 逐场景证据

### 场景 1 · 迁移演练（`test_migration_drill_head_orm_drift_and_one_step_down`）

- 空库 `alembic upgrade head` 后：`alembic_version = 0032`（脚本另有
  `alembic current` / `alembic heads` 输出），`ScriptDirectory.get_heads()` 长度 1。
- 十张表全部存在：`federation_probes / admissions / executions / requests /
  task_events`、`coverage_ledgers / coverage_entries`、`federation_deliveries`、
  `federation_cache_entries`、`tasks`。
- **约束/FK**：5 个唯一约束逐个断言（`uq_federation_admissions_org_idempotency`、
  `uq_federation_requests_org_idempotency`、`..._org_intent_idempotency`、
  `uq_federation_task_events_seq`、`uq_federation_cache_scope_key`）；
  `federation_executions.admission_id → federation_admissions` 外键存在；
  `coverage_entries.root_task_id → coverage_ledgers` 是 `ON DELETE CASCADE`。
- **ORM/迁移零漂移**：对十张表的每一列比较列集合、nullability 与类型族
  （`String(n)` / `DateTime(timezone)` / `Text` / `Integer` / `JSON` 归一化后比较，
  避免 PG 反射名 `TIMESTAMP`/`VARCHAR` 造成的假红）。漂移列表为空。
- **降一版再升回**：按迁移脚本现算的 `head` 取 `down_revision`（=0030），用
  **真 alembic CLI**（`database/corpus/alembic`，`env.py` 里是 `asyncio.run`，
  在 pytest 事件循环里跑不了）降级一版，断言 `version_num == 0030` 且
  `federation_cache_entries` 被删；`finally` 里升回 head，断言表恢复、漂移仍为空。

### 场景 2 · 协调者端到端（`test_coordinator_end_to_end_on_pg_queue_path`）

HTTP 应用 + 真 PG sessionmaker 覆盖（复用 `test_collection_catalog_pg.py` 的
override 模式），生产 `PgVectorIndex`，真实发布集合。显式 manifest 把 scope 钉在
本用例的集合上（避免 `site_public` 把历史发布集合都算进分母）。

1. `POST /task-intents → /task-plans → /task-plans/{id}/approve → /tasks`；
   `/tasks` 回 **202 + running + result=null**，且 `federation_plan` 队列任务与
   running 状态**同事务**（`dedupe_key=federation-request:<root>`）。
2. `drain_tasks` 走 worker 真实 handler（`federation_plan` → 协调者就地执行本地目标
   → 受理行排出的 `federation_execute` 再被领取）；断言 `federation_execute`
   任务 payload 的 `executor_task_id` 与执行行一致且最终 `succeeded`。
3. `GET /tasks/{id}`：终态 `succeeded`、`counts.succeeded == 1`、证据 id 等于
   夹具证据 id；`result_manifest_digest` = 对结果文档重算的 `plans.digest`。
4. `GET /tasks/{id}/coverage`：`counts.total_targets == 1`、条目 `succeeded`；
   **JSON 往返**：DB 里 `pg_typeof(counts_json)` + 列值与 HTTP 响应逐字段相等。
5. `GET /deliveries/{id}`：`result` 非空，重算摘要 == `result_manifest_digest`，
   DB 列值与响应相等；`ack` 后 `confirmed`，任务 `delivery_state=confirmed`。
6. 受理/执行行各**恰好 1 条**（同键重放不会造第二次执行）。

### 场景 3 · 受理幂等并发（`test_admission_same_key_race_one_row_replay_and_conflict`）

- **真实预检查双漏**：用 `asyncio.Barrier(2)` 顶掉 `catalog.lock_key`，两个独立
  会话同时越过 SELECT 后并发 INSERT —— 只有唯一约束能仲裁。断言：恰好
  1 条 admission、1 条 execution、1 条 `federation_execute` 任务；
  两个结果的 `(receipt, created)` 一个 True 一个 False，且**两个 receipt 完全相等**
  （败者重放胜者回执，不重跑）。
- **advisory lock 正常路径**：不顶掉锁再赛一次，锁串行化后败者读到已提交行，
  同样一胜一败；两个不同幂等键各得一条 admission。
- **同键不同摘要**：409 `idempotency_conflict`，不产生第二条回执。

### 场景 4 · 租约/fencing（队列与执行行两侧）

- `test_concurrent_claims_of_one_task_have_exactly_one_winner`：两个并发
  `queue.claim` 对同一条任务，恰好一个拿到；`generation=1`、`attempts=1`。
- `test_claim_skips_a_row_locked_by_another_transaction`：另一事务持
  `FOR UPDATE` 行锁时，`claim` 必须**跳过**并在 5s 内返回空（没有 SKIP LOCKED
  就会等锁/超时）—— 这条是 SKIP LOCKED 的确定性守卫。
- `test_cancel_between_claim_and_succeed_fences_the_stale_writer`：A 会话领取后，
  B 会话取消；A 拿旧 generation `succeed` 抛 `StaleGeneration`，行保持
  `cancelled`（generation+1、`dedupe_key` 清空）；再拿取消后的**当前** generation
  也写不进去（状态守卫独立承重）。`cancel` 幂等。
- `test_expired_lease_reclaim_reruns_and_terminal_rows_are_never_touched`：
  过期租约的 claimed 行被重新领取（generation=2、attempts=2），
  旧持有者 generation=1 的写入被拒；`succeeded` 行即便租约过期也**永不被领取**，
  其 `degraded`/`attempts` 原样保留。
- `test_federation_execute_takes_over_expired_lease_and_keeps_terminal_results`：
  `running` + 过期租约的执行被 `federation.execute` 接管（generation 2→3、租约清空）；
  终态执行再 `execute` 只读返回，`result_json` 的哨兵值分毫不动。

### 场景 5 · 缓存并发上限（`test_cache_caps_hold_under_parallel_writer_sessions`、
`test_cache_expired_purge_and_concurrent_same_key_upsert`）

- 3 个 scope × 4 个独立会话并发 `put`，`limits=(entries=5, bytes=1200,
  per_scope=2)`：最终总条目 ≤5、总字节 ≤1200、每个 scope ≤2（advisory lock
  串行化上限判定）。
- 过期行 `purge_expired(limit=2)` 只删 2、再删 3，之后归零。
- 同 key 两写者并发 upsert：唯一 `(scope_key, cache_key)` 只有一行，
  读回值等于其一，`hits` 正确 +1。

### 场景 6 · 清扫（`test_sweeper_on_pg_fails_expired_lease_and_stalled_request`）

插入：过期租约的 `running` 执行、过期租约的 `succeeded` 执行、卡死超阈值的
`running` 协调请求、已完成的请求、被卡死 root 的覆盖账本+条目、对应的
`federation_plan` 队列任务、以及另一 root 的队列任务。`sweep_federation_once` 后：

- 过期执行 → `failed + error=lease_expired`（generation+1、租约清空）；
- 卡死请求 → `failed + error=coordinator_stalled`（**覆盖账本与条目原样保留**，
  分母不消失）；
- `succeeded` 执行/请求不被改写；被清扫 root 的队列任务 → `cancelled` 且清
  `dedupe_key`，另一 root 的队列任务保持 `queued`；
- 再扫一遍全 0（幂等）。

## 3. 发现并修复的真缺陷（真 PG 并发才暴露）

**症状**：同键受理的**真实预检查双漏**下，败者不是"重放胜者回执"也不是
409，而是 `sqlalchemy.exc.PendingRollbackError`（HTTP 500 类）。

**根因**：`services/corpus-api/ddp_corpus/queue.py:60-67` 的
`enqueue` 用 `async with session.begin_nested(): session.add(task)` 吞
`IntegrityError`。savepoint 提交时会 flush 会话里**所有**待写行；`_create_admission`
刚 add 的 `federation_admissions` 行先撞
`uq_federation_admissions_org_idempotency`，这个 IntegrityError 被 `enqueue`
当成"任务 dedupe 冲突"吞掉，同时把会话置为 DEACTIVE（`_rollback_exception`）。
随后 `_create_admission` 的 `session.commit()` 抛 `PendingRollbackError`，
`federation.admit` 的 `except IntegrityError`（`federation.py:969`）永远到不了 ——
**代码里写明的"唯一约束替我们仲裁"实际上不工作**。

**修复**（小且局部）：`services/corpus-api/ddp_corpus/federation.py:1080-1088`
在 `queue.enqueue` 之前 `await session.flush()`，让唯一约束冲突以
`IntegrityError` 出现在 `admit` 层，败者走既有的重放/409 分支；savepoint 里
只剩任务一行，`enqueue` 的 dedupe 语义恢复精确。

**触发面**：正常部署下 `pg_advisory_xact_lock` 会串行化同键受理，所以这是
纵深防御失效（锁被跳过/不可用或哈希碰撞时才触发）。但"唯一约束仲裁"是代码
与注释承诺的行为，坏掉时表现是 500 而不是幂等重放，值得修。

**失败用例已先行**：修前 `test_admission_same_key_race_one_row_replay_and_conflict`
红在 `PendingRollbackError`；修后绿。

## 4. 变异确认（改掉被守的行 → 必须红 → 还原并核对哈希）

| # | 变异 | 结果 |
|---|---|---|
| M1 | `federation.py` 删掉 enqueue 前的 `await session.flush()` | **RED**（PendingRollbackError，即上述缺陷） |
| M2 | `cache.py` `_lock` 的 PG 分支改成 `if False:` | **RED**（并发后条目/字节超上限） |
| M3 | `queue.succeed` 的状态守卫放宽到包含 `cancelled` | **RED**（拿取消后的当前代次写成功） |
| M4 | `queue.claim` 去掉 `FOR UPDATE SKIP LOCKED` | **RED**（持锁行被等锁 → 5s 超时） |
| M5 | 清扫执行行的**候选 + 写入**两道闸一起拆 | **RED**（succeeded 行被覆盖） |
| M6 | `federation.execute` 去掉"running 且租约过期"接管分支 | **RED**（接管用例拿不到 succeeded） |
| M7 | 库级：`ALTER TABLE federation_requests ALTER COLUMN error SET NOT NULL` | **RED**（漂移检查报 `nullable orm=True db=False`） |

两条"单拆不红"与 P5 队列切片的记录一致，已改成能承重的形态：

- M4 只去掉锁、保留双领取者竞争**不足以**红（两次 SELECT 可能被往返时序错开）；
  因此新增 `test_claim_skips_a_row_locked_by_another_transaction`：另一事务
  **持锁**时 claim 必须立即返回空 —— 没锁就等锁超时，确定性红。
- M5 只拆写入闸不红（候选查询仍在过滤活跃态）；按 SQLite 切片 M5/M6 的做法用
  **复合变异**（候选+写入一起拆）确认。协调请求侧的 `mark_stalled` 守卫在
  `P5-QUEUE-VALIDATION-v3.md` 的 M6 已做过跨文件复合变异，本套件是同路径的
  真 PG 复刻，未重复改 `federation_tasks.py`（该文件正被并行改动）。

## 5. skip 行为

未设 `FEDERATION_TEST_DATABASE_URL` / `CORPUS_TEST_DATABASE_URL` 时，两个新文件
的 12 条用例各自 `pytest.skip`，消息明确点名环境变量并给出
`scripts/check_federation_pg.sh`。默认套件因此保持全绿：
**749 passed, 17 skipped**，不需要 docker。

## 6. 复跑

```bash
cd DeepDocParse
bash scripts/check_federation_pg.sh                    # 真容器，端到端，自动清理
cd services/corpus-api && ../../.venv/bin/python -m pytest -q   # 无 docker：749/16skip
../../.venv/bin/ruff check services/corpus-api --select F,B \
  --ignore F401,B008,B905,B904,B007
```

只跑新套件（容器已有时）：

```bash
cd services/corpus-api
env FEDERATION_TEST_DATABASE_URL=postgresql+asyncpg://ddp:ddp@127.0.0.1:15467/deepdocparse \
  ../../.venv/bin/python -m pytest -v tests/test_federation_pg.py tests/test_federation_concurrency_pg.py
```

## 7. 仍未验证（不当成已验证）

- **生产量级/多进程**：只有单容器 + 每测试至多 ~12 个独立连接（pool 8/overflow 32）。
  真正的多副本进程池、跨机时钟偏差、`TASK_HEARTBEAT_SECONDS` 长租约的真实续租
  都没跑；租约过期是**注入**的，不是等出来的。
- **连接池上限**：测试池参数非生产值；PG `max_connections` 下的排队行为未测。
- **多副本清扫竞态**：`sweep_federation_loop` 的 Redis 选主未在 PG 上验；只验了
  单次清扫的 SQL 语义。
- **pgvector 向量路**：E2E 夹具的 chunk 没有 embedding，走的是关键词路；
  真向量检索仍以 `scripts/e2e_web.py` 为准。
- **降级演练只走了一版**（0032→0030→head）；到 base 的全链 downgrade 未验。
- 结论对应的是第二轮复核修复后的工作树（2026-09-14）；修复前的记录中
  `federation_tasks.py` 曾被并行切片改动，相关时刻已在 §8 说明。

## 8. 第二轮独立复核修复后的复跑（2026-09-14）

修复 F3（队列死任务清扫）后重跑 `bash scripts/check_federation_pg.sh`：
**12 passed**（`test_federation_pg.py` 2 + `test_federation_concurrency_pg.py`
10，新增 `test_sweeper_on_pg_marks_queued_execution_whose_queue_task_died`）
+ 既有 opt-in 回归 **5 passed**，ALL STEPS PASS。
新 PG 用例钉住 payload JSON 查找（PG 上是 `->>`）、状态迁移到
`failed/queue_task_failed` 与 generation 前移；SQLite 侧的端到端
（handler fail + max_attempts=1 → resume 重跑一次）见
`P5-QUEUE-VALIDATION-v3.md` §10。
