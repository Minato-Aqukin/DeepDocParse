# P5 联邦业务实施与验证记录（v3）

> 2026-09-13。范围：计划 v3 §11 的 **P5**（探测前许可、Probe、类型化执行图、
> 持久接单与幂等对账、快速/范围穷查、覆盖账本、跨中心证据融合、本地带出处答案、
> 交付回执）与 P4 三个遗留缺陷（F1/F2/F3）。
> **这不是 M3 的完整通过结论**：见 §5「未实现/未验证」。本轮工作树未 commit，
> 因此按仓库流程这不构成一次正式验收；本文件记录的是三轮独立对抗性复查的结果。

## 1. 结论

- P5 代码平面已落地：共享内核、节点/执行者端点、入口协调者端点、本地运行时
  中心客户端、契约与路由守卫、corpus 迁移 0027–0029。
- 三轮独立对抗性复查（同一 reviewer，原文见 `plan` 的验收要求）分别抓到
  **4 个 BLOCKING + 1 MAJOR**（首轮）、**1 个 BLOCKING + 3 MAJOR**（次轮）、
  **1 个 MAJOR**（三轮）；每一个都带独立反例，修复后逐条复验关闭。
- 快速模式永不 `complete`、`internal_limits` 不得洗成成功、覆盖分母保留、
  探索许可前零外发、接单对账不重复执行、答案引用必须落到真实证据 —— 这些
  核心承诺现在都有正负两个方向的确定性与真实 HTTP 用例。
- **尚不能声称 M3 完成**：远端答案委托（跨节点证据数据边）、远端目录展开、
  交付字节下载、真实 CPU/GPU 生成、P6/P7 均未实现。

## 2. 交付物（按层）

| 层 | 位置 | 内容 |
|---|---|---|
| 共享内核 | `python/ddp_core/ddp_core/application/{probe,coverage,routing,admission}.py` | 零 I/O 的 Probe 校验/构造、覆盖账本合取、候选排序与根预算、接单幂等决策 |
| 节点执行者 | `services/corpus-api/ddp_corpus/federation.py`、`routers/federation.py`、`federation_models.py` | 真实证据 Probe、持久 Admission（幂等/对账/绑定校验）、执行（generation fence）、证据集、精确定位、引用解析 |
| 协调者 | `services/corpus-api/ddp_corpus/{federation_tasks.py,federation_peers.py,routers/tasks.py}` | intent/plan/approve/task/coverage/events/resume/cancel/delivery ack、探索许可门、快速/穷查、证据融合、本地带出处答案 |
| 本地运行时 | `python/ddp_local/ddp_local/{federation_client.py,federation_dispatch.py}` + `plan_http.py`/`cli.py` | 批准后按 payload 走 `authorize_dispatch` 外发、丢回执不自动重放、对账与交付确认 |
| 契约 | `packages/contracts/openapi/federation-tasks-v1.yaml`、`scripts/check_federation_routes.py` | 19 个端点的机器可读契约与双向路由守卫 |
| 迁移 | `database/corpus/alembic/versions/0027…0029` | 探测/受理/执行/协调/覆盖/交付/事件表、intent 幂等键、依据列宽 |
| 控制面 | `services/control-api/internal/api/server.go` | 协调者前缀经入口转发（保持身份头剥离）；`accepting_admissions` 如实透传 |

## 3. 验证证据

全量门禁 `./scripts/check.sh`：**27/27 PASS**（2026-09-13，`/tmp/opencode/check-final2.log`）。

| 套件 | 结果 |
|---|---|
| ddp_core（含 61 条内核新用例） | 146 passed |
| ddp_local（含 28 条中心客户端/派发用例） | 103 passed |
| corpus-api（含执行者/协调者/双节点/答案） | **633 passed, 3 skipped**（3 条 skip 是需真 PG DSN 的 opt-in） |
| model-gateway / corpus-worker / mcp / eval | 177+6skip / 10 / 52 / 45 |
| 架构守卫 / 联邦契约 / 联邦路由 | 18 / 7 schemas·68 fixtures / 19 端点一致 |
| Go / 前端 | go vet·test·gofmt·tidy 全绿；33 前端单测 + 类型检查 + 连接层协议 |

关键验收用例：

- **双节点真实 HTTP**（`tests/test_federation_two_node.py`，11 条）：节点 B 是独立
  uvicorn 子进程 + 独立 SQLite 文件库；A 经生产 `PeerClient` 真实 socket 调用
  B 的 probe/admission/evidence-set。覆盖 B-only 证据、A/B 分裂证据（同内容不
  制造双份共识）、注册但不可达的 C 留在分母、`local_only` 零外发、
  submit 重放、跨任务幂等键冲突 409、错 peer token Fail Closed、fast 不得
  complete、穷查 sealed 才 complete、取消/恢复只补未完成目标。
- **对抗性复查反例**：覆盖洗白（T85）、resume 重复执行、intent 无幂等
  （假守卫）、远端正文进不了生成 prompt、能力接单声明虚高、`exclusion_basis`
  溢出、终态被迟到超时覆盖、lookup 回执不绑定、并发写 500、全 partial 被判 failed、
  空白/超长 excerpt、probe 碰撞穿过 `create_plan` 等。
- **本地运行时**：无批准不外发、丢回执不自动重放、凭据不入错误、按 phase
  预算停发、交付摘要不符不确认；每条都做过变异确认。
- **答案**：带真实引用的输出 → bindings 指向融合证据；伪造/缺引用、上游失败、
  超预算、缺正文均以显式原因拒绝生成；两节点用例证明 B 的原文确实进入 prompt。

## 4. 三轮独立对抗性复查（同一 reviewer，逐条复验）

| 轮次 | 发现 | 状态 |
|---|---|---|
| 一 | F1 覆盖洗白（BLOCKING）、F2 resume 重复执行（BLOCKING）、F3 intent 无幂等（BLOCKING）、F4 远端正文缺失仍生成（BLOCKING）、F5 能力声明虚高（MAJOR）、F6–F9（MINOR） | 全部修复；二次复验确认 F1–F4 原反例不再复现 |
| 二 | N3 本地 lookup 404 崩溃（BLOCKING）、N2 lookup 回执不绑定（MAJOR）、N4 并发写 IntegrityError 逃逸（MAJOR）、N1 全 partial 有证据被标 failed（MAJOR）、N5–N7（MINOR） | 全部修复；三次复验确认 N1–N3、N5–N7 关闭 |
| 三 | N8 本地 probe 碰撞穿过 `create_plan` → `MissingGreenlet`/500（MAJOR） | 已修（SAVEPOINT 先建后插 + 协调者按主键重读）；定向复验 PASS，含 commit 层碰撞变体与变异确认 |

复查同时确认：守卫不是假守卫（`check_federation_routes.py` 删除真实路由会红、
import 失败不会静默放行）；迁移 0027–0029 在真 PG 上 upgrade/downgrade/upgrade
通过，ORM 与列无漂移；`accepting_admissions` 只有 `corpus.retrieve` 为真。

## 5. 明确未实现 / 未验证

- **远端答案委托**：`operation=answer` 的跨节点接单与证据数据边未实现；需要
  证据字节的受限传输与执行批准流程。当前协调者本地生成，远端 `can_generate`
  的 answer 步被显式丢弃。
- **远端目录展开**：P4 的 control 目录不调用远端节点的集合目录，ScopeManifest
  的 remote 成员只能来自显式传入的冻结清单。
- **交付字节**：只有 `pending → ack/expired` 的清单摘要确认；没有结果下载端点，
  因此"本地已校验持久提交"只覆盖摘要层面。
- **worker 队列 / `task_status=cancelled`**：~~执行是请求内 + 持久行 + `resume`
  对账；取消仍以 `failed + error=cancelled` 表达，未改 worker 状态机
  （与 `enums.yaml` 的说明一致）。~~ **已由 P5 队列切片关闭**（2026-09）：
  `federation.admit` 与 `POST /tasks` / `resume` 改成"同事务持久化 + 排
  `corpus.tasks` 任务、worker 领取执行"，`POST /tasks` 新受理回 **202**（重放
  仍是 200）；`task_status.cancelled` 连同 `queue.cancel`、generation fencing、
  过期租约回收清扫一起落地。行为差异、测试数与变异确认见
  `P5-QUEUE-VALIDATION-v3.md`。
- **真实模型/GPU**：本机无 N 卡；答案生成只在协议层用 stub 上游验证，未跑真实
  vLLM；检索在 SQLite MemoryIndex/关键词路验证，pgvector、TEI、真实 CPU 模型
  质量数字不在此记录。
- **P6/P7**：递归目录、根预算层级、路径环路、有界缓存、安装包、生产迁移未做。
- 对端为敌手的度量：协调者对 probe/证据信封做结构与绑定校验，但对端自报的
  `readiness` 不作密码学证明（P4 密钥交换未完成）；本记录不把它当作已验证。

## 6. 复跑

```bash
cd DeepDocParse && ./scripts/check.sh
cd services/corpus-api && ../../.venv/bin/python -m pytest -q
cd services/corpus-api && ../../.venv/bin/python -m pytest -q tests/test_federation_two_node.py
cd python/ddp_core && ../../.venv/bin/python -m pytest -q
cd python/ddp_local && ../../.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_federation_routes.py
```

真 PG 的 opt-in 与双节点子进程用例按 `tests/` 内的显式环境变量说明启用。
