# P5 联邦业务接口冻结（v3）

> 2026-09-23 更新。本文件是 P5（Probe / TaskPlan / Admission / 覆盖账本 / 快速与穷查 /
> 交付）并行实现的唯一接口依据。执行权威仍是工作区
> [plan.md](../../../plan.md) §6–§9（原 v3，编号不变）；本文件只把其中的
> 对象、签名与端点冻结成各方可同时编码的形状。冲突时以计划原文为准，并回来改这里。

## 0. 范围与非目标

本切片实现计划 §11 的 **P5**：探测前探索许可、真实能力/资源/证据 Probe、类型化
任务图、持久 Admission 与幂等对账、快速/范围穷查、覆盖账本、跨中心证据融合与
整项答案与 Wiki 委托、交付回执，以及这些入口共用的持久根预算。

**相邻边界**：递归目录与缓存见 P6；发行、设备兼容与生产恢复见 P7 的实际验证记录，
不能由本文件的接口存在性推导真实 GPU、发行或生产验收通过。

- 正式受理与 `federation_execute` / `federation_plan` 持久任务在同一事务内写入，
  由 worker 领取执行。`FEDERATION_EXECUTION_INLINE=true` 仅用于明确选定的单进程验证；
  队列语义见 `P5-QUEUE-VALIDATION-v3.md`。
- 取消是持久终态；worker 的 generation fencing 与条件写入拒绝迟到结果。
- Peer 只采用已批准节点的单次受众限定凭证，旧共享口令配置被拒绝；见 §5。

## 1. 共享内核（`python/ddp_core/ddp_core/application/`）

新模块必须：零 I/O、零 DB、import 无副作用、只依赖 `ports.ApplicationError`
与 Python 标准库。digest 一律沿用 `plans.canonical_bytes/content_digest`。

### 1.1 `probe.py`

```python
PROBE_KINDS = ("capability_input", "resource_locate", "evidence_retrieval")

def validate_probe(probe: dict) -> None          # 契约 ddp-task-probe/1#ProbeResult
def probe_digest(probe: dict) -> str             # canonical digest，不含 observed_at
def build_probe(*, probe_id, target_node_id, task_spec_digest, consent_ref,
                probe_kind, capability_check, retrieval=None, can_generate=False,
                missing_requirements=(), offer=None, observed_at) -> dict
def reusable(probe: dict, *, now, query_digest=None, index_revision=None,
             ttl_seconds=300) -> bool
```

语义：
- `validate_probe` 必须实现 schema 里的全部 allOf：`evidence_retrieval` 必须有
  `retrieval`；`retrieval.internal_limits` 非空 ⟹ `status="partial"`；`offer.reservation`
  必须 `false`；`can_generate` 是布尔，不是 `can_solve`。
- `reusable` 只在 `observed_at` 未过期、`query_digest`/`index_revision` 与参数相等、
  且 probe 携带满足条件的 `retrieval.index_revision` 时为真。过期或缺失一律
  重新探测，不把旧证据洗成新证据。

### 1.2 `coverage.py`

```python
def target_key(origin_node_id, collection_id, operation) -> dict
def new_entry(target_key, scope_ref, query_digest) -> dict        # state="planned"
def record(entry: dict, probe: dict | None, *, state=None, error=None,
           now) -> dict                                               # 返回新 entry
def ledger(*, root_task_id, scope_ref, search_mode, enumeration_state,
           entries) -> dict                                           # #CoverageLedger
def completeness(enumeration_state: str, search_mode: str, entries) -> str
def sufficiency(entries, *, bindings=(), conflicting=False) -> str
```

语义（计划 §7.4 的合取，逐条落成代码）：
- `completeness == "complete"` 当且仅当 ① `enumeration_state == "sealed"`；
  ② 无 `unexpanded_subtrees`（由调用方保证 enumeration 已 sealed）；③ 所有 entry
  为 `succeeded`；④ 无 `in_flight/not_attempted/unreachable/denied/revoked/partial/
  failed`。**fast 模式永远返回 `partial`**，即使全部成功。
- `record(entry, probe)`：`succeeded` ⟹ 至少一个 probe receipt 且
  `actual_index_revision` 非空；`unsupported` ⟹ 必须有 `exclusion_basis`（由调用方
  传入或从 probe 的 `missing_requirements` 派生）；probe 报 `internal_limits` ⟹
  `partial`；无 probe 而记 `unreachable/failed/denied` 时保留 `last_error`。
- `sufficiency`：无 bindings ⟹ `insufficient`（`unknown` 只在"还没评估"时用）；
  `conflicting` ⟹ `conflicting`；否则 `sufficient_by_policy`。**不得**用 LLM 自报
  信心。
- `ledger` 的 `counts`：`total_targets`（去重后全部）、`applicable_targets`
  （排除 `unsupported`）、`succeeded`、`excluded`（unsupported）、`incomplete`。
  契约的 allOf 必须被满足（fast 不得 complete；complete 必须 sealed + incomplete=0）。

### 1.3 `routing.py`

```python
def targets(manifest: dict) -> list[dict]                 # 去重 TargetKey，稳定排序
def candidates(targets, descriptors, *, query, limit, ordering="local_first",
               local_node_id=None) -> list[dict]          # [{"target_key", "score", "reason"}]
class RootBudget:
    def __init__(self, budget: dict, *, now)
    def reserve(self, kind: str, amount: int = 1) -> None  # 超限 raise budget_exhausted
    def used(self) -> dict
def plan_steps(*, targets, probes, local_node_id, coordinator_node_id,
               query, now) -> tuple[list[dict], list[dict]]   # (steps, data_edges)
```

语义：
- `targets` 用 `(origin_node_id, collection_id, operation)` 去重；重复路径不重复计数，
  但**不得**因摘要低分删除成员（穷查）。`enumeration_state != sealed` 的 manifest
  返回的列表必须让调用方能标注 partial，函数本身不删。
- `candidates` 只影响**顺序**与 fast 模式的取数上限；打分可用 descriptor 的
  languages/topics/time_range 与 `ordering`，**不读内容、不调用模型**。`local_first`
  时本地目标优先但不独占。相同输入必须给出确定顺序。
- `plan_steps` 生成契约合法的最小步骤图：每个取数目标一个 `retrieve` step
  （`executor_node_id` 为 origin），跨节点证据汇聚在协调者上做 `fuse`/`answer`；
  data_edges 的 `authorised_by` 由调用方填（函数接收 `authorised_by` 或从 target
  推导，同一 node 不产生数据边）；`max_hops` 与步数/边数必须自洽。生成结果要能
  通过 `plans.validate_plan`。
- `RootBudget` 是唯一账本：发现、probe、检索请求、字节、生成 token、hop 共用一份
  额度（计划 §7.5）。`reserve` 在超限时抛 `ApplicationError("budget_exhausted", ...)`，
  并记录已消耗，不允许每个子任务重新获得完整额度。

### 1.4 `admission.py`

```python
def request_digest(body: dict) -> str
def validate_receipt(receipt: dict) -> None        # #AdmissionReceipt 全部 allOf
def reuse(existing: dict | None, *, idempotency_key, request_digest) -> str
    # 返回 "reuse" | "create"；同键不同摘要 raise ApplicationError("idempotency_conflict")
def receipt(*, admission_id, issuer_node_id, executor_node_id, root_task_id, step_id,
            delegation_generation, idempotency_key, request_digest, plan_digest,
            state, input_validation, receipt_revision, effective_policy_ref,
            executor_task_id=None, verified_input_manifest_digest=None,
            accepted_at=None, quota_decision_ref=None) -> dict
```

语义：`accepted` 必须同时有 `executor_task_id`、`verified_input_manifest_digest`、
`input_validation="content_verified"`、`accepted_at`；`waiting_input` 的
`verified_input_manifest_digest` 必须为 null。`unknown` 是"回执丢失、待对账"，
**不是失败**；调用方不得因 unknown 自动换节点重做有副作用的步骤。

## 2. 节点/执行者端点（corpus-api，每个中心都暴露）

前缀见计划 §9.5。认证：服务凭据 `Authorization: Bearer SERVICE_TOKEN`，调用者
上下文沿用 `deps.actor` 头（`X-DDP-Organization` / `X-DDP-Actor` / `X-DDP-Role` …）。
所有写操作必须带 `Idempotency-Key`。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/v1/federation/probes` | `ProbeRequest`（见下） | 201 `ddp-probe/1#ProbeResult` |
| GET | `/api/v1/federation/probes/{probe_id}` | — | `ProbeResult`；过期 410 `probe_expired` |
| POST | `/api/v1/federation/admissions` | `AdmissionRequest` | 201/200 `AdmissionReceipt` |
| POST | `/api/v1/federation/admissions/lookup` | `{idempotency_key}` | `AdmissionReceipt`；无 404 |
| GET | `/api/v1/federation/tasks/{executor_task_id}` | — | `ExecutionStatus` |
| POST | `/api/v1/federation/tasks/{executor_task_id}/cancel` | `{}` | `ExecutionStatus`（幂等） |
| POST | `/api/v1/federation/resources/locate` | `{resource_id, version_id?}` | `LocateResult` |
| POST | `/api/v1/federation/results/resolve` | `{evidence_ref}` | `ddp-evidence/1#FederatedEvidence` |
| GET | `/api/v1/federation/evidence-sets/{set_ref}` | — | 授权后的证据清单（`federation-probe:<id>` / `federation-execution:<id>`），逐条重判权 |

`ProbeRequest`：
```json
{"schema":"ddp-task-probe/1#ProbeRequest","task_spec_digest":"sha256:…",
 "consent_ref":"…","probe_kind":"evidence_retrieval","target_node_id":"node-…",
 "scope_ref":"scope-17","collection_id":"col-1","query":"…",
 "query_digest":"sha256:…","candidate_limit":8}
```
- `target_node_id` 必须等于本节点身份（否则 409 `wrong_target`）。
- `evidence_retrieval` 只对**已发布**集合执行，检索按集合固定成员
  的 parse revision 限定；返回真实片段（含 locator）与 `index_revision`。
- 任何分片/限额导致的内部不完整必须进 `retrieval.internal_limits` 并把
  status 置 `partial`（T85）。
- `capability_input` 只返回 readiness/input_validation，不做检索；
  `resource_locate` 校验指定版本可读性。

`AdmissionRequest`：
```json
{"schema":"ddp-plan-admission/1#AdmissionRequest","idempotency_key":"…",
 "root_task_id":"…","step_id":"retrieve-1","delegation_generation":0,
 "task_spec":{…},"plan":{…},"execution_consent":{…},
 "inputs":[{"ref":"query","digest":"sha256:…","size_bytes":123}]}
```
校验顺序（任一失败给机器错误码，不静默）：
1. 重算 `plan_digest`/`task_spec_digest` 与请求一致，否则 `plan_changed`。
2. `plans.validate_plan(plan, task_spec, local_node_id=executor, now)`；`planning_state`
   必须 `approved`，`execution_consent.plan_digest == plan.plan_digest`，
   `task_spec.consent_refs.execution == execution_consent.consent_id`。
3. step 的 `executor_node_id` 是本节点；数据边若要求输入，digest 必须与 `inputs`
   逐一验证（`content_verified`）；验证不了则 `input_not_verified` 且
   state=`waiting_input`（不占 GPU、不执行）。
   `retrieve` 步骤还要过来源集合的转交策略（T83）：集合带 `onward_recipients` 时，
   按整份计划算本节点外发边（含 `relay_via`）下游能到的节点 ——
   `plans.onward_recipients`，与桌面 `validate_scope` 同一个下游遍历，只是不沿
   `query_text` 边走（问题原文不携带来源内容）；除根协调者外有一个不在名单里就
   403 `egress_denied`，不写 receipt、不排队。没有集合目标的检索按本组织全部
   带策略的已发布集合核对。
4. 幂等域是 `(organization_id, authenticated issuer_node_id, key)`：
   同域同键同 `request_digest` 返回已有 receipt（200，不复算执行）；
   同域同键不同摘要 409 `idempotency_conflict`。lookup 使用相同的签发节点边界，
   业务键仍为 `{root}:{step}[:证据摘要]`，不加入用户身份或 delegation_generation。
5. 通过后**同一个事务**持久写 receipt（`accepted`）、execution 行与一条
   `federation_execute` 持久队列任务；worker 领取后执行 `retrieve` 或
   `answer` 步骤，执行结果与 receipt 分离。受理响应不等执行（进程重启后
   任务仍在队列里）。

`ExecutionStatus`：
```json
{"executor_task_id":"…","admission_id":"…","root_task_id":"…","step_id":"…",
 "operation":"retrieve","state":"queued|claimed|running|succeeded|failed|cancelled",
 "generation":1,"lease_until":"…|null","result_ref":"…|null",
 "evidence_set_ref":"…|null","error":null,"updated_at":"…"}
```

## 3. 协调者端点（入口中心 corpus-api）

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/api/v1/task-intents` | 持久任务需求 + 已批准的探索许可；返回 root_task_id |
| POST | `/api/v1/task-plans` | 按 scope 清单与探索许可做 Probe、生成 TaskPlan |
| GET | `/api/v1/task-plans/{root_task_id}` | 只读取得本人最新计划修订；不触发 Probe、重新规划或外发 |
| POST | `/api/v1/task-plans/{root_task_id}/approve` | 批准计划修订 + 执行许可 |
| GET | `/api/v1/tasks` | 调用者**本人**的任务列表（创建时间倒序、键集游标，坏游标 400 `invalid_cursor`）；只带需求摘要与状态轴，结果按 id 读。管理员也只列自己的 |
| POST | `/api/v1/tasks` | 受理已批准计划（幂等键）并排入持久队列：202 新受理 / 200 重放，执行由 worker 推进；已取消任务 409 `task_cancelled` |
| GET | `/api/v1/tasks/{root_task_id}` | 权威状态、结果、覆盖引用、消耗 |
| GET | `/api/v1/tasks/{root_task_id}/coverage` | `ddp-scope-coverage/1#CoverageLedger` |
| GET | `/api/v1/tasks/{root_task_id}/events` | 带序号的可恢复事件 |
| POST | `/api/v1/tasks/{root_task_id}/resume` | 重判权、对账未知受理、补做未完成目标；fast 还可暂存下一批并要求重新批准；已取消任务 409 `task_cancelled` |
| POST | `/api/v1/tasks/{root_task_id}/cancel` | 显式、幂等取消；`cancelled` 是终态，迟到写入一律被状态守卫拒绝 |

- **协调者幂等域**：intent 和 submit 的键分别存储，均按组织 + 实际用户主体
  隔离；API key 取其所属用户。不同用户或组织可使用同键创建、提交独立任务；
  同主体同键重放仍返回原 intent/task，同主体改实体或改用另一 root 则 409。
- **探索许可门**：没有有效 `ExplorationConsent`（或 `egress_mode=local_only`）时，
  `/task-plans` 不得向任何远端发出 Probe，返回 `egress_denied`；问题/实体/资源名
  一个字都不出网。`task-intents` 的请求体携带用户已批准的探索许可，协调者只做
  校验与持久化，**不代用户签署**。
- **执行许可门**：`/tasks` 必须校验 `ExecutionConsent.plan_digest` 与提交的 plan
  修订一致、接收方集合覆盖所有数据边（含 relay），否则 `egress_denied`。
- **来源转交策略**（T83）：规划时从选中目标的集合描述读 `onward_recipients`；
  委托生成会把融合证据 A→候选节点外发，任一来源不允许该候选就跳过它（不发能力
  探测，`answer_probes` 记 `source_policy_denied`）。因此没有生成步骤时结果的
  `answer_reason` 是 `source_policy_denied` 而不是 `local_model_missing`。描述没取到
  时不知道策略，由来源执行者受理时按整份计划复核兜底。
- **fast**：按 `routing.candidates` 取有界候选，统一融合，结果
  `retrieval_completeness="partial"`，响应显式列出未检索目标。显式续查从持久候选图
  选择尚未批准的下一目标，即使上一轮有命中也不把它当作问题已完整回答的证明。
  新修订有新的 digest 与步骤 ID，清除旧执行许可，先返回 `planning_state=ready`；
  只读对账或加载计划均不批准或执行它。批准后再次显式 resume 才可执行。
  旧交付保持不可变，新修订使用新的交付 ID；固定文档任务不会自动扩展资料范围。
- **exhaustive_scope**：以 `ScopeManifest.expanded_members` 为分母，逐目标 Probe；
  失败的记 `unreachable/failed` 并保留在分母；全部成功且 enumeration sealed 才
  允许 `complete`。
- **answer 步骤**：规划时按能力清单生产者
  （`capabilities.collect_capability_profiles`）判断 `rag.answer.cited` 的实际就绪状态。
  可以使用协调者本地模型，或选择已探测、可接单的生成节点；全部 evidence 数据边及
  answer 返回边必须纳入执行许可。执行时把实际提供的融合证据 ID 与正文交给 OpenAI
  兼容上游（`upstream.chat_request`），使用共享 `ddp_core.answer` grounded-claims
  schema/decoder 与 `temperature=0`。仅在整份 JSON 完整通过后采用主张：缺失或越界
  ID、空 claims/引用、截断或非法矛盾组均整份拒收（`answer=null`、
  `validation_state=failed`、`unsupported_generation`），**不修补引用**。
  `insufficient_evidence` 是正常显式拒答，不产生答案或绑定。联邦显式启用的矛盾组
  每个 ID 还必须被至少一条主张引用。成功时由解码主张投影既有 `answer` 文本及
  `claim_evidence_bindings`（每条
  `structural_validation=passed`、`semantic_review=needs_review`）、`provider`、
  `disclosure`、内核判定的 `evidence_sufficiency` 与 `validation_state`。
  未就绪或生成失败时只写明确的 `answer_reason`（`local_model_missing`、
  `upstream_error`、`no_model_output`、`budget_exceeded` 等）并保留证据，
  **不把检索任务标失败**。远端执行者的证据回传仍走 `routing.plan_steps` 生成的
  `evidence_excerpts` 数据边，由协调者消费。
- **远端生成**：执行者正式接单后才生成；委托返回的证据绑定必须是实际传入证据的子集。
  没有模型、授权数据边不完整、输入摘要不匹配或结构校验失败均明确拒绝，不能用空答案
  冒充成功，也不能用模型输出新建原始证据。生成步骤（answer / wiki_pages）的业务幂等键是
  `{root}:{step}:{证据集摘要前 24 位}`（按 `(evidence_id, digest)` 排序后的规范 JSON 取
  sha256）：同一证据集的重放与丢响应对账复用原执行；resume 补回更多证据后是一次新的
  生成，绝不采用没见过这些证据的旧答案（执行者的请求摘要不覆盖 `evidence`）。该步骤的
  生成 token 预留仍按 (root, step) 只扣一次。升级前用旧键 `{root}:{step}` 受理的生成步骤，
  升级后 resume 查不到旧回执，会再受理一次并重新生成（多一条执行与 `federated_execution`
  计量），方向是重做而不是沿用可能过期的答案。
- **Wiki**：使用同一固定原始证据集合规划与生成版本化 Wiki，可委托 `wiki_pages` 给
  仅有生成能力的节点。计划同时记录 evidence 外发与 wiki_draft 返回边；中心保留不可变
  修订、人工段落、关系和依赖。`semantic_review=needs_review` 不表示已完成人工支持度评审。
  桌面用固定的 `wiki.list/get/revisions` 只读查询打开交付中的 Wiki 修订；外源引用保持
  原始节点身份，不改写成入口节点的裸证据 ID，也不从模型文字构造外部请求。
- **交付**：结果默认 `retention=temporary`，`delivery_state` 为 `not_requested`
  （留在中心）或 `pending`（等待客户端 `POST /api/v1/deliveries/{id}/ack`）。
  TTL 到期未确认 → `expired`，不得显示"已保存本地"。

### 根预算与续查持久性（0037）

`federation_root_ledgers` 每个 root 一行，固定调用者预算与服务端上限的交集、deadline，
并保存 requests / bytes / generation_tokens / hops / discovery / probes / egress_bytes。
账本分两类：hops 与 generation_tokens 是绑定获准计划步骤的额度预占，在步骤首次
尝试出站时扣账；0039 的 `federation_root_reservations` 以 `(root_task_id,
reservation_key)` 唯一键绑定 `(逻辑步骤, kind)`，同一步骤重试、resume 或崩溃重放不再
扣这两项。否则资料/生成节点一次短暂拒连，就会耗尽获准步骤额度，使恢复后答案不可达。
逻辑步骤不随 fast 续查的计划修订改名：retrieve 按目标（`retrieve:{执行节点}:{集合}`），
其余步骤按去掉 `r{n}-` 前缀的步骤 id；否则续查的生成步骤会再预占一次整份生成上限，
续查取回的证据永远进不了答案（F14）。升级前按旧键预占的进行中任务，续查时会按新键
再预占一次。
requests / bytes / egress_bytes / probes / discovery 是物理消耗，每次实际尝试仍先扣账，
失败不退款；发现分页、健康探测、Probe、执行请求、轮询与证据读取均计入，缓存命中本身
不伪装成网络调用。重新规划、业务回滚或重启不重置根账本。

预占记录的 insert-if-absent 与带上限的根账本 UPDATE 在同一个独立事务提交；仅新记录
增加计数，超限则两者一起回滚。唯一键使并发协调者只预占一次；读取到已预占的键不重复
更新账本或内存计数（恢复时已从账本重放用量）。BigInteger 处理大字节量。
生成 token 是获准输出上限的预占，不宣称是模型实际 usage；bytes 是受限应用层载荷，
不宣称涵盖 TCP/TLS 开销。读取状态只合并持久消耗，不能借读取动作补发写请求。
根账本与预占表故意不对正在被业务事务锁住的 request 行设置外键，避免独立扣账等待父行锁。
历史账本不退款；0039 不臆造历史步骤预占，升级前已消费额度仍保留。

### 联邦计量（T81 / T58）

根账本是预算闸门，计量是业务结果的账单记录，两者分开。执行者每条执行在赢得终态围栏
（`generation` 匹配且尚未终止的那一次 UPDATE）时记一条 `federated_execution`；协调者每个
root 在结果交付后记一条 `federated_delivery`。两种 kind 都是只报告（`requests=1`、
`pages=0`），不进页数配额。用量事件写入 `corpus_outbox`，与终态写入同一事务；`event_id`
由业务键 `federation-execution:{executor_task_id}` / `federation-delivery:{root_task_id}`
确定性派生，outbox 插入是 insert-if-absent，control 的 `usage_ledger.event_id` 唯一约束
再去重一次。因此受理重放、响应丢失后的 lookup 对账、resume、代次轮换与崩溃重放都不重复
计量；被围栏挡下的迟到写入、取消后的成功、`waiting_input`、幂等冲突与失败的 root 不计量。
部署顺序：先升级 control（认识新 kind），再升级 corpus；顺序反了 control 会对未知 kind
回 200 并记 ERROR 日志，那笔用量不入账。

## 4. 持久化（corpus alembic 0027）

- `federation_probes`：probe_id、organization_id、actor_id、target_node_id、
  task_spec_digest、consent_ref、probe_kind、collection_id、query_digest、
  state（coverage_target_state）、result_json、expires_at、created_at。
- `federation_admissions`：admission_id、organization_id、actor_id、
  idempotency_key、request_digest、plan_digest、root_task_id、step_id、
  delegation_generation、issuer_node_id、executor_node_id、state（admission_state）、
  input_validation、executor_task_id、verified_input_manifest_digest、
  effective_policy_ref、receipt_json、receipt_revision、created_at、updated_at；
  unique `(organization_id, issuer_node_id, idempotency_key)`（迁移 0040）。
- `federation_executions`：executor_task_id、admission_id、root_task_id、step_id、
  operation、state（task_status）、generation、lease_until、result_ref、
  evidence_set_ref、result_json、error、created_at、updated_at。
- `federation_requests`（协调者）：root_task_id、organization_id、actor_id、
  task_spec_digest、scope_id、scope_digest、search_mode、planning_state、
  plan_revision、plan_digest、execution_consent_ref、status（task_status）、
  retrieval_completeness、evidence_sufficiency、result_json、coverage_ref、
  delivery_id、delivery_state、error、created_at、updated_at；
  submit 和 intent 分别 unique `(organization_id, actor_id, idempotency_key)` /
  `(organization_id, actor_id, intent_idempotency_key)`（迁移 0040）。0040 只收窄唯一键，
  请求摘要不含 issuer／actor，升级前的受理、意图与提交行在同一 issuer／用户下仍可原样重放；
  回退前若扩大后的键域已被占用（同组织同键多行）则拒绝降级。
- `coverage_ledgers`：root_task_id（pk）、scope_ref、search_mode、enumeration_state、
  retrieval_completeness、evidence_sufficiency、counts_json、manifest_digest、
  created_at、updated_at。
- `coverage_entries`：root_task_id + target_digest（复合 pk）、target_key_json、
  query_digest、state、probe_refs_json、actual_index_revision、search_profile、
  attempts、last_error、evidence_refs_json、used_budget_json、exclusion_basis。
- `federation_deliveries`：delivery_id、root_task_id、state（delivery_state）、
  result_manifest_digest、retention、expires_at、verified_at、receipt_json、
  created_at、updated_at。

## 5. Peer 目录与凭据（Fail Closed）

`settings.federation_peers` 只保存 `{node_id: {"endpoint": "https://…"}}`，
不保存对端 service_token 或 peer_token。地址登记只决定“往哪发”；控制面的已批准
成员与公钥记录决定能否签发/接受凭证，见
`packages/contracts/ddp/node-credential-format.md`。

- 每次物理请求由本节点控制面签发 Ed25519 凭证，绑定 audience、最终 actor、操作、
  范围和有效期；入站验签、校验本机受众与操作，并用 jti 防重放。
- 有效期 `FEDERATION_CREDENTIAL_TTL_SECONDS` 为 1..120 秒；已批准公钥缓存
  `FEDERATION_PEER_KEY_CACHE_SECONDS` 为 0..60 秒，也界定撤销的新请求生效延迟。
  未知、pending、revoked 不缓存为可用成员。
- 旧 `FEDERATION_PEER_AUTH` / `FEDERATION_PEER_TOKEN` 或 peer 目录里的口令字段
  均为配置错误，没有回退到共享密钥的认证档位。
- endpoint 必须是 HTTPS、无 userinfo/query/fragment；HTTP 只允许显式的
  `federation_allow_loopback` 测试开关配合字面 `127.0.0.1/[::1]`。
- 未登记节点不发请求；远端 actor 映射为受限本地 peer 主体，再由来源节点 ACL 判权。
- 凭证不回显、不入日志；出站 HTTP 必须 `trust_env=False`、`follow_redirects=False`。

## 6. 文件归属（并行避免冲突）

| 工作流 | 独占写入 |
|---|---|
| A1 内核 | `python/ddp_core/ddp_core/application/{probe,coverage,routing,admission}.py`、`python/ddp_core/tests/test_{probe,coverage,routing,admission}.py` |
| A2 P4 修复 | `services/control-api/internal/api/scope_handlers.go`、`services/control-api/internal/store/{discovery,scope}.go`、`services/corpus-api/ddp_corpus/catalog.py`、对应测试 |
| A3 契约 | `packages/contracts/openapi/federation-tasks-v1.yaml`、`scripts/check_federation_routes.py`、`scripts/check.sh`、`.github/workflows/guards.yml`、`packages/contracts/ddp/federation-format.md` 状态更新 |
| A4 执行者 | `services/corpus-api/ddp_corpus/{federation_models,federation}.py`、`routers/federation.py`、`main.py`、`config.py`、`capabilities.py`、`database/corpus/alembic/versions/0027_federation_execution.py`、`services/corpus-api/tests/test_federation_*` |
| B1 协调者 | `services/corpus-api/ddp_corpus/federation_tasks.py`、`routers/tasks.py`、`main.py`（仅追加 mount）、`services/corpus-api/tests/test_federation_tasks*` |
| B2 本地执行 | `python/ddp_local/ddp_local/{federation_client.py,plan_http.py,runtime.py,http.py,cli.py}`、`python/ddp_local/tests/test_federation_client.py` |

## 7. 验证要求

- 每个新模块/端点都要有**负向**用例：无许可不发 Probe、同键异摘要冲突、
  未登记节点不重试、partial 不得报 complete、fast 不得 complete、
  伪造 evidence_ref 被拒、过期交付不得确认。
- 守卫（路由契约、覆盖合取）必须做**变异确认**：改掉被守的那一行，确认测试
  真的红，再还原。
- 不把模拟器记作真实能力：本轮验收报告必须区分"协议/单元验证"与"真实 CPU/GPU"。
