# DDP-Federation v1 —— 联邦路由的六份契约

> 状态：**DDP-RESOURCE 已落到库里**（迁移 0015 + `ddp_core.models` 的三个模型
> + `tests/test_resource_layer.py` 6 条）。其余五份不再是"实现为零"：P5 的端点、
> 请求/响应形状与实现进度统一跟踪在 `docs/refactor/P5-INTERFACES-v3.md`
> §2–§3 与机器可读契约 `openapi/federation-tasks-v1.yaml`（**跟踪 ≠ 验证通过**，
> 验收状态以那两份为准）；现状缺口与基线仍见 `docs/refactor/BASELINE-v3.md`。
> 先定义契约再做执行器是刻意的顺序（计划 §9.3）。

这几份契约要回答的问题只有一个：**查了哪里、谁接了单、哪些范围没查到、
结果凭什么可信？**

现有的 DDP-Layout / DDP-Extract 已经把出处下沉到块级与字段级。联邦把同一个
要求推到跨节点：一条答案不但要指回原文，还要能说清它**没有**看过哪里。

| 契约 | 文件 | 回答什么 |
|---|---|---|
| DDP-DISCOVERY | `schemas/ddp-discovery/v1.json` | 有哪些节点、它们真的能干什么、怎么到达 |
| DDP-SCOPE-COVERAGE | `schemas/ddp-scope-coverage/v1.json` | 检索范围（分母）与覆盖账本（分子） |
| DDP-TASK-PROBE | `schemas/ddp-task-probe/v1.json` | 需求、外发许可、实际预检回执 |
| DDP-PLAN-ADMISSION | `schemas/ddp-plan-admission/v1.json` | 执行图、数据边、执行许可、正式接单 |
| DDP-EVIDENCE | `schemas/ddp-evidence/v1.json` | 跨节点证据身份、带出处的答案、交付回执 |
| DDP-RESOURCE | `schemas/ddp-resource/v1.json` | 逻辑资产、固定版本、上传事件（**已有实现**：迁移 0015）|

- 枚举取值**不在 schema 里**：schema 只写 `"x-ddp-enum": "coverage_target_state"`，
  真值由 `scripts/check_federation_contracts.py` 从 `enums.yaml` 注入。
  已解析成品在 `generated/schemas-resolved.json`（入库、过期即红）。
- 夹具在 `fixtures/{valid,invalid}/`，清单 `fixtures/manifest.yaml`。
- 守卫：`scripts/check_federation_contracts.py`（schema ←→ 枚举 ←→ 夹具）、
  `scripts/check_federation_routes.py`（契约端点 ←→ corpus-api 实际路由）；
  两份都已进 `scripts/check.sh` 与 `guards.yml`。

## 为什么不用一个 `can_solve` 或一个 `confidence`

这是整份契约唯一重要的设计决定，其余都是它的推论。

一次联邦问答里有**四件独立的事**，它们各自可以成功或失败：

```text
① 成员名单封没封上        enumeration_state
② 该查的目标查完没有      retrieval_completeness
③ 拿到的证据够不够        evidence_sufficiency
④ 结论对不对             ——— 机器判不了，只能人工评审
```

把它们压成一个绿色「完成」，就是本项目在 M4a 吃过的那个亏的联邦版本：
向量检索静默退回 BM25，界面上只有一个"成功"，**没人发现**，
直到后来加了 `embedding_unavailable` 才看得见。

所以 `CoverageLedger` 上这三个轴是三个独立字段，且 schema 里有三条
机器可查的合取约束（`retrieval_completeness=complete` 要求
`enumeration_state=sealed`、要求 `counts.incomplete=0`、且 **`search_mode=fast`
时永远不许是 `complete`**）。

第④件事在 `ClaimEvidenceBinding` 上分成 `structural_validation`（机器给）
与 `semantic_review`（人给）。**引用可点击率不是正确率** —— 计划 §14.3 原话。

## 副本不是独立共识

任务结果的 `evidence` 保留每份真实副本的节点、资源、版本与证据身份。
`support_groups` 把相同 `source_digest` 或 `excerpt_digest`（包括传递关系）
归为一个保守支持单位，`support_counts.independent_sources` 不随多次上传、
多节点副本或多路径命中增长。覆盖账本的 `counts` 仍是检索目标数，不能当来源数。
每组的 `copies` 列出全部归属；`representatives` 只去重重复摘录，同文档的
不同片段仍供生成使用。生成与 Wiki 来源清单只消费代表，答案绑定的 `support_refs`
由协调者按实际副本归组计算，不相信模型自报的独立来源数。
缺失摘要不构成相等证明；这不是语义相似性判断。`sufficient_by_policy`
只表示存在可引用支持，不表示跨来源证实，更不表示结论已经人工语义核验。

## 不同 embedding 域不共享分数尺

每个检索节点在本域用自己的 embedding 模型编码问题；向量不跨节点转交，
不同维度与模型不需要协商成一个向量空间。HTTP 证据集和任务结果不携带内部
`_score` / `_similarity`。融合保留计划选定的节点顺序与各节点的本地排名，
不能因为另一节点的原始分数数值更大，就把它当作更可靠的证据。
`test_federation_embedding_domains.py` 与真实双 uvicorn HTTP 演练分别使用
3 维和 7 维命名编码器、刻意不同比例的分数；演练使用 SQLite cosine adapter
和真实 HTTP 编码器替身，不把它冒充 pgvector 或 GPU 运行时验证。

## 三个阶段的"不能推断什么"

契约里每个对象都带 `x-ddp-doc`，写的全是这一句的变体。挑三条最容易搞错的：

**有节点描述 ≠ 当前可达。** `NodeDescriptor` 里**没有** `online` 字段。
可达性只能由实际连接验证，所以它住在 `CapabilityProfile.readiness`，
而那个值带 `valid_until` —— 过期记录不是当前能力证明，按 `unknown` 处理。

**`configured` ≠ 能用。** 计划举的例子是 `gpu=true` 不代表所需模型已就绪。
本项目已经踩过这个坑的本地版本：注册表里只有 OCR 专用模型时，抽取平面拿它
去抽值抽不出来，被记成 `not_found` —— 系统能力缺失伪装成"文档里没有"，
后来才加了 `no_instruct` 能力词。联邦侧把它拆成三个字段：静态配置
（`configured`）、周期健康（`readiness`）、本次预检（`ProbeResult`）。

**`retrieval.status=succeeded` ≠ 全文穷尽。** 它只表示按声明的配置执行完了
本次检索。节点内部有分片失败、索引落后或只查了子集时，必须在
`retrieval.internal_limits` 里报出来，而 schema 钉着：
**`internal_limits` 非空就只能是 `partial`**。
少了这条，"一台服务器有多个集合，探测了一个就把整台标成完成"没有任何东西挡得住。

## 两层外发许可，先于 Probe

```text
ExplorationConsent  ──→  远端 Probe  ──→  TaskPlan  ──→  ExecutionConsent  ──→  Admission
（能发什么问题）          （实际探测）      （谁执行）      （能发什么数据）       （正式接单）
```

**第一次远端检索就已经暴露问题、实体、资源名** —— 所以许可在 Probe 之前，
不在执行之前。未授权时只在本地准备任务，连问题都不发出。

`egress_mode: local_only` 在 schema 里是硬约束：`allowed_payload` 必须为空、
`allowed_recipients` 必须为空、`max_egress_bytes` 必须为 0。
一个 `local_only` 却带着 `allowed_payload` 的许可对象是自相矛盾的，
而这种矛盾在运行时的表现是**本地模式偷偷外发** —— 最不该静默的那一类。

## 接单：与既有 `tasks.generation` 是同一个思想

`AdmissionReceipt.delegation_generation` 是防旧结果提交的围栏。这不是新发明 ——
`services/corpus-api` 的 `Task` 模型上已经有一份：lease 只解决"谁**可以**接管"，
解决不了"被判死的旧 worker 其实还活着"，所以最终写入还要比 generation。

内容尚不能校验的接单只记 `waiting_input/metadata_only` 回执：不建执行行、
不入队、不取得租约或槽位，也不调用模型、embedding、parse 或能力网关。
生成就绪检查在内容校验通过之后，不能用预检启动计算。

联邦侧多出来的是**对账**：

- 相同幂等键 + 相同请求摘要 → 返回已有任务
- 相同幂等键 + 不同正文 → `idempotency_conflict`，不许复用不相关结果
- 回执丢失 → `admission_state=unknown`。**`unknown` 不等于「没执行」**，
  要先按幂等键查询对账，不能立刻把有副作用的步骤换个节点重做

任务需求与提交键域包含组织和 acting principal（API key 随所属用户），
不同用户的同名键互不串用。Peer admission 的原始业务键保持不变，接收方按
认证组织与签发节点隔离；不同协调节点不能查出或复用彼此的受理回执。
交付确认先校验结果摘要：已确认后的同键异正文仍返回 `idempotency_conflict`。

### 递归受理与预算份额（P6）

`ScopeManifest.node_routes` 只给间接来源。根按最短路径、再按节点 id 稳定选路，
按第一跳合并为 `delegate`；`delegated_targets` 的路径相对委托者，不含委托者与叶两端。
`budget_share` 是父预算切片，不是新的完整预算；父节点为自己的 HTTP 尝试留额度。
`delegation_path` 从根到当前发出者，接收者已在其中或路径重复时拒绝 `delegation_loop`，
达到份额 `max_hops` 时拒绝 `budget_exceeded`，拒绝发生在落受理与执行队列之前。

子协调者保留原执行许可的接收方集合、授予者与有效期，在已批准叶子图内派生子计划、
步骤和数据边，不新增接收方；数据边包含最终根接收者及其证据下游，所以来源侧策略
不能靠 P→A 或 A→生成节点的中转被绕过。凭证使用 P 自己签名与派生 `peer-*` 只读主体，
`actor_kind=peer` 不把远端用户冒充成本地用户，也不转发长期用户密钥。

迁移 `0042` 的 `federation_delegation_consumption` 以根号／逻辑步骤号保存批准的份额与
实际回报，份额整笔预扣一次，重放不退款；所有物理 HTTP 请求另记账。成功子回报替换
实际报告中的子份额预占，但不改写预扣账本。没有可验证回报时保留预占上界，错误逐叶可见。
回报超份额、目标越界、来源不在分配或摘录摘要不符时整份结果作废，不采用其中的证据。

`CoverageEntry.reported_by` 明确谁自报了叶覆盖；有一条就不能报 `complete`。
递归成功可以是 `succeeded`，但永远只给 `partial` 的范围保证。自报条目的
`probe_receipts` 可以引用实际执行 Admission，不把它冒充根直连来源的规划 Probe。
`ExecutionStatus.actual_index_revision` 来自真实检索结果，不能用 `parse_revision` 替代。
证据的来源、权威、资源与版本不变，只有回传 `relay_via` 增加中继节点。


`state=accepted` 在 schema 里要求三件套同时成立：有 `executor_task_id`、
有 `verified_input_manifest_digest`、`input_validation=content_verified`。
只看文件描述（`metadata_only`）就受理，等于信任客户端声明的哈希 ——
本项目在直传上传那里已经踩过同一个坑（`upload_status` 的 `verifying` 不能跳过）。

## DDP-EVIDENCE 是信封，不是新表

`FederatedEvidence` 的每个字段与当前 `evidence` 表的列映射写在 schema 的
`x-ddp-local-mapping` 里。**其中四个字段在当前源码里没有对应物**
（`origin_node_id`、`authority_node_id`、`excerpt_digest`、
`retrieval_receipt_ref`、`policy_revision`），还有一个语义不同
（`resource_id` ↔ `evidence.document_id`，见 BASELINE-v3 的 C-01）、
一个恒为常量（`source_version_id` ↔ `doc_version`，迁移 0007 起恒为 0）。

那些不是这里漏写，是 P0 记录的真实缺口。**下载 URL 是临时位置，不是证据身份** ——
所以这份信封里没有任何 url 字段。

`Locator` 上有一条约束值得单说：**有 `bbox` 就必须有 `page_size`。**
迁移 0007 的原注释写着"缺它遇到 CropBox 偏移/旋转页会裁错区域"，
而"出处图对不上原文"是最恶劣的错。

## 加一条约束时

1. 改 `schemas/` 下的 schema。枚举字段用 `x-ddp-enum`，**不要手写 `enum` 数组**
   （守卫会当场红 —— 唯一真相只能有一份）。
2. 加一个**正例**和一个**反例**夹具。反例要从正例只改一个字段派生，
   这样它被拒绝的理由只可能是那条规则本身。
3. 在 `manifest.yaml` 里登记，反例必须声明 `violates`（出错的实例路径）；
   路径是 `$`（缺必填字段那类）的还要声明 `violates_message`。
4. `--write` 重新生成 bundle。
5. **变异确认**：把那条 if/then 删掉，守卫必须报
   "本该被拒绝却通过了"。没做过这一步的守卫不算守卫 —— 理由见
   `docs/refactor/FINDINGS.md` 里那几条假守卫。
