# v3 实施台账

执行依据：工作区 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md`（2026-09-12）。
本轮用户明确要求按该计划完成改进。与旧 `plan.md` 冲突的桌面、逻辑资源、权限及联邦方向按 v3 实施；旧计划的工程不变量和提交验收要求继续适用。

源码基线：`c09783d3555fae2ce86f229f18f37418b036f276`。开始时分支为 `deploy/autodl-cloudflare-tunnel`，带已有未提交 v3 schema/枚举/0015 资源层；继续到 `codex/desktop-federation-v3`，未覆盖已有工作。

| 阶段 | 状态 | 本轮工作与剩余出口 |
|---|---|---|
| P0 基线与契约 | 进行中 | 接续既有六份schema及夹具；补齐真实源码基线，独立验收待做 |
| P1 资源/权限/出处/Wiki | 进行中 | 资源ACL、文件凭证按主体与资源绑定、安全Bundle、引用GC、固定Wiki修订并行；真实全链路验收待做 |
| P2 共享内核/本地 | 进行中 | SQLite/受控文件/CPU解析/检索/本地生成/模型管理 |
| P3 Electron/双执行 | 进行中 | 真实Electron/SQLite/CPU链、共享工作区/模型/Wiki界面、中心读取与回执已接；两层许可正在实现，批准后的传输/执行/交付未完成 |
| P4 可枚举发现 | 进行中 | 稳定节点身份/私钥证明/调用者隔离的成员快照、显式发布集合 producer、持久 ScopeManifest、精确定位 `resources/locate` 已实现并通过独立复验（F1/F2/F3 已修，见 `P4-INDEPENDENT-REVIEW-v3.md` 修复复验章节）；**远端目录展开仍缺**（control 不调用远端目录，见 P5 验证记录）|
| P5 联邦业务 | **本机可完成部分已完成**，见 `P5-VALIDATION` / `P5-ANSWER-DELIVERY` / `P5-QUEUE` / `P5-PG` 四份记录 | 内核、执行者、协调者、本地客户端、双节点 HTTP、远端答案委托、交付字节、持久队列与 `cancelled` 终态、恢复扫描器、真 PG 并发。剩：真实 GPU/多主机验证 |
| P6 递归联邦 | **本机可完成部分已完成**，见 `P6-DIRECTORY-EXPANSION` / `P6-CACHE` / `P6-CACHE-WIRING` / `P6-ROUTING-EVAL` | 远端目录递归展开（预算/环路/去重/child_manifests）、有界缓存与 Wiki 依赖失效、摘要排序接入、路由评测骨架。剩：真实多主机实验、真实语料质量数字 |
| P7 发行与验收 | **本机可完成部分已完成**，见 `RELEASE-MANUAL` / `CAPACITY-LOCAL` / `RECOVERY-DRILL` / `COMPATIBILITY-MATRIX` | 可复现 Arch 构建、更新/回滚签名校验、日志脱敏机械守卫、SSRF/注入/降敏测试、备份+节点身份演练、本地 CPU 压测数字。剩：GPU 容量、生产快照迁移、签名基础设施/AUR/其他平台、用户拍板容量与 RTO/RPO |

## 验证记录

- Go 文件授权回查：主体头防伪、撤销即时生效、错误/重定向默认拒绝、资源身份一致性；聚焦测试通过。
- Corpus 文件能力：两个合法同内容资产需显式上下文，删除r1不能借r2续用r1凭证；无身份拒绝。2条通过。
- 临时 PostgreSQL 容器 `ddp-v3-review-pg`，仅回环端口15439；不连接用户开发/生产数据库。control 0001—0004与真实凭证并发/隔离测试通过。
- 后续全量检查、独立验收、真实用户路径结果在通过后补录；不以计划声明或单测冒充真实模型验收。

## 参考冻结

t3code参考HEAD：`b1e223e2b0d87124883b1410ab52dd6a1338e40d`（2026-09-12读取）。仅借鉴连接与执行边界，未复制代码或资产。
参考：[t3code](https://github.com/pingdotgg/t3code/tree/b1e223e2b0d87124883b1410ab52dd6a1338e40d)、[Electron安全](https://www.electronjs.org/docs/latest/tutorial/security)、[凭证后端](https://www.electronjs.org/docs/latest/api/safe-storage)。

## 迁移兼容约束

旧稳定文件token不含主体，无法证明授权对象，v3兑换时默认拒绝；凭证行保留。已授权任务/用户按稳定的 `(组织,主体,资源,文档)` 重新取token。证据与文档身份继续采用固定ID，不使用下载URL替代。上线前需暂停旧处理任务、重新授权未完成任务；不能把旧永久token继续公开使用当作兼容。

六个逻辑节点可以用本机独立进程与独立存储验证协议及CPU行为；是否有足够容量必须实测，不能直接把“没有GPU”写成目录/故障实验不可能。真实GPU及真实生成效果另计，不租用付费实例前做完可本地验证的代码与清单。

## 2026-09-12 集成进展

- 独立 P1 提前审阅发现公开读者写入、来源复制绕过、私人反链、共享 pending 解析、历史回填、元数据、Wiki 固定版本、草稿标题与 GC 续扫缺陷；已逐项修正或继续集成，不能把聚焦测试绿色视为全 P1 通过。
- root 元数据/固定版本/迁移/文档回归：47 passed，日志 `/tmp/ddp-context-final.log`。
- Bundle 内核 27、HTTP/GC/存储 30、真实 PG 竞态 3 项通过，见 `BUNDLE-GC-VALIDATION-v3.md`。
- Opus 完成五个 MCP 工具从直接全库访问迁到语料 API，正在补部署接线和固定版本二次检查。
- DeepSeek CLI 三次因连接重置退出，未留下可用 client-runtime 实现；不能记为已完成。
- 本地 runtime 开始提取共享应用端口与真实 borndigital 解析；尚未完成模型与桌面验收。

## 2026-09-13 集成进展

- 真实CPU模型在无外网命名空间完成回答与Wiki试验，实际OOM/恢复与六轮失败记录保留。最终本地Wiki两页六句、两条模型选择的原文关系、CAS人工编辑和重启恢复通过；关系仅source_mention_order profile且全部待复核。见 `P3-LOCAL-MODEL-VALIDATION-v3.md`、`P2-LOCAL-WIKI-VALIDATION-v3.md`。
- Electron主进程持有共享HttpProvider/ConnectionRegistry/SQLite与凭证，真实Wayland/PDF.js、包外CPU链自验通过；独立review发现两项并发缺陷，修复与二审通过。最新Vue/凭证变更尚未重打目录包。
- 中心client协议实现固定版本窗口、调用者隔离游标、真实receipt和查询。真实Go→Python鉴权、能力producer诚实unknown、真实PG0025迁移与并发通过；不宣称远端写已完成。
- 当前共享客户端/桌面69测试、Vue30单元测试、工作台9浏览器测试通过。最新整仓check、真实MCP全链路、完整双执行/联邦业务与精确commit diff独立验收仍未完成；没有commit/push。
- Wiki快速刷新草稿竞态由整套回归抓出，新增共享立即发送的DraftWriter和3条确定性回归，修后整套浏览器89 passed（`/tmp/ddp-v3-browser-all10.log`）。Claude新审查调用触及会话限额；不将其记为通过，其他独立验收与OpenCode审阅仍继续。

## 2026-09-13 P5 实施（本轮）

- P4 独立复查 F1/F2/F3 已修并复验；P5 代码平面落地：共享内核（probe/coverage/routing/admission）、节点执行者（真实证据 Probe、持久 Admission、generation fence 执行、证据集、精确定位、引用解析）、入口协调者（intent/plan/approve/task/coverage/events/resume/cancel/delivery ack、探索许可门、快速/穷查、证据融合、本地带出处答案）、本地运行时中心客户端（`authorize_dispatch` 外发、丢回执不重放、对账与交付确认）。
- 契约：`openapi/federation-tasks-v1.yaml` 19 端点 + `scripts/check_federation_routes.py` 双向守卫；接口冻结见 `P5-INTERFACES-v3.md`；完整记录见 `P5-VALIDATION-v3.md`。
- 验证：全量门禁 **27/27 PASS**；corpus-api **633 passed, 3 skipped**；ddp_core 146、ddp_local 103；双节点真实 HTTP 11 条；三轮独立对抗性复查分别抓到 4 BLOCKING + 5 MAJOR/MINOR、1 BLOCKING + 6 MAJOR/MINOR、1 MAJOR，全部修复并逐条复验关闭（F1–F9、N1–N8）。
- **尚未完成**：远端答案委托（跨节点证据数据边）、远端目录展开、交付字节下载、worker 队列与 `task_status=cancelled`、真实 CPU/GPU 生成、P6/P7。因此 **M3 不宣布通过**。
- 本轮没有 commit/push；工作树中仍混有此前未提交的 P0–P4 改动，提交前须按流程做一次针对精确 diff 的正式独立验收。

## 2026-09-13 第二轮：P5/P6/P7 本机可完成部分

- **P5 收尾**：远端答案委托（`AdmissionRequest.evidence` + 节点 `answer` 执行 + 协调者能力探测/数据边/绑定子集校验）；交付字节（`GET /api/v1/deliveries/{id}` + 本地校验 digest 后才 ack）；持久队列化（`federation_execute`/`federation_plan` 任务、`task_status=cancelled` 终态、按 root 条件 UPDATE 防终态覆盖、死队列任务对账、过期租约恢复扫描器、`resume` 对 cancelled 409）；探针/执行 actor 绑定与 `source_revoked` 生产者。
- **P6**：远端目录展开（`GET /api/v1/federation/members|collections` peer 认证 + Go 递归 BFS/预算/环路/去重/child_manifests + PG 持久化）；有界缓存（条目/字节/TTL/负数缓存/探针复用）与 Wiki 依赖失效；摘要描述符排序接入；`eval/routing` 确定性评测（fast 63.3% / exhaustive 100% 相对召回，诚实性不变式全过）。
- **P7**：可复现 Arch 包（两次构建 hash 一致）、真实 ed25519 更新签名与回滚、日志脱敏 AST 守卫（新门禁）、SSRF/提示注入/跨组织降敏测试、兼容矩阵与旧版本夹具、真实 PG+MinIO 备份恢复与节点身份演练、本机 CPU 压测报告（明确非容量承诺）。
- **验证**：`./scripts/check.sh` **29/29**；corpus-api 750 passed/17 skipped（含真 PG 12+5、双节点 12、答案 15、缓存 24+、队列 21）；corpus-worker 17；ddp_local 108；ddp_core 160；eval 66。
- **独立复查（第二轮，同一 reviewer）**：抓到 1 BLOCKING（cancelled 被 resume 复活）、2 MAJOR（终态读改写竞态、死队列任务令执行卡死）、4 MINOR（ack 假确认、执行 actor 越权、脱敏守卫绕过、更新 TOCTOU）；全部修复并复验，F3 残留的「重试后被下一轮 sweep 再判死」反例也已修复（活跃任务优先）并变异确认。最终判定 **PASS**。
- **仍只能在有 GPU/生产环境的机器上做**：mineru/VQA/TEI/真实生成本身与质量数字、多主机 TLS/凭证交换、生产快照迁移与灰度、AUR/签名基础设施、其他平台；容量目标与 RTO/RPO 仍需用户拍板。
- 本轮同样没有 commit/push。

## 2026-09-15 证据冲突轴与 T01–T88 台账

- **冲突轴**（计划 §7.6，路由评测发现 1）：契约先行，`ddp-scope-coverage` 的
  `CoverageLedger` 新增可选 `conflicts`（`EvidenceConflict`：依据 / ≥2 条证据引用 /
  人工复核态）与双向 allOf，`enums.yaml` 新增 `evidence_conflict_basis`。内核
  `coverage.version_conflicts`（规则）与 `agent.conflicts_from_text`（生成标注，
  引用不成立整份拒收）；协调者、远端委托校验、覆盖读取与取消都带上矛盾记录。
- **验收台账** `ACCEPTANCE-MATRIX-v3.md`：88 条逐项登记状态、证据与缺口
  （✅ 38 / 🟡 46 / 🔴 4），新门禁 `scripts/check_acceptance_matrix.py` 校对
  条目完整、汇总计数、每个 ✅ 行的**证据栏**都有可校验的测试引用、引用的文件与用例
  存在且指得准（17 种台账漂移形状逐一变异确认会红，含两轮验收指出的缺口栏冒充证据、
  短标题子串、不带反引号的路径、ASCII 省略号、skip/todo 标题）。
- **提交前第五次验收抓到的两件事**：① 冲突一度优先于"没有绑定"，版本分歧会把
  `insufficient` 改写成 `conflicting` 并绕过协调者的生成闸（在真实协调者流程里复现）——
  已改成 unknown > insufficient > conflicting > sufficient_by_policy，契约 allOf 同步放宽为
  "有矛盾记录就不许报充分"，规则一路只收自报来源 = 返回目标节点的条目，resume 复原
  上一轮的规则矛盾；② 台账初稿 50 条 ✅ 里抽查的 22 条有 12 条只覆盖了一半判据，已降级。
- **第六次验收**：同一处的两个版本**分两轮到达**（第一轮 v1，resume 补做的目标带回 v2）
  时复原的条目不进规则一路，账本照报 `sufficient_by_policy` —— 已改为结果里记内部字段
  `_attributed_evidence`（可归属证据键，不进交付文档、状态出口剥掉 `_` 字段），resume
  时复原条目据此参与比较；没有该字段的旧结果仍原样复原已记下的矛盾。T13 补了"已知/未知
  摘要响应同形"断言。
- **第七次验收 PASS 后记下的后续项（本次不修，既有问题）**：协调者融合证据时按
  `(origin, resource, version, evidence_id)` **直接覆盖**，坏对端可以冒用别家的键把已归属
  条目的信封顶掉（交付结果里别家的证据也能被替换）；对冲突轴的影响只会多出 `needs_review`
  的矛盾。应改成"非归属条目不许覆盖已归属的键"，单独做、单独验收。
