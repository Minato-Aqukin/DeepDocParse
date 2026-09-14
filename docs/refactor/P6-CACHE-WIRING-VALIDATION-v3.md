# P6 缓存接入协调者、负面缓存与摘要排序自验（2026-09-14）

当前状态：**探测复用、负面缓存、远端目录摘要排序已接入协调者并自验通过**。
执行权威是工作区 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md` §5.5 / P6；
缓存对象的冻结形状见 `P6-CACHE-VALIDATION-v3.md`，本文件只记录**协调者怎么用**
与这次接入的验证结果。**这不是 commit 前的独立验收，也不代表 P6 全部出口**
（集合摘要增量构建、排序早停、路由质量基准仍缺）。

## 1. 范围

| 文件 | 内容 |
|---|---|
| `services/corpus-api/ddp_corpus/federation_tasks.py` | 计划期目录摘要（本地 + 远端）、探测复用、负面缓存、执行按计划恢复目标、事件/账本可见标记 |
| `services/corpus-api/ddp_corpus/federation_peers.py` | `PeerClient.published_collections`（对等目录读一页）与 `PeerDirectory.collections`（有界分页跟随） |
| `services/corpus-api/tests/test_federation_cache_wiring.py`（新，12 条） | 复用/负面/摘要排序/许可/预算/撤回的接入行为与负向 |
| `services/corpus-api/tests/test_federation_tasks.py` | stub peer 增加目录端点与按请求回集合的回执；`task_spec` 支持 operation；`scope_manifest` 支持逐节点修订向量 |
| 本文件 | 记录 |

没有改 `eval/routing/**`（运行器/夹具/报告归并行切片，未编辑、未写报告）；
没有改缓存表与冻结 API（`cache.py` 一行未动），没有改契约、配置项与迁移。

## 2. 接入语义（协调者实际执行的规则）

### 2.1 探测复用：`create_plan` 内、任何真探测之前

对每个进入候选的目标（`corpus.retrieve` 一类；固定资源定位 `corpus.locate`
不参与——缓存 API 只复用证据检索回执）：

1. **只有拿到"当前索引修订"才可能复用**：索引修订来自计划期取到的集合描述符
   （本地 `catalog.visible_catalog` / 远端对等目录读）。没有描述符（未授权、
   目录读失败、预算耗尽、集合已撤回）就没有当前修订，直接真探测——不拿回执里
   的旧修订冒充当前修订。
2. `cache.find_reusable_probe(session, actor, target_key=…, query_digest=<与探测请求
   同一个 canonical digest>, index_revision=<描述符修订>, policy_revision=
   "registry:<node>:<manifest registry_revision|unbound>", now)`。TTL 取持久行
   `expires_at - observed_at`、摘要与索引修订比对全部由冻结实现判定。
3. **调用者绑定复核**：冻结 API 的作用域是 organization；命中后再按同一
   `(org, target, collection, kind)` 的至多 64 行做回执内容比对，取到行后要求
   `row.actor_id == federation.acting_actor(actor)`。远端回执的行 id 与回执里的
   `probe_id` 不同，不能按 id 取——这个复核同时决定**本地行 id**（见第 4 点）。
4. **复用命中**：计划步骤的 `probe_refs` 指向被复用回执的**本地行 id**；不发
   HTTP、不 `budget.reserve("probe")`、不落新的 `federation_probes` 行（复用不是
   新观测）。`plan_ready` 事件的 `payload.reused_probes` 逐目标列出被复用的行 id。
5. **执行阶段**：`_load_probe` 以"回执行创建时刻早于本任务受理时刻"识别缓存回执，
   把覆盖条目的 `search_profile` 写成 `cached_probe_receipt`；`probe_receipts`
   引用被复用回执，`attempts`/`used_budget` 仍是**本次执行**的真实记账。
6. 同一组织内不同调用者不复用；撤回/撤权的集合不产生描述符，因此结构上不可能
   命中旧回执（有测试钉着）。

`policy_revision` 用节点目录修订（登记/可见性变化的载体）。当前 P5 回执不记录
`policy_revision`（缓存切片已声明这是已知缺口），所以这一项只在将来有记录时
生效——现在不假装它在比对。

### 2.2 负面缓存：联系对端之前

- 键 = `cache.negative_cache_key(organization_scope(org), <peer node>, <node_revision>)`；
  `node_revision` 取自 ScopeManifest 的 `registry_revision_vector`。**该节点没有
  修订条目就不查也不记**——不编一个修订号去绑，否则新资料会被恒定键挡住。
- 远端目标在**预占探测预算之前**先 `cache.get_negative`。命中：目标结局记
  `unreachable` 或 `denied`（理由前缀决定），零 HTTP、零预算；`get` 只加命中计数，
  不续 `expires_at`（有测试逐字段核对）。
- 只有 `PeerUnavailable` 且**传输失败**（`status is None`，记 `unreachable:<code>`）
  或**显式 403**（记 `denied:<code>`）才 `cache.record_negative`（短 TTL 60s）。
  4xx/5xx 协议错误不进负面缓存——把"对端坏了"缓存成"对端不可达"会挡住恢复后的重试。
- 新节点修订产生新键，TTL 到期自然失效；两者都有测试证明"必须重新接触"。
- 负面缓存只覆盖计划期的目标探测联系；执行期的 admission/证据集读取路径不查它
  （那是真实取数，不是探索备忘）。

### 2.3 摘要排序：计划期取描述符，内核排序，计划是执行的权威

- `_gather_descriptors`：本地已发布集合走 `catalog.visible_catalog`（本库读，不出网）；
  远端逐节点走 `PeerDirectory.collections(node, reserve=…)` → `PeerClient.published_collections`
  打对等目录端点 `/internal/federation/published-collections`，用同一套 peer 认证头
  （`Authorization` / `X-DDP-Peer-Token` / `X-DDP-Target-Node`），按快照 + 游标
  **跟随到终止页**（`complete=true` 是完整性的证明），上限 4 页 × 100 条。
  只采信 `origin_node_id` 等于该节点自己的描述符（坏对端不能借用别家 origin 影响排序）。
- **许可门先于任何字节**：`_directory_denial` 要求 `egress_mode=listed_nodes`、
  节点在 `allowed_recipients`、`collection_filters` 在 `allowed_payload`。任一不满足
  就跳过该节点的目录读——**零请求**，排序退化为确定性 `local_first`。
- **预算**：每页请求前 `RootBudget.reserve("discovery")`（与请求额度共用，T87；
  同时受 `max_discovery_requests` 子额度约束）。耗尽只停止翻页，已读到的页保留；
  计划声明的 `max_requests` 通过 `_root_budget(..., discovery_count=已消耗)` 计入。
- 失败/超时/坏页只意味着该节点没有摘要：`_gather_descriptors` 捕获后记在
  `plan_ready` 事件的 `descriptor_sources`（本地条数 + 逐远端原因）里，规划继续。
  摘要**只影响顺序和 fast 取谁，永不删除成员**：穷查仍枚举全部目标。
- 排序实现只有内核一份：`_select_targets` 把描述符原样交给 `routing.candidates`。
- **执行不重排**：`_plan_selected_targets` 用每个 retrieve 步骤的
  `executor_node_id` + `fixed_inputs`（`collection:<id>` / 裸资源 id）把计划映射回
  枚举目标，`_execute_plan` 不再调用 `_select_targets`。否则执行阶段没有目录摘要
  可重排，retrieve 步骤与目标会 zip 错位、把一个目标的回执静默挂到另一个目标上
  （有变异确认的测试钉着）。

### 2.4 冲突轴（评测发现 1）的裁决：本切片不做，如实记录

`coverage.sufficiency` 支持 `conflicting`，协调者的账本仍不计算它，互相矛盾的
两版证据会被记为 `sufficient_by_policy`。**本切片明确不做冲突检测**，理由：

1. 评测夹具里的"矛盾"是**两个不同 `source_digest` 的不同文档**给出不同参数值；
   任务书里给的确定性候选规则（同一 `source_digest`、同一 locator 的摘录不一致，
   或同一摘要的两个存活版本同时出现）对这种冲突**不会触发**——拿它当"已处理"
   是假的。
2. 判断两份**不同来源**是否真的互相矛盾是语义判断，不是信封字段能机械推出的：
   同一摘要的两个版本也可能只是重复上传，标成 `conflicting` 是假阳性
   （T44 只要求"不制造独立共识"，不等于"互相矛盾"）。
3. 语义轴已经如实暴露：答案绑定一律 `semantic_review=needs_review`，证据与来源
   逐条可回溯；冲突复核属于上游语义审阅，不在本次路由/缓存接入的写入范围。

因此 `evidence_sufficiency` 维持现有规则，本文件不把它读成"冲突已处理"。

## 3. 测试与变异确认

`services/corpus-api/tests/test_federation_cache_wiring.py` 12 条，覆盖：

| 用例 | 守护的语义 |
|---|---|
| `test_fast_selection_uses_peer_catalog_descriptors` | 描述符真的进 `routing.candidates`；9 目标上限 8 时摘要把 col-9 排进候选、身份顺序会漏掉它；执行跑的是计划选中的集合（col-8 记 `not_attempted/search_mode_fast`）；探测数仍是 8 |
| `test_create_plan_reuses_a_valid_probe_without_touching_the_peer` | 复用零请求、零新行；计划 `probe_refs` 指被复用回执；事件 `reused_probes`；执行账本 `search_profile=cached_probe_receipt` |
| `test_reuse_is_rejected_when_the_index_revision_moved` | 索引修订前进必须重新探测（变异靶子） |
| `test_reuse_is_rejected_for_another_query_or_an_expired_receipt` | query digest 不同 / 回执过期都退回真探测 |
| `test_reuse_never_crosses_the_calling_actor` | 同组织不同调用者不复用 |
| `test_negative_hit_skips_the_peer_and_does_not_extend_its_life` | 命中零 HTTP；`expires_at` 不变、hits 只加 1；事件结局 `unreachable` |
| `test_a_new_registry_revision_is_never_blocked_by_a_negative_entry` | 新修订/过期后必须重新接触 |
| `test_denied_probe_is_cached_as_denied_not_failed` | 403 记 `denied`，命中零 HTTP |
| `test_remote_catalog_fetch_needs_the_collection_filters_payload` | 载荷未批准零目录请求，事件记 `payload_not_allowed`，规划照常 |
| `test_remote_catalog_fetch_respects_the_discovery_budget` | 预算 0 零请求；预算 1 只发一页且第一页摘要仍参与排序 |
| `test_remote_catalog_failure_degrades_instead_of_blocking_planning` | 目录不可达只降级排序，不影响探测 |
| `test_withdrawn_collection_is_never_reused` | 撤回前同集合可复用、撤回后没有描述符故不复用，失败的重新探测不伪造回执 |

**人工变异确认**（改掉被守的那一行 → 目标用例必须红 → 还原后 `sha256` 与
变异前一致、套件回绿；每次先确认文件内容真的变了）：

| 变异 | 结果 |
|---|---|
| 复用调用写死旧 `index_revision`（跨修订强制复用） | 红：`…index_revision_moved` |
| `node_revision` 置 `None`（去掉负面缓存查询） | 红：`…negative_hit_skips…` |
| 负面键的 `node_revision` 换成常量 `"1"`（不绑修订） | 红：`…new_registry_revision…` |
| `_directory_denial` 直接 `return None`（目录许可门失效） | 红：`…needs_the_collection_filters_payload` |
| `_reserve` 恒 `True`（发现预算失效） | 红：`…respects_the_discovery_budget` |
| `_select_targets` 把描述符换成 `[]`（摘要排序失效） | 红：`…uses_peer_catalog_descriptors` |
| `_find_reusable_probe` 去掉 actor 绑定复核 | 红：`…never_crosses_the_calling_actor` |
| `_execute_plan` 改回 `_select_targets` 重排（执行不认计划） | 红：`…uses_peer_catalog_descriptors` |

## 4. 路由评测：before / after

**冻结运行器未改**，`eval/routing/**` 一个字节未动，因此运行器基线**没有变化**：

| 运行 | fast 绝对召回 | 说明 |
|---|---|---|
| 基线（`eval/reports/routing-156461c66d71290b.json`） | **63.3%（19/30）** | 运行器给 `_select_targets` 不传描述符 |
| 接入后重跑同一运行器（`python -m routing.run --no-write`） | **63.3%（19/30）** | 运行器路径未变，同样不传描述符 |

**为什么接入后运行器数字不变（如实说明，不把它读成"接入无效"）**：

1. 运行器 `harness.run_question` 直接调
   `coordinator._select_targets(all_targets, task_spec, local_node_id)`，本来就不传
   描述符；接线的取数发生在 `create_plan` 的 `_gather_descriptors`，运行器不经过它。
2. 运行器默认探索许可只批准 `query_text`、且没有 `max_discovery_requests`；
   按接入后的真实协调者路径，这份许可**也不允许**读远端目录（载荷门 + 发现预算 0）。
   运行器要量到排序收益，得先把许可扩到 `collection_filters` 并给发现预算——
   那是评测夹具/运行器的写入范围（并行切片），本切片不动。

**敏感性探针（越界脚本，`/tmp` 下，不入仓库、不改运行器）**：把夹具描述符直接喂给
`_select_targets`（等价于许可允许目录读时的排序），其余完全走运行器：

| 类别 | 基线 fast | 描述符排序 fast | Δ |
|---|---|---|---|
| `b-only` | 3/3 | 3/3 | 0 |
| `conflicting-versions` | 5/6 | 3/6 | −2 |
| `cross-collection-split` | 3/6 | 6/6 | +3 |
| `local-similar-decoy` | 1/3 | 1/3 | 0 |
| `local-solvable` | 3/3 | 3/3 | 0 |
| `nearest-node-decoy` | 1/3 | 2/3 | +1 |
| `private-decoy` | 3/3 | 3/3 | 0 |
| `summary-hidden` | 0/3 | 0/3 | 0 |
| **合计** | **19/30 = 63.3%** | **21/30 = 70.0%** | **+2** |

探针与基线使用同一夹具、同一计划目标数（fast 216）与远端探测请求数（162）——
排序只换目标，不多探一个。诚实结论：**排序在压力夹具上净提升 2 条，但不是一致
改进**——`cross-collection-split` 与 `nearest-node-decoy` 变好，`conflicting-versions`
反而丢 2 条（摘要把别的集合排进了 8 个上限，挤掉了其中一个矛盾版本）；
`summary-hidden` 仍在 fast 上限之外（内核排序把必要集合排到 12/12，与
`P6-ROUTING-EVAL-v3.md` 的记录一致）。以上是合成夹具数字，不是真实语料结论。

## 5. 验证环境与结果

| 项目 | 结果与证据 |
| --- | --- |
| 全量门禁 | **29/29 PASS**（2026-09-14，改动后重跑 `./scripts/check.sh`） |
| corpus-api 默认套件 | **749 passed, 17 skipped**（第二轮复核修复后；skip 全部是未设 DSN 的 opt-in PG 用例，含 `test_federation_pg.py` / `test_federation_concurrency_pg.py`） |
| 本切片新用例 | `test_federation_cache_wiring.py` 12 条，全绿 |
| eval 套件 | **66 passed**（`cd eval && ../.venv/bin/python -m pytest -q`） |
| 路由运行器 | 同夹具两次运行摘要不变；`fast 63.3%`（见 §4；敏感性探针另计，不在仓库内） |
| ruff（`F,B`，既有 ignore） | 全绿 |
| 变异确认 | 8 处，全部先红后还原；还原后 `sha256` 与变异前一致（`federation_tasks.py` `13e688be…`，`federation_peers.py` `814afda7…`） |

没有 commit/push。本记录由本次作者自验，不替代独立 agent 的提交验收。

## 6. 限制与后续

- **目录摘要不缓存**：本切片每次规划重新读本地/远端目录（受许可与发现预算约束）。
  用 P6 缓存把描述符按 `catalog.cache_revision` 绑键投影是下一步；现在不做，
  因为远端描述符的跨修订陈旧正是我们要让 `find_reusable_probe` 避免的那类错误。
- **负面缓存只覆盖计划期目标探测**：执行期的 admission/证据读取失败不加负缓存；
  这是刻意的（那是真实取数，重试语义由队列/对账负责）。
- **复用不跨调用者**是对缓存 API 的收紧（缓存 API 按组织作用域；同轮复核起
  `require_execution` 也按 actor 绑定过滤，见下方修复记录）。复核要按内容扫
  至多 64 行，成本只在命中时发生。
- **`policy_revision` 目前是目录修订代理**：P5 回执不记录它，所以真正的策略比对
  要等协议侧补字段后自动生效（与缓存切片记录的缺口一致）。
- **冲突轴**：本切片不做，理由见 §2.4；评测报告继续把它记成"观察到冲突、账本
  未计算冲突"。
- **执行阶段不再重排**是这次接入的硬约束：任何想法要在执行期重新排序的改动，
  必须同时改掉 retrieve 步骤与目标的装配方式，否则会静默错位挂回执。
- **真实环境未验**：远端目录读、复用跨任务命中的真实时序、真实对等认证
  （P4 未完成）都只有协议/单测级证据，真机 e2e 留到有 GPU/多节点的环境。

## 7. 第二轮独立复核修复记录（2026-09-14）

本切片的缓存路径没有行为变化（`cache.py` 仍未动），复跑全绿；相邻的 P5
执行/队列修复直接影响本切片验证环境，记录如下：

- **F5 执行读取/取消收紧到调用者绑定**：`federation.require_execution` 现在
  按 `admission.actor_id == acting_actor(actor)` 过滤（管理员仍可复核），
  与 `cache.find_reusable_probe` 命中后的调用者复核（本切片 §2.3）同一判据。
  缓存复用路径本来就有这道复核，不受影响；冻结 API 的组织级可见性从此不再
  足够读到别人的执行结果。
- **F3 清扫新增队列死任务分支**：不碰缓存表；有界缓存的写入/驱逐逻辑未变。
- **F1 取消终态**：`resume` / `POST /tasks` 对已取消任务 409 `task_cancelled`
  （`federation_error` 新增值，三语言生成物同步）。缓存不产生该错误码。
- 复跑：corpus-api **749 passed / 17 skipped**（PG opt-in 17 条在
  `check_federation_pg.sh` 下全绿），本切片 12 条用例不受影响。
