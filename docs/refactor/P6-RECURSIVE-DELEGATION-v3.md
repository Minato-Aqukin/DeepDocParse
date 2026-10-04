# P6 递归联邦：子树发现与预算份额委托（设计契约）

权威：工作区 [plan.md](../../../plan.md) §5.3、§7.5、§8.4 与 P6；对应台账 T36／T40／T41／T57／T64／T87。
本文是本轮实现的共同契约：各切片按这里的名字与不变式实现，改动先改契约。

**实现与验证**：`a048c5b`（子树发现、预算份额委托、最近节点）、`cba01f8`（T64 存储上限）、`befe421`（份额从
剩余预算按叶目标比例切出）；四中心实机演练（D 只登记在 B）见
[`artifacts/p6-recursive-delegation-20261004.json`](artifacts/p6-recursive-delegation-20261004.json)，
规模实验见 [`artifacts/scale-storage-20261004.json`](artifacts/scale-storage-20261004.json)。

## 实现前的状态（2026-10-04，`e4650f8`）

- 信任不传递：节点只联系本地已批准的直接成员（control `peer.go` 只给已批准 audience 签凭证，对端
  `authenticatePeerRead` 只认自己已批准的 issuer；corpus `PeerDirectory.client` 对未登记节点零字节拒绝）。
- 目录展开是"网状"：A 展开 P 时会**直接**联系 P 列出的成员 R；R 只在 P 那里登记时，A 记
  `unexpanded_subtrees{R, denied|unknown}`。`scope_remote_sources` 只记第一跳 `via_node`，完整路径丢失。
- 任务只有一跳：协调者对每个目标直接 probe／admission；执行者没有预算份额，`delegation_generation`
  只是围栏代次；`relay_via` 有校验但没有生产者。

## 术语

- **根协调者**：用户任务所在节点（A）。
- **路由 `via_node_ids`**：从某个视角到目标来源之间的中间节点，按顺序排列，不含两端。
  A 看 R（只登记在 P）为 `[P]`；A 看 S（只登记在 R，R 只登记在 P）为 `[P, R]`；直接成员与本地为空。
- **委托者**：路由的第一跳（A 视角的 P）。它对分到的叶目标充当子协调者。

## 不变式（测试与演练必须证明）

1. **信任不传递**：任何节点只联系自己的已批准直接成员；A 永不联系 P 背后的 R。
2. **环路与深度**：每个委托请求带 `delegation_path`（从根到发出者的节点序列，根在前）。接收者在路径里
   → 拒绝 `delegation_loop`；路径长度达到份额的 `max_hops` → 拒绝 `budget_exceeded`。目录子树读同理带
   `path`，响应方不展开路径上的节点。
3. **一个叶目标只执行一次**：根按 `(origin_node_id, collection_id, operation)` 去重；同一来源多条路由时
   取最短（再按节点 id 字典序），其余路由不产生第二个业务任务。委托者只执行分到的叶目标，不能新增。
4. **预算份额不放大**：每个 `delegate` 步骤带 `budget_share`（`max_requests`、`max_bytes`、`max_hops`、
   `max_probes`、`deadline`），在规划时从根预算切出，各份额之和加上根自己的预占不超过根上限；根账本在
   该逻辑步骤首次出站时一次性预占整个份额（与 F14 同一逻辑步骤键，不退款）。委托者用份额建自己的预算，
   自己的物理请求与再委托的子份额都从份额里扣；结果回报实际消耗；根记录"预占 vs 实际"，回报超出份额
   则整个委托结果作废（叶目标记 `failed`／`budget_exceeded`）。发现同理：A 把剩余发现额度作为子树读的
   上限传给 P，P 回报实际消耗，计入 A 的发现额度。
5. **来源身份不变**：证据信封的 `origin_node_id`／`authority_node_id`／资源与版本永远是叶节点的；中继
   只出现在 `relay_via`，没有任何节点改写来源。
6. **保证等级**：经委托得到的叶覆盖条目带 `reported_by`（委托者节点 id）；只要账本里有这样的条目，
   `retrieval_completeness` 就不能是 `complete`（§7.5"首版不把不可核验的黑盒自报当成叶节点穷查证明"）。
7. **许可覆盖整条链**：路由上的每个节点与每个叶来源都是问题文本的接收方，探索许可与执行许可必须列出它们；
   计划数据边：问题 A→叶（`relay_via` = 路由），证据 叶→A（`relay_via` = 反向路由）。叶执行者按整条链
   （最终接收方 A 也在内）核对来源侧 `onward_recipients`。
8. **自治**：上级（委托者或更高层）停止时，下级照常服务本域（自己的本地任务、自己的直接成员）；根把该
   子树记为 `unreachable`，不当成空集。

## 契约增量

### 发现（control，Go）

- `ScopeManifest.node_routes`（可选）：`[{node_id, via_node_ids: [node_id, ...]}]`，`via_node_ids` 至少一项；
  只列不能直接到达的来源节点；计入 `manifest_digest`。`expanded_members` 的 `TargetKey` 形状不变。
- 对等读 `GET /api/v1/federation/subtree`（节点凭证操作 `directory_subtree_read`，与 members／collections
  读同一鉴权与快照分页：`limit`、`snapshot_id`、`cursor`）。参数 `path`（逗号分隔的调用链，根在前）、
  `max_requests`、`max_nodes`。响应页：`targets: [{target_key, via_node_ids}]`（路由相对响应方，不含响应方
  自己）、`registry_revision_vector`、`unexpanded_subtrees`、`enumeration_state`、
  `consumption: {requests, nodes}`。响应方只公开其按自身可见性规则允许该调用方组织看到的成员。
- A 的展开：P 列出的成员 A 不能直接联系时，A 改为读 P 的子树（一次快照分页），目标的 `via_node_ids`
  前缀加上 P；P 回报的修订向量与未展开子域并入 A 的清单（`directory_ref` 标明来自 P 的报告）；
  P 的 `consumption` 计入 A 的发现额度。能直接联系的成员保持现有网状展开。

### 计划与受理（corpus）

- `PlanStep.operation` 新增 `delegate`；`PlanStep` 新增可选 `delegated_targets: [{target_key,
  via_node_ids}]`（路由相对该步执行者）与 `budget_share`。
- `AdmissionRequest` 新增可选 `delegation_path`（根在前，最后一项是发出者）。根直接发出的请求为 `[A]`。
- `CoverageEntry` 新增可选 `reported_by`。
- 委托执行者（P）：受理时核对不变式 2、4、7，以及每个叶目标"来源是自己，或来源是自己的已批准直接成员，
  或 `via_node_ids[0]` 是自己的已批准直接成员"；随后以子协调者身份执行：对直接成员的叶目标发普通单跳
  retrieve（子计划 `root_coordinator_node_id` = P，数据边含最终接收方），对更深的叶目标向下一跳再委托
  （子份额、`delegation_path + [P]`）。结果：每个叶目标的 `{target_key, state, last_error,
  actual_index_revision, evidence_refs}`、实际消耗、证据集（叶来源不变）。
- 根协调者（A）：带路由的目标按第一跳分组成 `delegate` 步骤，不做规划期 Probe；执行时预占份额、受理、
  轮询、读取，校验（消耗 ≤ 份额、叶目标 ⊆ 分配、证据来源 ⊆ 分配的叶来源、摘录摘要），并入覆盖账本。

### 最近节点（T36）

候选先按硬约束过滤（属于冻结范围、操作匹配、生成能力 ready、许可允许），过滤后的排序依次为：
`local_first` 时本地优先 → 路由长度（本地 0、直接成员 1、委托 1+len(via)）→ 可达性（近期负面缓存
命中的节点排后）→ 现有描述符得分 → 稳定键。距离只做同类之间的取舍，永远不能让缺资料或缺能力的
近节点被选中。

### 规模（T64）

普通中心不因登记节点增多而镜像远端原文／全文／向量；随节点数增长的持久数据只有有上限的目录快照、
范围清单与缓存。实验以 N 个替身成员测量各表行数与字节、对象存储，并给出上限来源。
