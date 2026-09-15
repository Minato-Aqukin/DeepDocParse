# P5 联邦业务接口冻结（v3）

> 2026-09-13。本文件是 P5（Probe / TaskPlan / Admission / 覆盖账本 / 快速与穷查 /
> 交付）并行实现的唯一接口依据。执行权威仍是工作区
> `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md` §6–§9；本文件只把其中的
> 对象、签名与端点冻结成各方可同时编码的形状。冲突时以计划原文为准，并回来改这里。

## 0. 范围与非目标

本切片实现计划 §11 的 **P5**：探测前探索许可、真实能力/资源/证据 Probe、类型化
任务图、持久 Admission 与幂等对账、快速/范围穷查、覆盖账本、跨中心证据融合与
整项答案委托、交付回执。

**非目标（本轮不做，须在报告里如实列出）**：
- P6 递归目录/多级预算/路径环路/缓存（A/B→P→R）。
- P7 安装包、真实 GPU profile、生产迁移。
- ~~把联邦执行挂到 `corpus.tasks` worker 队列~~：**P5 队列切片已实现**
  （2026-09：`federation.admit` 与协调者受理都在同一个事务里排
  `federation_execute` / `federation_plan` 持久任务，worker 领取执行；
  `FEDERATION_EXECUTION_INLINE=true` 只保留给没有 worker 的部署）。
  细节与验证记录见 `P5-QUEUE-VALIDATION-v3.md`。
- ~~`task_status` 新增 `cancelled` 枚举~~：**已连同 worker 状态机落地** ——
  `queue.cancel` 幂等落终态、generation fencing、`claim` 不领取 cancelled、
  worker handler 不写覆盖；协调任务/执行行的取消都以 `cancelled` 表达。
- 跨节点密钥交换（P4 未完成）：远端 peer 认证采用管理员配置的固定信任域凭据
  （计划 §5.1 "登记密钥/认证方法"），没有配置的节点一律 Fail Closed。

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
4. 幂等：同键同 `request_digest` 返回已有 receipt（200，不复算执行）；
   同键不同摘要 409 `idempotency_conflict`。
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
| POST | `/api/v1/task-plans/{root_task_id}/approve` | 批准计划修订 + 执行许可 |
| GET | `/api/v1/tasks` | 调用者**本人**的任务列表（创建时间倒序、键集游标，坏游标 400 `invalid_cursor`）；只带需求摘要与状态轴，结果按 id 读。管理员也只列自己的 |
| POST | `/api/v1/tasks` | 受理已批准计划（幂等键）并排入持久队列：202 新受理 / 200 重放，执行由 worker 推进；已取消任务 409 `task_cancelled` |
| GET | `/api/v1/tasks/{root_task_id}` | 权威状态、结果、覆盖引用、消耗 |
| GET | `/api/v1/tasks/{root_task_id}/coverage` | `ddp-scope-coverage/1#CoverageLedger` |
| GET | `/api/v1/tasks/{root_task_id}/events` | 带序号的可恢复事件 |
| POST | `/api/v1/tasks/{root_task_id}/resume` | 重判权后补做未完成目标；已取消任务 409 `task_cancelled`（终态不许复活） |
| POST | `/api/v1/tasks/{root_task_id}/cancel` | 显式、幂等取消；`cancelled` 是终态，迟到写入一律被状态守卫拒绝 |

- **探索许可门**：没有有效 `ExplorationConsent`（或 `egress_mode=local_only`）时，
  `/task-plans` 不得向任何远端发出 Probe，返回 `egress_denied`；问题/实体/资源名
  一个字都不出网。`task-intents` 的请求体携带用户已批准的探索许可，协调者只做
  校验与持久化，**不代用户签署**。
- **执行许可门**：`/tasks` 必须校验 `ExecutionConsent.plan_digest` 与提交的 plan
  修订一致、接收方集合覆盖所有数据边（含 relay），否则 `egress_denied`。
- **fast**：按 `routing.candidates` 取有界候选，统一融合，结果
  `retrieval_completeness="partial"`，响应显式列出未检索目标。
- **exhaustive_scope**：以 `ScopeManifest.expanded_members` 为分母，逐目标 Probe；
  失败的记 `unreachable/failed` 并保留在分母；全部成功且 enumeration sealed 才
  允许 `complete`。
- **answer 步骤（本切片：协调者本地生成）**：规划时按能力清单生产者
  （`capabilities.collect_capability_profiles`）判定本层 `rag.answer.cited` 是否
  `ready`；就绪才在计划里保留一个 `executor_node_id` 为协调者的 `answer` 步
  （`depends_on` 为全部 retrieve 步，`fixed_inputs` 含 query），根预算给固定的
  生成 token 额度。执行时用融合证据的编号上下文调用 OpenAI 兼容 chat 上游
  （`upstream.chat_request`），并以 `ddp_core.agent.assertions_from_text` 做结构
  校验：无断言、有断言无支撑、或引用不在本次融合证据集合内，一律拒绝
  （`answer=null`、`validation_state=failed`、`unsupported_generation`），
  **不修补引用**。成功时结果带 `answer`、`claim_evidence_bindings`（每条
  `structural_validation=passed`、`semantic_review=needs_review`）、`provider`、
  `disclosure`、内核判定的 `evidence_sufficiency` 与 `validation_state`。
  未就绪或生成失败时只写明确的 `answer_reason`（`local_model_missing`、
  `upstream_error`、`no_model_output`、`budget_exceeded` 等）并保留证据，
  **不把检索任务标失败**。远端执行者的证据回传仍走 `routing.plan_steps` 生成的
  `evidence_excerpts` 数据边，由协调者消费。
- **仍然待做**：把整项答案委托给已接单的生成节点（admission
  `operation=answer`，把证据/子图经类型化数据边外发给远端生成者）。本切片只
  实现协调者本地生成，`plan_steps` 因远端 `can_generate` 生成的 answer 步在
  规划时被显式丢弃。任何节点都无生成能力时，仍只返回证据与
  `evidence_sufficiency`，**不伪造答案**。
- **交付**：结果默认 `retention=temporary`，`delivery_state` 为 `not_requested`
  （留在中心）或 `pending`（等待客户端 `POST /api/v1/deliveries/{id}/ack`）。
  TTL 到期未确认 → `expired`，不得显示"已保存本地"。

## 4. 持久化（corpus alembic 0027）

- `federation_probes`：probe_id、organization_id、actor_id、target_node_id、
  task_spec_digest、consent_ref、probe_kind、collection_id、query_digest、
  state（coverage_target_state）、result_json、expires_at、created_at。
- `federation_admissions`：admission_id、organization_id、actor_id、
  idempotency_key、request_digest、plan_digest、root_task_id、step_id、
  delegation_generation、issuer_node_id、executor_node_id、state（admission_state）、
  input_validation、executor_task_id、verified_input_manifest_digest、
  effective_policy_ref、receipt_json、receipt_revision、created_at、updated_at；
  unique `(organization_id, idempotency_key)`。
- `federation_executions`：executor_task_id、admission_id、root_task_id、step_id、
  operation、state（task_status）、generation、lease_until、result_ref、
  evidence_set_ref、result_json、error、created_at、updated_at。
- `federation_requests`（协调者）：root_task_id、organization_id、actor_id、
  task_spec_digest、scope_id、scope_digest、search_mode、planning_state、
  plan_revision、plan_digest、execution_consent_ref、status（task_status）、
  retrieval_completeness、evidence_sufficiency、result_json、coverage_ref、
  delivery_id、delivery_state、error、created_at、updated_at。
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

`settings.federation_peers`：JSON 对象
`{node_id: {"endpoint": "https://…", "service_token": "…", "peer_token": "…"}}`，
由管理员按节点接入流程登记（计划 §5.1）。`service_token` 是对端要求的服务凭据
（`Authorization`），`peer_token` 是对端 `FEDERATION_PEER_TOKEN` 接受的同伴凭据
（`X-DDP-Peer-Token`）。约束：
- endpoint 必须是 HTTPS、无 userinfo/query/fragment；HTTP 只允许显式的
  `federation_allow_loopback` 测试开关配合 `127.0.0.1/[::1]`。
- token 绝不回显、绝不入日志、绝不进错误消息；比较用 `hmac.compare_digest`。
- 未登记节点：`unreachable`（覆盖账本如实记缺口），不发请求；请求携带目标
  `X-DDP-Target-Node`，接收方校验 token 后只服务自己的数据。
- 出站 HTTP 必须 `trust_env=False`、`follow_redirects=False`（仓库铁律 8）。
- **已知局限**：一个同伴凭据由所有已登记同伴共享（每个节点的
  `FEDERATION_PEER_TOKEN` 是单值）。按同伴颁发限定 audience/操作/有效期的委托凭证
  属于 P4 未完成的密钥交换，不在本切片内谎称已完成。

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
