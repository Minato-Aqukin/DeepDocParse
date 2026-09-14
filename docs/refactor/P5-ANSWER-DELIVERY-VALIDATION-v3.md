# P5 缺口收口：远端答案委托与交付字节（验证记录 v3）

> 2026-09-13。范围：填平 `P5-VALIDATION-v3.md` §5 的前两项 ——
> **远端答案委托**（跨节点证据数据边 + `operation=answer` 接单与执行）与
> **交付字节**（有界结果文档的下载端点 + 本地摘要校验 + 过期映射）。
> 本文件是这两个切片的验证记录；P5 其余部分与三轮复查结论见
> `P5-VALIDATION-v3.md`。工作树未 commit，按仓库流程这不构成正式验收。

## 1. 结论

- **答案委托已落地**：`rag.answer.cited` 本地未就绪时，协调者按探索许可与
  根预算探测候选执行节点；就绪节点会在计划里得到 `answer-1` 步骤与
  `evidence_excerpts` 数据边（先有计划、后有批准）。执行者只在生成真的就绪
  且证据逐条可复核时接单，用**与协调者本地生成同一份实现**产出带引用答案。
- **交付字节已落地**：结果文档（规范 JSON，无正文/源文件字节）持久在
  `federation_deliveries.result_json`（0030），`GET /api/v1/deliveries/{id}`
  有界返回；`ddp_local` 下载后重算 `content_digest(canonical result)` 与
  `result_manifest_digest` 对账，通过才允许 ack；TTL 到期 410 `delivery_expired`
  且本地状态必为 expired。
- **真双节点 HTTP 验收**：A 无生成能力、B 有真实 loopback 模型端点；A 经生产
  `PeerClient` 在真实 socket 上探测、受理、轮询，B 的模型真的看到了跨节点送去
  的正文。harness 没有被削弱（见 §3）。
- **全量门禁**：`./scripts/check.sh` **29/29 PASS**（2026-09-13，
  `/tmp/opencode/check-final2.log`；对照基线 27/27 —— 多出的两项是并行工作流
  新增的门禁）。

## 2. 交付物

| 层 | 位置 | 内容 |
|---|---|---|
| 共享生成 | `ddp_corpus/federation.py`（`ANSWER_SYSTEM_PROMPT` / `answer_skeleton` / `unavailable_answer` / `excerpt_reason` / `grounded_answer`） | 提示词 + 结构验收的**唯一实现**，协调者本地与远端执行者共用；`federation_tasks` 只保留薄包装 |
| 执行者 | `federation.py`（`_verify_evidence`、`_run_answer`、`capability_input.operation`、`execution_status.answer`）、`capabilities.py`（`answer_generation_ready`、`ADMISSIBLE_OPERATION_PROFILES`） | 证据摘要重算/边界、answer 受理与执行、按 operation 的就绪度与接单声明 |
| 协调者 | `federation_tasks.py`（`_probe_answer_candidates`、`_append_delegated_answer_step`、`_delegated_answer`、`_validated_delegated_answer`、`read_delivery`、`_bounded_delivery_document`） | 能力探测、委托计划与数据边、绑定子集校验、交付文档持久化与 TTL 读取 |
| 契约 | `packages/contracts/openapi/federation-tasks-v1.yaml` | `ProbeRequest.operation`、`AdmissionRequest.evidence`/`AdmissionEvidence`、`ExecutionStatus.answer`、`GET /api/v1/deliveries/{delivery_id}` + `DeliveryRead` |
| 本地运行时 | `python/ddp_local/ddp_local/{federation_client,federation_dispatch}.py` | `CenterFederationClient.delivery()`；`fetch_delivery` 下载→本地校验→持久投影→才可 ack |
| 迁移 | `database/corpus/alembic/versions/0030_federation_delivery_result.py` | `federation_deliveries.result_json`（可交付文档，可空；超界不存、不截断） |
| 测试 | `tests/test_federation_answer_delegation.py`（新）、`test_federation_two_node.py`、`federation_two_node.py`、`test_federation_probes.py`、`test_federation_tasks.py`、`test_federation_admissions.py`、`python/ddp_local/tests/test_federation_{client,dispatch}.py` | 见 §3 |

## 3. 验证证据

套件计数（`check.sh` 全绿同一轮）：

| 套件 | 结果 |
|---|---|
| ddp_core | 160 passed |
| ddp_local | **107 passed**（基线 103） |
| corpus-api | **687 passed, 3 skipped**（基线 633；本切片新增 21 条，其余为并行工作流新增） |
| model-gateway / corpus-worker / mcp / eval | 177+6skip / 10 / 52 / 45 |
| 守卫 | 联邦契约 7 schemas·71 fixtures / 联邦路由 20 端点一致 / 其余全绿 |

### 3.1 远端答案委托

- **真双节点**（`test_remote_answer_delegation_over_real_http`）：B 是独立
  uvicorn 子进程 + SQLite 文件库；`ModelStub` 是测试进程里真实的 loopback
  HTTP 服务（`GET /v1/capabilities` 观测到 instruct 通道、`POST /v1/chat/completions`
  返回带引用答案）。A 规划时探测 B 的 `rag.answer.cited` 就绪度，把
  `answer-1` 与 `edge-answer-1` 落进计划；批准后 A 把融合证据摘录经 admission
  发给 B，轮询取回答案；断言绑定指向 B 的真实 `evidence_id`、`disclosure.remote
  = true`、B 的模型 prompt 里真的有跨节点正文；B 端 ground truth：2 探测、
  2 受理（取数+答案）、2 次证据集读，无重试风暴。
- **执行者证据校验**（`test_federation_answer_delegation.py` 执行者段）：
  摘要重算、id 唯一、空白/超 2000 字符/超 50 条一律 `input_not_verified`
  （不截断）；缺证据是 `waiting_input` 且不产生执行行；预算 0 是
  `budget_exceeded`；就绪度复核不通过是 `capability_unsupported`；生成无引用
  时执行完成但 `answer=null` + `unsupported_generation` + `validation_state=failed`。
- **对抗对端**：伪造/越界 evidence id → `delegated_binding_out_of_scope`，
  答案作废、证据保留；空绑定 → `delegated_bindings_missing`；答案执行 500 →
  显式原因 + `validation_state=failed`、证据保留。
- **许可门**：执行许可不含 `edge-answer-1` → 审批 403 `egress_denied`，
  对端零 admission（一个字节都没发）。
- **能力诚实**：`rag.answer.cited` 未就绪的节点不会得到 answer 步；能力探测
  仍然发出（诚实问过），没有 ready 节点时保持 `answer=null` +
  `local_model_missing`。执行端点侧：请求 operation 缺省 `corpus.retrieve`；
  问 `rag.answer.cited` 时观测不到模型通道一律 unknown/`can_generate=false`，
  观测到 instruct 通道才 ready/true（`test_federation_probes.py`）。
- **本地运行时**：执行许可里的 `edge-answer-1` 与接收方原样送到中心 approve，
  客户端不重写许可（`test_dispatch_carries_answer_delegation_edge_in_consent`）。

### 3.2 交付字节

- **正向闭环**（中心 + 本地）：`pending → GET /deliveries/{id} → 本地
  content_digest 校验 → 持久 result → ack`；读取响应 `Cache-Control: no-store`，
  文档不含 `excerpt`/`_excerpt`，`plans.digest(result)` 等于
  `result_manifest_digest`。
- **篡改**：中心返回的文档与声明摘要不符 → 本地 `verified=false` +
  `result_manifest_mismatch`，不落结果、不 ack，显式原因可见。
- **过期**：中心 410 → 本地 `delivery.state=expired` + 原因，`confirm_delivery`
  拒绝；中心侧 TTL 读取先落 expired 事件与行，再回 410 `delivery_expired`；
  已 confirmed 的不再受 TTL 影响。
- **不可见**：其它 actor / 未知 id 一律 404 `delivery_not_found`。
- **超界**：结果文档超过上限不持久化、不截断，读取 `result=null`，ack 因
  "没有可校验字节"被 409 拒绝。

### 3.3 变异确认（全部实测：破坏 → 红 → 还原）

`/tmp/opencode/mutate.py` + `mutate2.py` + `mutate3.py` 逐条改动被守卫的代码行
并跑定向用例，16/16 确认：

| # | 变异 | 变红的用例 |
|---|---|---|
| 1 | `_verify_evidence` 不重算摘要 | 执行者摘要不符拒绝 |
| 2 | 缺证据不再 waiting_input | 缺证据等待用例 |
| 3 | 委托绑定不再做子集校验 | 伪造 evidence id 用例 |
| 4 | 审批不再校验许可覆盖数据边 | 许可缺 answer 边用例 |
| 5 | `can_generate` 不再要求 readiness ready | 能力探测诚实性用例 |
| 6 | 协调者对任意探测都选生成节点 | 未就绪节点不得有 answer 步 |
| 7 | 去掉 2000 字符越界拒绝 | 超界不截断用例 |
| 8 | 客户端不再校验交付摘要 | 篡改交付用例 |
| 9 | 读取端点不再按 TTL 置 expired | TTL 410 用例 |
| 10 | 受理不再复核生成就绪度 | 就绪度复核用例 |
| 11 | 去掉证据条数上限 | 超条数拒绝用例 |
| 12 | 不拒绝重复 evidence_id | 重复 id 拒绝用例 |
| 13 | 客户端不把 410 映射成 expired | 过期映射用例 |
| 14 | 无存量字节也允许 ack | 超界不可确认用例 |
| 15 | 交付读取不做 actor 可见性 | 他人不可见用例（bob 读到 200） |
| 16 | 交付读取去掉 `Cache-Control: no-store` | 响应头断言 |

（还原后 `grep` 复核无残留；脚本对每次替换都断言唯一命中。）

## 4. 刻意记录在案的偏差 / 契约说明

- **`result_manifest_digest` 的预像变了**：由原来的
  `{root_task_id, scope_ref, evidence, counts}` 改为**整份交付文档**
  （`result` 里的 `result_manifest_digest` 字段本身除外）。理由：交付校验必须
  是 `content_digest(canonical result) == result_manifest_digest`，客户端拿到的
  就是这份文档；旧预像让下载的字节与摘要覆盖的内容不是同一件东西。旧值不在
  任何冻结 schema 里，但这是可观察行为的变化，故记录。
- **`ExecutionStatus` 新增可选 `answer` 字段**（原任务列出的 YAML 改动未包含
  它）：远端 answer 执行的结果必须经既有轮询读回，新增端点不如扩一个可选字段
  保守。取数执行该字段恒为 null。
- **边界错误用显式机器码而不是 422**：`AdmissionEvidence` 在 Pydantic 层只做
  防滥用粗界（4096 字符/64 条），契约的 2000/50 由 `_verify_evidence` 把关并
  返回 `input_not_verified`，保证"越界 → 显式机器错误"是可测的同一条路径。
- **契约探针的候选范围**：本切片没有目录展开，候选生成节点 = 本轮取数目标里
  的远端节点（稳定顺序、最多 8 个、先过探索许可门与根预算）。"A 有证据、B 只
  生成不取数"的场景需要 P6 的目录能力；当前测试用 B 同时是取数目标。
- **文件归属偏差**：为落地功能，除了任务列出的文件，还改了
  `ddp_corpus/federation_models.py`（delivery 加 `result_json` 列）与
  `tests/test_capabilities.py`（`accepting_admissions` 现在还必须对
  `rag.answer.cited` 说真话，旧断言"只有 corpus.retrieve 可为 true"与新行为
  矛盾）。两处都是必要且最小的改动。

## 5. 仍未验证 / 明确局限

- **真实 GPU 生成**：双节点用例里的模型是 loopback 上的固定应答桩；真实 vLLM/
  DeepSeek-OCR 的生成质量、token 计数口径与真实延迟不在此记录。
- **真实 socket 之外的网络形态**：全部双节点调用是 loopback TCP；WAN、TLS、
  代理、P4 密钥交换（委托凭证）未验证，同伴凭据仍是每节点单值（与
  `P5-VALIDATION-v3.md` §5 一致）。
- **0030 只验了 SQLite batch 迁移与 `alembic heads=0030`**：真 PostgreSQL 上的
  upgrade/downgrade 往返没在本轮跑（0029 的那条 opt-in 真库用例未覆盖 0030）。
- **委托答案的 resume 语义**：`_delegated_answer` 先 lookup 再受理，幂等复用
  已有回执；但没有专门的 resume-委托用例，也没有"委托步在 resume 时证据变化"
  的对抗用例。
- **交付字节的真实时间流逝**：TTL 用行时间戳直接改到过去验证，不等真实 TTL。
- 对端自报的 `readiness/can_generate` 仍不是密码学证明（P4 未完成）；
  执行者接单前会复核自己就绪度，但协调者无法证明对端真的就绪，只能验证
  它回传的引用是否落在所发证据内。

## 6. 复跑

```bash
cd DeepDocParse && ./scripts/check.sh
cd services/corpus-api && ../../.venv/bin/python -m pytest -q
cd services/corpus-api && ../../.venv/bin/python -m pytest -q tests/test_federation_answer_delegation.py
cd services/corpus-api && ../../.venv/bin/python -m pytest -q tests/test_federation_two_node.py
cd python/ddp_local && ../../.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_federation_routes.py
.venv/bin/python scripts/check_federation_contracts.py
```
