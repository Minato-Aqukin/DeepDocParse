# P6 路由/覆盖评测 v3（合成夹具基线）

> 2026-09-13。范围：计划 v3 §7（路由算法与覆盖账本）与 §14（专项实验）里
> **"路由是否找到目标证据、付出了什么成本"** 这一问的可重复离线评测。
> 本文记录的是本机一次真实运行的结果：夹具、运行器与报告都已落盘。
> **所有数字都是合成夹具上的路由覆盖数字，不是真实语料的质量结论，
> 也不代表 P6 的完成验收**；本轮工作树未 commit。

## 1. 交付物与复现

| 位置 | 内容 |
|---|---|
| `eval/routing/` | 夹具构造器（`dataset.py`）、关键词预言机（`executor.py`）、内核路径运行器（`harness.py`）、分轴报告（`report.py`）、入口 `python -m routing.run` |
| `eval/routing/fixtures/federation-v3.json` | 冻结夹具，内容摘要 `sha256:156461c66d71290b65798f321000ed113261c8f4ea8e7339db0e0e9dc96178c4` |
| `eval/reports/routing-156461c66d71290b.json` | 本次基线运行报告（`schema=ddp-routing-eval/1#Report`，含 `summary_markdown`） |
| `eval/tests/test_routing_eval.py` | 评测器自身的回归与变异确认（21 例） |

```bash
cd DeepDocParse/eval
../.venv/bin/python -m routing.run            # 跑冻结夹具 → 写 eval/reports/routing-<digest>.json
../.venv/bin/python -m routing.run --rebuild  # 按构造器重建并冻结夹具（改变夹具才用）
../.venv/bin/python -m pytest -q              # 66 passed（原 45 + 本包 21）
python -m routing.dataset --check             # 只校验冻结摘要
```

> 仓库的虚拟环境在仓库根 `DeepDocParse/.venv`；从 `eval/` 出发的相对路径是
> `../.venv`（任务书里写的 `../../.venv` 指到了工作区根，那里没有 venv）。
> 报告内容与运行顺序无关：同一夹具两次运行字节一致（已用 `cmp` 验证）。

## 2. 夹具（冻结、可摘要验证）

**形状**：6 个逻辑节点（`node-a` 本地协调者，`node-b`…`node-f` 远端）·
12 个公开集合 + 1 个私有集合 · 43 份小文档 / 43 条证据 ·
27 道人工标注题 = 9 类 × 3。其中 30 条是"必需证据"，10 条是公开诱饵，
3 条是私有诱饵。

**问题类别与设计意图**（计划 §14.1 的十类去掉"提示注入"，因为本轮不调用任何
模型，注入无从测量 —— 见 §7）：

| 类别 | 题数 | 设计意图 |
|---|---|---|
| `local-solvable` | 3 | 证据只在本地集合；fast 的 local_first 必须真的探到 |
| `b-only` | 3 | 证据只在 node-b；fast 必须越出本地 |
| `cross-collection-split` | 3 | 答案需要两个集合各一条；2 题有一半在候选上限外 |
| `nearest-node-decoy` | 3 | 最近节点有相似内容但无答案；2 题真证据在 fast 候选上限外 |
| `local-similar-decoy` | 3 | 本地有相似命中；2 题真证据在远端候选上限外 |
| `summary-hidden` | 3 | 唯一证据在 `col-f-misc`（摘要写 `misc/unlabeled`） |
| `no-evidence-in-scope` | 3 | 范围内零命中；穷查应 `complete` + `insufficient` |
| `conflicting-versions` | 3 | 两版矛盾值都标注为必需；1 题第二版在候选上限外 |
| `private-decoy` | 3 | 私有集合里有近似证据，任何探测/返回都是违规 |

**冻结口径**：`dataset_digest` = 去掉该字段后 canonical JSON（sorted keys、
无空白）的 `sha256`，由 `ddp_core.application.plans.digest` 计算 ——
文件缩进/键序变化不改变数据身份；`load_frozen` 每次重算，摘要不符直接拒绝。
夹具构造器还做两道自检（坏夹具进不了仓库）：

1. 每题必需证据必须落在 scope 内、且**只要其集合被探到，就必须落在候选
   上限内**（用执行器同一个 `rank_evidence` 复算）——否则召回差异会混入
   "检索截断"，评测就不再只测路由；
2. "范围内无证据"的题必须在**所有集合**上零命中。

## 3. 运行器设计：真在哪、模拟在哪

**没有调用 corpus-api 应用、没有 DB、没有 HTTP、没有真实索引、没有模型。**
这不是偷懒：夹具要的是确定性与可复核，而 P5 的真实链条需要 PG + 对等进程；
本机做不到真环境时，把"检索执行"换成夹具预言机是唯一诚实的测法。
替换面被明确限制在一个点上：

| 环节 | 真实实现（评测直接调用） | 还是模拟 |
|---|---|---|
| ScopeManifest 校验与目标枚举 | `routing.targets` + 协调者 `_validate_manifest` | 真实 |
| 候选选择（fast 上限 / 穷查全量 / local_first） | 协调者 `_select_targets`（内部 `routing.candidates`） | 真实 |
| 根预算推导与记账 | 协调者 `_root_budget` + `routing.RootBudget` | 真实 |
| 探索许可门（deny 零外发） | 协调者 `_peer_probe_denial` | 真实 |
| Probe 构造与校验 | `probe.build_probe` / `validate_probe` | 真实 |
| 步骤图与计划合法性 | `routing.plan_steps` + `plans.validate_plan` | 真实 |
| 覆盖记录/合取/计数 | `coverage.new_entry/record/ledger` + `validate_ledger` | 真实 |
| **集合检索执行** | — | **`executor.FixtureExecutor`（关键词预言机）** |

预言机行为：按查询内容词与证据文本的**重叠数**稳定排序 `(-overlap, evidence_id)`，
`candidate_limit=8` 截断；零重叠不返回。它不读任何模型、不调任何服务。
"内容词"用一张小停用词表（`the/is/what/...`），否则 `the` 会把任意两段文本
凑成命中，"范围内无证据"就会假命中。**这不测检索质量** —— 构造自检保证
必需证据在候选上限内，所以两模式之间的召回差异只来自目标选择。

运行器不调用 `_execute_plan`（它需要 ORM Session/PeerDirectory），但逐行镜像
其两步覆盖记录：先 `record(entry, probe)`，再 `record(entry, None, state=执行结局)`；
计划装配也按 `create_plan` 的方式补 `fixed_inputs`/`probe_refs`。
`fast` 的候选上限 `8`、`probe_requests` 只对远端记账、被 deny 的目标零调用，
都与协调者 `federation_tasks.py` 当前行为一致。

## 4. 强制不变式（任何一条红 → 评测当场抛错，CLI 退出码非 0）

1. `fast` 永不 `complete`；
2. `complete` 要求枚举 sealed、全部适用目标 `succeeded`、`incomplete=0`；
3. 账本条目必须覆盖枚举出的每一个目标（独立于内核复算），分母一致；
4. `evidence_sufficiency=sufficient_by_policy` 当且仅当真的检索到了证据；
5. 返回的每条证据必须属于被探集合且在 scope 内；
6. 私有集合绝不允许被探、被返回；
7. 被 deny / 预算挡下 / 未选中的目标必须零执行调用（执行者调用日志审计）；
8. 装配出的计划必须通过 `plans.validate_plan`，账本必须通过
   `coverage.validate_ledger`。

第 2/5/6/7 条都有直接变异用例（§6）。注意第 3 条这个变体绕得过内核校验：
`validate_ledger` 只看计数，一个"complete + 计数全 0"但目标被 deny 的账本
它能放行 —— 逐目标复算专门抓这个，有负向用例钉着。

## 5. 基线运行结果（合成夹具）

**总体**（27 题 × 2 模式）：

| 轴 | fast | exhaustive_scope | 含义 |
|---|---|---|---|
| 计划目标数 | 216 | 324 | fast 每题 8 个候选；穷查 12 个全量 |
| 实际探测目标数 | 216 | 324 | 本题无一目标失败/拒绝 |
| 远端探测请求（预算 `requests`） | 162 | 270 | 每题 6 vs 10 个远端目标；P5 只对远端 Probe 记账 |
| 计划数据边（hop 代理） | 324 | 540 | 每个远端目标两条类型化数据边 |
| **绝对召回** | **63.3% (19/30)** | **100% (30/30)** | 标注必需证据的召回 |
| `retrieval_completeness` | partial=27 | **complete=27** | fast 永不 complete；穷查全部封存完成 |
| `evidence_sufficiency` | insufficient=4 | insufficient=3 | 3 题范围内无证据 + fast 另有 1 题（摘要隐藏）零命中 |
| 诚实性违规 | 0 | 0 | — |

**快速相对召回**（§14.3 口径：fast 命中数 / 穷查命中数）：**19/30 = 63.3%**。
计划建议的 95% 是真实语料的**质量目标**，这份夹具是故意构造的选举压力
（一半题目的必需证据在 fast 候选上限之外），63.3% 只说明该实现当前的选择
上限在压力网络里会漏，不能反推真实部署的召回。

**按类别**：

| 类别 | 题数 | fast 探测 | 穷查探测 | fast 召回 | 穷查召回 | 相对召回 |
|---|---|---|---|---|---|---|
| `b-only` | 3 | 24 | 36 | 100% | 100% | 100% |
| `conflicting-versions` | 3 | 24 | 36 | 83.3% | 100% | 83.3% |
| `cross-collection-split` | 3 | 24 | 36 | 50.0% | 100% | 50.0% |
| `local-similar-decoy` | 3 | 24 | 36 | 33.3% | 100% | 33.3% |
| `local-solvable` | 3 | 24 | 36 | 100% | 100% | 100% |
| `nearest-node-decoy` | 3 | 24 | 36 | 33.3% | 100% | 33.3% |
| `no-evidence-in-scope` | 3 | 24 | 36 | 不适用 | 不适用 | 不适用 |
| `private-decoy` | 3 | 24 | 36 | 100% | 100% | 100% |
| `summary-hidden` | 3 | 24 | 36 | 0% | 100% | 0% |

**逐题事实（全部可在报告 JSON 里逐条核对）**：

- `q-cross-02`：fast 拿到 `col-d-software` 的校验和、漏掉 `col-e-archive` 的
  保留规则（0.5 vs 1.0）；`q-cross-03` 两条都在 node-e/f，fast 全漏（0 vs 1）。
- `q-near-01/02`、`q-ldecoy-01/02`：fast 探到了相似诱饵却没有真目标；
  诱饵证据在 fast/穷查下各被召回 10 条（10/10），**诱饵被召回不等于必需证据
  被召回** —— 这正是两轴分开报告的意义。
- `summary-hidden` 三题 fast 全漏；内核摘要排序探针（`routing.candidates`
  带夹具 descriptors，非协调者实际路径）把它们的必需集合都排到第 12/12 位，
  远超候选上限 8。**fast 探到的不是必需证据但也不是零命中**：`q-hidden-02`
  命中了本地一条同样提到 humidity 的存储规范，`q-hidden-03` 命中了
  "use" 一词撞上的公开诱饵（别题的 decoy，在 `col-c-power`）—— 账本据实记
  `sufficient_by_policy`：它确实有证据，只是不是答案。报告把"有证据"与
  "必需证据召回"分成两轴，不做二次解释。只有 `q-hidden-01` 完全零命中。
- `conflicting-versions`：两版都拿到时报告 `conflict.observed=true`，
  并记录内核可直接表达的冲突轴（`coverage.sufficiency(..., conflicting=True)
  == "conflicting"`）。**协调者的账本目前不计算冲突轴**（§7 的发现 1）。
- `no-evidence-in-scope`：穷查 12/12 目标 `succeeded`、账本 `complete`，
  但证据集合为空、`evidence_sufficiency=insufficient` —— "检索覆盖完成"
  与"找到证据"是两回事，这个夹具把它演示出来了。
- `private-decoy`：私有集合既不进 scope，也不在 27×2 次运行的调用日志里出现；
  三条私有诱饵零召回。若哪天它出现，第 5/6 条不变式当场红。

## 6. 测试与变异确认

`eval` 套件 **66 passed**（原 45 + `test_routing_eval.py` 21 例）。除常规回归外，
每条不变式都有"注入变异 → 必须红"的负向用例：

| 用例 | 注入的变异 | 期望 |
|---|---|---|
| `test_fast_claiming_complete_is_rejected` | 内核账本对 fast 回 `complete` | `CoverageHonestyError` |
| `test_complete_claim_with_a_non_succeeded_target_is_rejected` | 账本回 `complete` + 计数洗成 0，但目标被 deny | 逐目标复算抓住 |
| `test_sufficiency_claim_without_evidence_is_rejected` | 零证据却写 `sufficient_by_policy` | 充足性复算抓住 |
| `test_removing_required_evidence_from_retrieval_drops_recall` | 从夹具副本删必需证据 | 召回 1.0 → 0.0，不被洗成命中 |
| `test_out_of_scope_evidence_from_the_executor_fails_the_run` | 执行者多塞私有证据 | 越界检查抓住 |
| `test_private_collection_is_refused_even_when_asked_directly` | 直接探私有集合 | 执行器拒绝 |
| `test_budget_blocked_targets_are_not_attempted` | 远端预算压到 0 | 远端零调用、本地照常 |
| `test_local_only_consent_denies_remote_targets_without_a_single_call` | `local_only` 许可 | 10 个远端目标 denied、调用日志只有本地 2 个 |
| `test_executor_without_an_audit_log_is_rejected` | 执行者不暴露调用日志 | 拒绝运行，无法证明"deny 零调用" |
| `test_runs_are_deterministic` | 同夹具跑两遍 | 逐字段相等 |
| `test_cli_exits_non_zero_when_an_invariant_breaks` | 运行器抛诚实性错误 | CLI 退码 2、不写报告 |

另按仓库惯例做了**人工变异确认**（改掉守卫那一行 → 目标用例必须红 →
从备份还原）。7 处变异全部被测到，还原后文件 sha256 与变异前一致，套件回绿：

| 变异 | 被哪条用例抓住 |
|---|---|
| 删掉 `complete` 的逐目标 `succeeded` 检查 | `…non_succeeded_target…` |
| 删掉"充足性 ⇔ 有证据"检查 | `…sufficiency_claim_without_evidence…` |
| 删掉返回证据的集合归属检查 | `…out_of_scope_evidence…` |
| 删掉执行者审计日志要求 | `…audit_log_is_rejected` |
| 删掉夹具"必需证据可召回"自检 | `…builder_rejects_evidence…` |
| 把召回集合交集改成空集 | `…removing_required_evidence…` |
| 删掉私有集合拒绝 | `…private_collection_is_refused…` |

Ruff（门禁口径 `F,B` + 既有 ignore）全绿。

## 7. 诚实的限制与发现

**未测量（报告 JSON 的 `not_measured` 逐条列出）**：

- **检索质量**：执行器是关键词预言机（重叠打分），不是真实索引，也不是
  真实向量/混合检索；召回差异只反映目标选择，不反映检索算法。
- **字节/带宽**：P5 协调者的 `used_budget.bytes` 恒为 0（需要数据面计数器），
  报告只报请求数与计划边数，不报字节。
- **答案质量**：不调用任何生成模型，引用有效性、主张支持度、答案正确性
  无从测量。
- **延迟/并发**：单进程确定性一遍，不含排队、冷缓存与并发效应。
- **递归联邦**：A→P→R、路径环路、有界缓存不在本夹具内（P6 目录展开的
  控制面验证见 `P6-DIRECTORY-EXPANSION-VALIDATION-v3.md`）。
- **多组织隔离与真实对等认证**：夹具是单组织 + 固定信任域。
- **提示注入**：计划 §14.1 的类别里有它，但本轮不调模型，测不了，故不编题。

**本次跑出来的两个值得记录的语义发现（评测不做代码修复，只如实报告）**：

1. **协调者账本没有冲突轴**：`coverage.sufficiency` 支持 `conflicting`，
   但 `_execute_plan` 的 `ledger(...)` 调用不传它，互相矛盾的两版证据会被
   记为 `sufficient_by_policy`。本轮报告把"观察到冲突"与"账本充分性"分开记，
   不替协调者宣布冲突已处理。
   **2026-09-15 已接入**：契约 `CoverageLedger.conflicts` 与两条 allOf（有记录就不许报
   `sufficient_by_policy`；报 `conflicting` 必须有记录）；协调者两路取矛盾 —— 规则一路
   （同一来源不同版本在同一定位上正文不同，`version_divergence`，只收自报来源 = 返回
   目标节点的条目）与生成标注一路（`CONFLICT: [n] [m]`，引用须落在本次证据域，
   `generation_reported`），都只能把"充分"压成"矛盾"、全部 `needs_review`。
   **`insufficient` / `unknown` 优先于 `conflicting`**：矛盾不能把"不足"改写成"矛盾"
   （那会藏掉不足信号、绕过生成闸 —— 提交前第五次验收复现），此时矛盾记录照样保留。
   本夹具的矛盾对是**不同集合的语义矛盾**，规则一路测不出；评测执行器不调模型，
   所以夹具上仍只记"观察到冲突"，协调者路径的覆盖在 corpus-api 的回归用例里。
2. **fast 的摘要排序当前没有被用上**：`_select_targets` 给
   `routing.candidates` 传的 descriptors 是空列表，所以真实 fast 实际是
   "本地优先 + 身份顺序 + 上限 8"；报告另记内核摘要排序的位次（三个
   `summary-hidden` 问题都排到第 12/12），但明确标注那不是协调者的实际
   路径。该探针的打分对集合主题做**子串**匹配（`is` 能落进 `misc`），
   夹具的隐藏问题用词避开了这类硬碰撞；位次本身只作记录，不单独解读。
   要按 §7.2 用摘要排序，需要协调者把集合目录摘要接进规划 —— 这不在
   本次评测的写入范围内。

## 8. 边界

本次只写入 `eval/routing/**`、`eval/tests/test_routing_eval.py`、
`eval/reports/routing-*.json` 与本文件；没有 commit，没有改动（也没有 revert）
其他 agent 并行编辑的 federation/cache 文件与测试。
本记录是本次作者的自验，不替代 commit 前的独立验收。
