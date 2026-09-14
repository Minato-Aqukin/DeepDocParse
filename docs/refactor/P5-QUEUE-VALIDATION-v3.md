# P5 队列/取消/回收切片验证记录（v3）

> 2026-09-14。范围：`docs/refactor/P5-VALIDATION-v3.md` §5 遗留的
> **「worker 队列 / `task_status=cancelled`」** 缺口，外加企业边界 7 的第二半
> ——"已经死掉的执行不能永远显示成运行中"。
>
> 本轮工作树未 commit，因此按仓库流程这不构成一次正式验收；本文件记录的是
> 实现范围、契约/行为变化、测试计数与逐条变异确认。

## 1. 结论

- `task_status.cancelled` 成为**真实状态**：契约四件套（value/summary/label/
  severity）落在 `enums.yaml`，三语言生成物同步；`queue.cancel`、`claim`、
  `succeed`/`fail`、`heartbeat`、`is_terminal` 与 worker runner 一起认它。
- **受理即排队**：`federation.admit`（peer 端点）与协调者 `POST /tasks` /
  `resume` 都在**同一个事务**里写业务行 + `corpus.tasks` 任务；执行由
  corpus-worker 领取。受理进程重启不会留下永远 queued/running 的任务
  （企业边界 7）。
- **回收清扫**（corpus-api lifespan 循环，像 outbox）：过期租约的执行落
  `failed + error=lease_expired`（generation 前移），卡死的协调任务落
  `failed + error=coordinator_stalled`（覆盖分母保留）。终态行绝不改写。
- `POST /tasks` 的契约响应从"总是 200 + 终态"变成 **202（新受理）/
  200（同键重放）**；`POST /resume` 保持 202 但语义变为"已受理、worker 会补做"。
  契约文件（`federation-tasks-v1.yaml`）已有这两个码，本轮补齐的是实现与描述。
- 取消从"`failed + error=cancelled`"改为 **`status=cancelled` 终态**；迟到的
  成功/失败写入被"状态守卫 + generation 围栏"双重拒绝。

## 2. 契约变化（先于实现）

`packages/contracts/enums.yaml`：

| 枚举 | 新增 | 说明 |
|---|---|---|
| `task_status` | `cancelled`（warn，「已取消」） | 终态；与 `failed` 分开是因为"用户不想要了"与"系统做砸了"对用户是两件事 |
| `task_kind` | `federation_execute` | 节点侧单步执行队列 |
| `task_kind` | `federation_plan` | 协调者计划推进队列（与执行分池，防父等子饿死） |

生成物：`python/ddp_contracts/ddp_contracts/enums.py`、
`services/control-api/internal/contracts/enums.go`、
`packages/contracts/generated/ts/enums.ts`（全部由
`packages/contracts/scripts/generate.py` 重生成，无手写第二份）。

**没有迁移**：`tasks.kind` 是 `String(24)`（`federation_execute` 18 字符），
`tasks.status` 是 `String(16)`（`cancelled` 9 字符），现有列宽足够；head 仍是
`0032`，未动。

## 3. 队列与取消语义（`ddp_corpus/queue.py`）

| 入口 | 语义 | 守卫 |
|---|---|---|
| `cancel(session, task_id)` | 幂等：queued/claimed/running → cancelled；succeeded/failed/cancelled 原样返回 False | `status.in_(活跃三态)` + **generation+1** + 清 `dedupe_key` |
| `cancel_by_dedupe(kind, dedupe_key)` | 联邦取消按业务键找活跃任务 | 同上 |
| `claim` | 候选**只含** queued 与"租约过期的 claimed/running" | cancelled 永远领不到 |
| `succeed` | 落成功 | `status.in_(("claimed","running"))` + generation 相等，双闸 |
| `fail` | 重试/落失败 | generation 相等 + 终态拒绝（`StaleGeneration`） |
| `heartbeat` | 续租 | generation 相等 + `status.in_(("claimed","running"))` —— 否则一次心跳能把 cancelled **复活**成 running |
| `is_terminal` | succeeded/failed/cancelled | 新值进终态集合 |

worker runner 不需要新代码：`run_one` 已经捕获 `StaleGeneration`，而取消会让
generation 前移，所以"跑完时任务已被取消"的路径天然不写成功（有测试钉着）。

## 4. 联邦执行的队列接线

### 4.1 节点侧（`federation.admit` / `federation.execute`）

- `_create_admission`：receipt 行、`federation_executions` 行、
  `federation_execute` 任务**同一个事务**提交。任务的
  `dedupe_key = federation-execution:<executor_task_id>` —— 幂等键就是执行 id。
- **任务 payload**：`{"executor_task_id": …, "actor": {organization_id,
  actor_id, kind, role, principal_id}}`。actor 是**最小持久绑定**（五项，不再多带
  请求现场字段），worker 用 `actor_from_binding` 重建并重新判权
  （`require_execution` 的组织检查 + 检索路径的资源授权）。
- **旧的内联路径**保留在 `FEDERATION_EXECUTION_INLINE=true` 之后（默认
  **false**）：只给没有 worker 的单进程部署与双节点验收夹具用，配置注释里
  写明了"它恢复不了的崩溃场景正是队列要解决的"。
- `federation.execute` 的领取条件增加 **"running 且租约过期"**：worker 崩溃后，
  队列任务被别的副本重新领取时能接上那条半途执行（generation+1，崩溃前
  worker 的迟到写入仍被围栏拦住）。
- `cancel_execution`：落 `state=cancelled`（generation+1），并
  `cancel_by_dedupe` 停掉队列任务；终态不因重复取消改变。
- `retry_execution`：只对 `failed` 且 error 落在
  `federation.RETRYABLE_EXECUTION_ERRORS`（`lease_expired` / `queue_task_failed` /
  `queue_task_cancelled` / `queue_task_missing`）生效，重置为 queued、
  generation+1、同事务补排队列任务 —— 这是 `resume` 补做的落点。
  （2026-09-14 第二轮复核前只有 `lease_expired` 一种。）

### 4.2 协调者（`federation_tasks`）

- `execute_task` 返回 `(status, created)`：**202 = 新受理**（行 status=running、
  `federation_plan` 任务同事务提交、`result=null`），**200 = 同键重放**。
- `resume`：状态改 running + 排 `federation_plan`（`retry_only=true`）回 202，
  worker 只补未完成目标；已经 `lease_expired` / `queue_task_failed` 的本地执行
  在这里重排。**对已取消的任务 409 `task_cancelled`、状态原样不动**；
  `execute_task`（POST /tasks）对已取消任务同样 409 —— 终态不许被新幂等键复活
  （2026-09-14 第二轮复核修复）。
- `run_queued`（worker 入口）：状态不是 running 直接返回；已知业务错误落
  `failed` 并返回，未知异常交给 runner 重试（那才是队列的用武之地）。
- 本地目标：`_local_execution_outcome` 看到 `queued` 时**由协调者 worker 自己
  调 `federation.execute` 跑完**（队列任务仍是崩溃恢复路径，被领取时看到终态
  空转、绝不重复执行）；看到 `claimed/running`（别的 worker 领走了）才轮询。
  这样单进程测试不需要第二条并发连接，也不会出现"父等子、子没人领"的死锁；
  协调与执行分成两个任务池（`federation_plan` / `federation_execute`）防止
  同一池里互相饿死。
- **终态围栏**：`_execute_plan` 的最终写入是条件 UPDATE
  （`WHERE status='running'`）；命中 0 行 = 执行期间被取消/被清扫，
  整笔（账本、交付、事件）rollback，返回真实终态。运行中途的取消因此不会
  再把 `cancelled` 翻回 succeeded。
- `cancel`：落 `status=cancelled` + `error=cancelled`，未完成目标记
  `not_attempted` 留在分母里，`cancel_by_dedupe` 停掉队列任务；幂等。

### 4.3 worker handler（`ddp_worker/handlers.py`）

- `federation_execute`：从 payload 重建 actor，`federation.execute` 真正执行。
- `federation_plan`：`run_queued`；按 `TASK_HEARTBEAT_SECONDS` 续协调行
  `updated_at`（清扫按它判卡死）。
- **执行行租约续期集中在 `federation.execute(heartbeat=True)`**：无论排队路径
  还是协调者就地执行，长检索都不会被清扫误杀；不续租的形态是"清扫把活着的
  执行标死、结果随后被 generation 围栏丢掉"，且所有层都不报错。
- 并发配置：`TASK_CONCURRENCY_FEDERATION_PLAN`（默认 2）、
  `TASK_CONCURRENCY_FEDERATION_EXECUTE`（默认 4）。

## 5. 回收清扫（`ddp_corpus/reconcile.py`）

`SWEEP_FEDERATION_LOOP`（lifespan 里的第三个循环，多副本走 Redis 锁）：

1. `federation_executions` 里 `queued/running` 且 `lease_until < now` 的行：
   `state=failed, error=lease_expired, generation+1, lease_until=NULL`。
2. **（2026-09-14 补）** `federation_executions` 里 `state=queued` 且
   `lease_until IS NULL`、但对应的 `federation_execute` 队列任务已经
   terminal-failed/cancelled（按任务 payload 的 `executor_task_id` 对账）的行：
   `state=failed, error=queue_task_failed|queue_task_cancelled, generation+1`；
   任务整个消失且过了 `TASK_LEASE_SECONDS` 宽限的落 `queue_task_missing`。
   没有这一支，重试耗尽死掉的执行任务会留下一行永远 queued 的执行。
3. `federation_requests` 里 `running` 且 `updated_at` 超过
   `FEDERATION_REQUEST_STUCK_SECONDS`（默认 900s）的行：
   `state=failed, error=coordinator_stalled`（覆盖账本原样保留，事件留痕），
   随后取消对应的 `federation_plan` 队列任务。
4. 候选查询与写入 UPDATE **都**带条件，写入 0 行 = 这期间行已落终态/被接管，
   绝不覆盖。协调任务的失败写入（`_mark_failed` / `mark_stalled`）同样改成了
   条件 UPDATE：先读后写会在读与写之间被一次并发取消插队，把 cancelled
   改回 failed（2026-09-14 第二轮复核修复）。

配置：`FEDERATION_SWEEP_INTERVAL`（默认 30s）；文档随
`scripts/gen_config_docs.py` 生成到 `services/corpus-api/CONFIG.md`。

## 6. 测试与验证

| 套件 | 结果 |
|---|---|
| corpus-api 全量 | **749 passed, 17 skipped**（第二轮复核后；PG 用例默认 17 skip，跑 `check_federation_pg.sh` 时全绿） |
| corpus-worker 全量 | **17 passed**（基线 10；新增 7 条取消语义） |
| 契约生成物 / 枚举用法 / 联邦契约 / 联邦路由 / 配置文档 | 全绿（`generate.py --check`、`check_enum_usage.py`、`check_federation_contracts.py`、`check_federation_routes.py`、`gen_config_docs.py --check`） |
| ruff（F,B） | corpus-api + corpus-worker 全绿 |

新增用例：

- `tests/test_federation_queue.py`（15 条）：受理与队列任务同事务、换会话仍能看到
  任务、worker 重建 actor 并重新判权（组织不符拒绝执行）、actor 绑定往返与
  篡改拒绝、同键重放不排第二条/不执行第二次、取消停队列任务且不留结果、
  `POST /tasks` 202 → 队列推进 → 重放 200、未领取时取消 → `cancelled` 且分母
  保留、执行中途取消不被迟到成功覆盖、过期租约被 worker 接管、清扫标
  `lease_expired` 且不碰 succeeded、清扫标 `coordinator_stalled` 且不碰
  succeeded、`resume` 重做 `lease_expired`（不新造受理）、入队失败整体回滚
  （原子性）、内联逃生口行为、契约文案。
- `tests/test_task_cancel.py`（7 条）：queued 取消幂等、cancelled 不被领取
  （lease 过期也不领）、取消 fencing 心跳与终态写入、**拿取消后的新代次也写
  不进去**（状态守卫）、succeeded 取消 no-op、按 dedupe 取消、runner 容忍
  被取消的租约。
- 第二轮独立复核新增（`tests/test_federation_queue.py`，5 条）：
  `test_resume_of_cancelled_task_is_409_and_performs_nothing`（含"取消在提交
  之前"的变体）、`test_mark_failed_cannot_overwrite_a_concurrent_cancel`、
  `test_mark_stalled_cannot_overwrite_a_concurrent_cancel`、
  `test_dead_queue_task_fails_execution_and_resume_reruns_it`、
  `test_execution_read_and_cancel_are_actor_scoped`。

## 7. 变异确认

`/tmp/opencode/mutations.py` + `mutations2.py` + `mutations3.py`（临时脚本，
每次先确认变异真的改动了文件，跑完还原并核对哈希）：

| # | 变异 | 期望红的用例 | 结果 |
|---|---|---|---|
| M1 | `succeed` 去掉状态守卫 | `test_terminal_writes_cannot_overwrite_cancelled_with_current_generation` | RED |
| M2 | `cancel` 不再 generation+1 | `test_cancel_claimed_lease_fences_heartbeat_and_terminal_writes` | RED |
| M3 | `claim` 把 cancelled 当候选 | `test_cancelled_task_is_never_claimed` | RED |
| M4 | `execute` 不接管过期租约 | `test_worker_reclaims_a_running_execution_with_expired_lease` | RED |
| M5 | 清扫终态守卫整体拆除（候选+写入；单拆一道不红，因为两道闸各自独立承重） | `test_sweeper_fails_expired_lease_and_never_touches_terminal_rows` | RED |
| M6 | `mark_stalled` 终态守卫整体拆除（候选+函数内，跨两个文件） | `test_sweeper_marks_stuck_coordinator_request_and_keeps_denominator` | RED |
| M7 | `_execute_plan` 终态写入去掉条件围栏 | `test_execute_plan_never_overwrites_a_cancelled_row` | RED |
| M8 | `resume` 不再重排 `lease_expired` | `test_resume_redoes_lease_expired_execution_without_second_admission` | RED |
| M9 | 入队失败被吞掉 | `test_admission_is_atomic_with_the_queue_task` | RED |

M5/M6 的单道闸拆掉后测试仍是绿的（候选过滤和写入守卫是纵深防御），因此用
**复合变异**证明写入守卫在承重 —— 这也是记录变异时必须写清的一点：单独测
一道闸证明不了两道闸都有意义。

第二轮独立复核的 12 个变异（`/tmp/opencode/mutations_p5p7.py`，逐个先确认
文件真的被改、跑完还原并核对 sha256）：

| # | 变异 | 期望红的用例 | 结果 |
|---|---|---|---|
| N1 | `resume` 去掉 cancelled 守卫 | `test_resume_of_cancelled_task_is_409_and_performs_nothing` | RED |
| N2 | `execute_task` 去掉 cancelled 守卫 | 同上 | RED |
| N3 | `_mark_failed` 的条件 UPDATE 改成无条件写 | `test_mark_failed_cannot_overwrite_a_concurrent_cancel` | RED |
| N4 | `mark_stalled` 的条件 UPDATE 改成无条件写 | `test_mark_stalled_cannot_overwrite_a_concurrent_cancel` | RED |
| N5 | 清扫不认 terminal-failed 的队列任务 | `test_dead_queue_task_fails_execution_and_resume_reruns_it` | RED |
| N6 | `retry_execution` 不认 `queue_task_failed` | 同上 | RED |
| N7 | ack 不读回执 state | `test_ack_expired_response_never_marks_local_confirmed` | RED |
| N8 | `require_execution` 去掉 actor 绑定 | `test_execution_read_and_cancel_are_actor_scoped` | RED |
| N9 | 日志脱敏去掉下标键识别 | `check_log_redaction.py --self-test` | RED |
| N10 | 更新副本摘要校验拆除 | `test_apply_rejects_archive_swapped_after_verification` | RED |
| N11 | 额外文件检查拆除 | `test_apply_rejects_unmanifested_files_in_archive` | RED |
| N12 | 模型缓存检查搬回 swap 之后 | `test_apply_refuses_model_cache_change_before_swap` | RED |

## 8. 行为变化清单（对外契约）

| 位置 | 旧行为 | 新行为 |
|---|---|---|
| `POST /api/v1/tasks` | 200 + 终态（请求内执行） | **202 + running**（新受理，排队）；同键重放 200 + 权威状态 |
| `POST /api/v1/tasks/{id}/resume` | 202 + 终态（请求内补做） | 202 + running（排队补做） |
| `POST /api/v1/tasks/{id}/cancel` | `status=failed, error=cancelled` | `status=cancelled`（终态） |
| `POST /api/v1/federation/admissions` | 201 后请求内执行 | 201 只落受理；执行由 worker 领取 |
| `POST /api/v1/federation/tasks/{id}/cancel` | `state=failed, error=cancelled` | `state=cancelled` |
| `POST /api/v1/tasks/{id}/resume` 的 202 含义 | "已重新执行完" | "已受理恢复，worker 会补做" |

`federation-tasks-v1.yaml` 的相应 description 与
`docs/refactor/P5-INTERFACES-v3.md` §0/§2/§3 已同步；`P5-VALIDATION-v3.md` §5
的缺口条目已标注关闭。

## 9. 已知边界（不当成已验证）

- **协调者本地目标由协调者 worker 就地执行**（不是排给另一个 worker 再等）：
  队列任务仍然存在，作用是崩溃恢复与 peer 受理路径；但"本地目标的执行完全
  由 `federation_execute` handler 驱动"这句话只在 peer 受理路径成立。
- `federation_plan` / `federation_execute` 的租约续期依赖
  `TASK_HEARTBEAT_SECONDS`（默认 30s）；比 `TASK_LEASE_SECONDS`（300s）短一个
  数量级，但**没有在真实多副本部署下压过**。
- 回收清扫只在单测的 SQLite 上验证；真 PG 的并发行为（两个副本同时清扫、
  与 worker 领取赛跑）留给部署阶段。
- 内联逃生口（`FEDERATION_EXECUTION_INLINE=true`）不满足企业边界 7 的恢复
  承诺，仅用于无 worker 的夹具/单进程开发。
- 远端节点（双节点验收里的 B）仍然用内联模式，因为夹具没有 worker 进程；
  生产默认队列模式。

## 10. 第二轮独立复核修复（2026-09-14）

范围：对 P5/P6/P7 第二批改动做独立复核后发现的四个问题（F1/F2/F3/F5）。
本节是**修正记录**，上文 §4/§5/§6/§7 的对应句子已就地更正；仍未 commit。

- **F1（阻塞）resume 复活已取消任务。** 旧 `resume` 只查
  `planning_state=approved` + 计划/许可，把 cancelled 行改回 running；取消腾出
  的 dedupe 键让新的 `federation_plan` 真的跑完并写 succeeded。现在
  `resume` 与 `execute_task` 都对 cancelled 回 **409 `task_cancelled`**且不改
  任何状态；`task_cancelled` 已进 `enums.yaml` 的 `federation_error`
  （三语言生成物同步）与 `federation-tasks-v1.yaml` 的 `FederatedError` 枚举 /
  两个端点的 409 描述。回归用例覆盖完整
  create→plan→approve→submit→cancel→resume 序列，并断言 drain 之后零受理、
  零执行；另覆盖"取消发生在提交之前"。
- **F2（重要）协调终态是先读后写。** `_mark_failed` / `mark_stalled` 旧实现
  `SELECT` 到 running 后无条件 `UPDATE`，取消在读写之间提交时会被改写成
  failed。现在两者都是条件 UPDATE（`WHERE root_task_id=? AND status='running'`），
  命中 0 行 = 别人拥有结局，不写状态也不追加事件。对抗性用例用 `_RacingSession`
  在第一次终态原语前提交真实取消，断言最终是 cancelled。
- **F3（重要）队列任务死掉后执行永远 queued。** 执行行 `lease_until IS NULL`，
  过期租约清扫扫不到；`retry_execution` 只认 lease_expired。现在清扫新增
  `_sweep_dead_queue_executions`：按任务 payload 里的 `executor_task_id`
  对账，terminal-failed/cancelled 的任务把执行落
  `failed/queue_task_failed|queue_task_cancelled`（任务消失过宽限期落
  `queue_task_missing`），generation 前移且绝不覆盖终态；
  `RETRYABLE_EXECUTION_ERRORS` 相应扩到这几种，resume 重排一次新代次。
  SQLite 端到端用例强制 handler 失败 + `max_attempts=1`，断言执行落 failed 后
  resume 只重跑一次且不新造受理；真 PG 另有
  `test_sweeper_on_pg_marks_queued_execution_whose_queue_task_died`。
- **F5（次要）执行读取消只按组织过滤。** `require_execution` 旧查询只比
  `organization_id`，同组织另一个调用者能读（含证据/答案）并取消别人的执行。
  现在 join 受理行按 `admission.actor_id == acting_actor(actor)` 过滤
  （组织管理员与 tasks/deliveries 同口径仍可复核），越权与不存在同形 404；
  协调者用原始 actor 绑定轮询不受影响。回归用例断言 owner 200、同组织 bob 404、
  bob 取消 404 且执行仍 queued、owner 取消 200。

**这次复核也修正了两处文档漂移**：§5 的清扫分支数（2 → 3）与 §4.1 的
`retry_execution` 受理范围。
