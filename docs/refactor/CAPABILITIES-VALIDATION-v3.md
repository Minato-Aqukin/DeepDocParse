# capability 生产者自验记录（2026-09-12）

当前状态：`GET /v1/capabilities`（model-gateway）与 `GET /internal/capabilities`
（corpus-api）两个生产者的**语义修复 + 负向回归**自验通过。**这不是 P4/P5 完成，
也不是 commit 前的独立验收**，更**不代表任何真实模型跑过**。执行权威是工作区上级
`DeepDocParse_桌面与可验证联邦路由升级计划_v3.md`（§5.2 / §5.5 / §6.4）；
消费侧契约见 `packages/contracts/ddp/discovery-control-format.md` 与
`packages/contracts/schemas/ddp-discovery/v1.json`。

改动只落在四个实现/测试文件 + `packages/contracts/openapi/gateway-v1.yaml` 的
`/v1/capabilities` 一节。control 侧的 discovery/client/local/UI 未触碰。

## 一、修掉的五个语义缺陷

审查从一个问题开始：**这份清单声明的东西，这台机器真的会做吗？**
五处答案是「不会」，而五处的失败方式都是**静默的**——清单是绿的，任务派过来才崩，
或者更糟：不崩，只是答案是假的。

### D-1　网关宣称它根本不执行的 operation

改前 `profiles` 里有 `doc.compile` / `rag.answer.cited` / `wiki.pages` /
`corpus.retrieve` 四条，而 model-gateway 只有 `/v1/parse`、`/v1/chat/completions`、
`/v1/embeddings`、`/v1/extract`、`/v1/rerank` 五个端点——编译在
`corpus-api/ddp_corpus/compilation.py`，带出处的问答在 `qa.py`，Wiki 在
`knowledge.py`，跨文档检索在 `routers/search.py`。planner 读到这份清单会把编译/问答
派到一个只会转发 chat 的进程上。

改后网关只声明它真有端点的三条（`doc.parse` / `extract.fields` / `rerank`），
模型通道另立 `model_channels`，**并明说它不是能力声明**。

### D-2　`doc.compile` 只借 vision 就宣称整个编译可用

改前的规则是 `("doc.compile", "vqa_models", "vision", None)`——只要有一个带
`vision` 的条目健康就报 ready，**且故意不排除 `no_instruct`**（原注释写着
"OCR 模型正是干这个的"）。

真实的编译视觉步骤（`compilation.py::_understand`）发的是一句
「理解这一个文档视觉原子，**只输出 JSON**」的**指令**。OCR 专用模型不听指令，
它会继续抄字，`_description()` 解析不出 JSON，落成 `vision_invalid_output`。
所以编译要的是 **vision + instruct 两个词**，而随仓库发布的 `models.yaml` 里
恰恰没有这种条目（默认 `deepseek-ocr-2` 是 `[vision, no_instruct]`，
`qwen3-4b-instruct` 是纯文本）。

改后由 corpus 组合，并且**不用 readiness 说这件事**：编译在没有视觉时仍然产出
（打 `vision_unavailable` 降级继续），报 unhealthy 是另一个方向的谎。
降级落在 `profile` 上：`with_vision` / `text_only`。这是契约里唯一能承载它的字段
（`CapabilityProfile` 是 `additionalProperties: false`，没有 degraded 字段）。

### D-3　把 embedding 的健康当成「SQL 索引可检索」

改前 `("corpus.retrieve", "embedding_models", "dense", None)`：TEI 活着就报
`corpus.retrieve: ready`。两个方向都错——

- **不充分**：检索真正要读的是本层 Postgres 里的 `chunks`（`ddp_core/search.py`
  的 `PgVectorIndex`）。库不通、迁移没跑，TEI 再健康也检索不了；
- **不必要**：embedding 挂了检索照样跑关键词路，`search.py` 打
  `embedding_unavailable`（M4a 那次静默退回 BM25 的教训就在这条上）。

改后 `corpus.retrieve` 的 readiness 只来自本层的 `observe_store()`——
**打的是 `chunks` 而不是 `SELECT 1`**：库活着但迁移没跑时 `SELECT 1` 照样成功，
而检索会 500。向量路可用与否落在 `profile`：`hybrid` / `keyword_only`。

### D-4　`/v1/models` 探测只看状态码，不看服务的是不是那个模型

`/v1/models` 返回 200 只说明运行时活着。vLLM 只认自己 `--served-model-name` 的
那一个 id，对不上时**每一次真实请求都是 404 `model_not_found`**——
`infra/autodl/README.md` 的排障表里已经有这一条，注册表注释里也写着
「名字必须与运行时的 `--served-model-name` 一致」。

改后 OpenAI 协议的条目必须在 `data[].id` 里找到**本网关真会请求的那个 id**，
而这个 id 按调用路径取：`vlm-ocr` 引擎填 `options.model`（`services/engines.py`），
chat/embedding/rerank 反代填条目名，`adapter` 优先（`services/extraction.py`）。
拿条目名一把梭会两个方向都错。

顺带修的同类问题：

- **选路不看健康**。真实选路（`Registry.default_of` / `_pick_chat`）只看 default
  标记与能力词，改前的"探完候选取第一个 ready 的"会在默认条目死掉、兄弟条目健康时
  报 ready，而请求照样发给死掉的那个。
- **任务存储是硬依赖**。`/v1/parse` 与 `/v1/extract` 受理第一步是读 Redis 在途水位，
  Redis 不通时模型再健康也受理不了。改前只探模型。
- **`instruct` 的判据错了**。改前按 `"instruct" in capabilities` 筛，而真实选路
  （`_pick_chat`）的判据是**没写 `no_instruct`**——段名会把没写 capabilities 的
  vqa 条目补成 `[vision]`，抽取平面照样会挑中它们。按原判据会把这些部署报成
  「没有抽取能力」，而 `/v1/extract` 明明在用。**方向相反的谎也是谎。**
- **`limits` 报错了平面**。`extract.fields` 用的是 chat 反代的信号量
  `VQA_MAX_CONCURRENCY`，而抽取链直连模型端点、走的是自己的
  `EXTRACT_CONCURRENCY`；`doc.parse` 把 `PARSE_QUEUE_MAX`（在途水位上限）
  报成了 `max_concurrency`（并发度）。后者契约里没有对应字段，**宁可不报**。

### D-5　上游的过期观测 / 未知取值 / 越界字段被透传成 ready

改前 corpus 按白名单原样复制网关的 profile，包括 `readiness`、`observed_at`、
`valid_until`。于是：

- 网关自己说 `capability_status: unknown` 时，它的 profiles 照样被当成 observed
  转出去（把"不知道"洗成"知道"）；
- 上游一条过期的观测被原样转出——§5.5 明写「过期记录不是当前能力证明」；
- 上游给 `readiness: "healthy"` 这种契约外取值时原样转出；
- 上游给 `limits: {max_concurrency: -1}` 这种越界值时原样转出，而 control 对整份
  观测是**全有全无**（一条不合法就整份判 unknown）——一个坏字段会把整个节点的
  能力清单连坐成"状态未知"。

改后：上游 envelope 必须自称 `observed`；readiness 必须在 `enums.yaml` 的
`capability_readiness` 里（`configured` 也不算证据）；`valid_until` 过期或
`observed_at` 在未来（容忍 5s 时钟偏移）一律按 unknown；越界/坏类型的
`limits` / `engine_versions` 丢掉而不是转发；**时间戳永远是本层自己的观测时刻**。

## 二、两个生产者现在的契约

### model-gateway `GET /v1/capabilities`（已写进 `openapi/gateway-v1.yaml`）

```
{ capability_status: observed|unknown,
  profiles: [CapabilityProfile]        // 只含 doc.parse / extract.fields / rerank
  model_channels: [{channel, model, profile, default, readiness,
                    supports{instruct|vision|dense|rerank}, limits?, observed_at, valid_until}] }
```

`supports` 是**派生布尔**而不是注册表原词：`no_instruct` 该压过 `instruct` 这条
判断留在网关（它拥有注册表语义），消费方不必也不该重新实现一遍能力词规则。

### corpus-api `GET /internal/capabilities`（本仓库没有对应 openapi 文件，契约记在这里）

响应 `{profiles, capability_status}`，profile **不含 `node_id`**（control 注入）、
`accepting_admissions` 恒 false。每条 readiness 是各依赖的合并，**最差者胜**；
可选依赖只改 `profile` 名字，绝不把 readiness 抬回 ready。

| operation | 硬依赖 | profile 取值 | 不列出的条件 |
|---|---|---|---|
| `doc.parse` | 网关 `doc.parse` + 本层检索库 | 网关给的引擎名 | 网关没声明 |
| `doc.compile` | 本层检索库 | `with_vision` / `text_only` | 从不（本层实现） |
| `rag.answer.cited` | 遵指令 chat + 检索库 | `verified` / `unverified` | 无任何 chat 模型 |
| `extract.fields` | 遵指令 chat + 检索库 | `verified` / `unverified` | 无任何 chat 模型 |
| `wiki.pages` | 遵指令 chat + 检索库 | 无 | 无任何 chat 模型 |
| `corpus.retrieve` | 本层检索库 | `hybrid` / `keyword_only` | 从不（本层实现） |
| `rerank` | `RERANK_ENABLED` + rerank 通道 | 模型名 | 开关关着或网关无模型 |

三条分界，写在这里是因为它们最容易被写反：

1. **"没有这个能力" ≠ "能力不可用"。** 一个 chat 模型都没有 -> 不列
   （control 侧对应 `capability_unsupported`）；有模型但它是 OCR 专用 ->
   列出来标 `unhealthy`（我们确实观测到了，只是它干不了这活）。
2. **"观测到的否定" ≠ "没有观测"。** 独立配置的 chat/embedding/rerank 端点
   （`CHAT_URL` 等，ADR #17）观测不到 -> `unknown`；观测到了但不匹配 -> `unhealthy`。
3. **网关整体不可达 -> 整份 `([], "unknown")`。** 与 control 对"上游没接线"的口径
   一致：模型侧唯一的证人不在场时不发半份清单。**这是一个取舍**——本层的检索
   其实仍然可用，写在这里是为了下次有人想改的时候知道当初为什么这么选。

## 三、验证结果

命令用绝对路径 venv `/home/minatoaqukin/Projects/CSIE/DeepDocParse/.venv/bin/python`，
各包在**自己的目录**下跑（rootdir 不对会让 async 用例集体报 fixture 错）。

| 项目 | 结果与证据 |
| --- | --- |
| 目标测试（改前基线） | 网关 14 passed / 语料 11 passed —— 全绿，而**绿的是错的语义**：其中两条正在钉着 D-1/D-2 的错误行为（「OCR 模型能编译」「embedding 健康即可检索」各一条断言）。 |
| 网关 capability 用例 | PASS：28 passed（`services/model-gateway`，`pytest tests/test_capabilities.py`）。 |
| 网关全套 | PASS：177 passed, 6 skipped。 |
| 语料 capability 用例 | PASS：41 passed（`services/corpus-api`，改前 11）。 |
| 语料全套 | PASS：494 passed。 |
| 契约守卫 | PASS：`scripts/check_contract.py` —— 13 个端点，契约与网关一致。 |
| 枚举用法守卫 | PASS：`scripts/check_enum_usage.py` —— 111 处用法全部在契约内。 |
| 联邦契约守卫 | PASS：`scripts/check_federation_contracts.py` —— 7 份 schema / 68 夹具 / 53 定义。 |
| 仓库架构守卫 | PASS：仓库根 `pytest -q` 18 passed。 |
| 契约形状（schema） | PASS：两侧产出的每条 profile 都按 `generated/schemas-resolved.json` 的 `ddp-discovery/1#CapabilityProfile` 校验（补一个假 `node_id` 后），schema 是 `additionalProperties: false`，顺带钉住"不得自带 node_id"与"不得发明契约外字段"。 |
| 响应体 vs openapi | PASS：网关整份响应按 `gateway-v1.yaml` 的 200 schema 校验（envelope 也加了 `additionalProperties: false`）。 |
| readiness 取值 | PASS：两侧产出的 readiness ⊆ `ddp_contracts.enums.CAPABILITY_READINESS_VALUES`（生成物，来源是 `enums.yaml`）。 |
| control 侧时间规则 | PASS（等价断言，非真跑 Go）：产出满足 `observed_at <= now < valid_until` 且不沿用上游时刻——control 的 `ProjectProfiles` 对这两条是整份连坐。 |

### 变异确认（17 条）

守卫必须做变异确认，而**写错的变异看起来就是"守卫是假的"**——这次又撞了一次，
如实记在这里。

| 变异 | 结果 |
|---|---|
| 网关：`_serves` 恒 True（跳过 model id 核对） | 红（`test_runtime_serving_another_model_is_not_ready`） |
| 网关：解析/抽取不看任务存储 | 红（`test_task_store_down_makes_queued_planes_unhealthy`） |
| 网关：按 `"instruct" in caps` 筛 | 红（`test_no_instruct_beats_instruct_when_both_declared`） |
| 网关：把 `corpus.retrieve` 加回 `_OPERATIONS` | 红（`test_only_operations_the_gateway_executes_are_profiles`） |
| 网关：探完候选取第一个 ready 的（旧写法） | 红（`test_healthy_sibling_does_not_rescue_a_dead_default`） |
| 网关：响应多一个契约外字段 `gpu: true` | 红（`test_response_matches_the_openapi_contract`）——**第一次是绿的**，见下 |
| 语料：检索就绪度改看 embedding | 红（`test_retrieval_readiness_comes_from_the_store_not_the_embedder`） |
| 语料：编译只要 vision | 红（`test_compile_does_not_claim_full_compile_from_a_vision_only_model`） |
| 语料：不看上游观测是否过期 | 红（`test_expired_upstream_observation_is_not_relayed_as_ready`） |
| 语料：不看上游的 `capability_status` | 红（`test_gateway_saying_unknown_is_not_laundered_into_observed`） |
| 语料：安静透传上游 readiness | 红（`test_unknown_upstream_readiness_becomes_unknown` 的 4 个参数） |
| 语料：指定的 chat 模型缺失时退回 default | 红（`test_configured_chat_model_must_match_a_gateway_channel`） |
| 语料：不过滤畸形通道项 | 红（`test_malformed_channel_entries_do_not_break_the_endpoint`） |
| 语料：沿用上游的观测时刻 | 红（4 条，含 `test_output_satisfies_the_control_side_temporal_rules`） |
| 语料：合并表漏掉 `draining` | 红（`test_draining_upstream_is_merged_not_crashed`） |
| 语料：非字符串 `profile` 名照样 `str()` 发出去 | 红（`test_non_string_upstream_profile_name_is_dropped`） |
| 语料：`observe_store` 换成 `SELECT 1` | 由 `test_store_probe_reads_the_table_retrieval_reads` 直接覆盖：drop 掉 `chunks` 后 `SELECT 1` 仍然通，探针必须报 unhealthy |

两条要单独说：

- **"任取一个健康条目"第一次写错了变异**：把 `default_of(usable)` 换成
  `next(iter(usable.items()))`，而测试数据里默认条目恰好排第一 —— **配置根本没变**，
  守卫当然绿。改成真正的旧写法（探完候选取第一个 ready 的）之后才红。
- **`gpu: true` 那条第一次是真绿**：openapi 的 envelope 当时没有
  `additionalProperties: false`，我的测试 docstring 却写着"契约外字段会红" ——
  那句话当时是假的。给 envelope 补上约束后才红。**假的不是守卫，是我写的说明**，
  两种都要修。

## 四、限制（这份记录**不**证明什么）

1. **没有任何真实模型跑过。** 全部上游是 respx mock，本机无 N 卡。
   "OCR 专用模型不能做编译/抽值"这条判断来自注册表能力词与既有真机记录
   （`infra/autodl/README.md`、`services/extraction.py` 的注释），**不是本轮实测**。
2. **没有真实 E2E。** 没起 compose、没跑 `scripts/e2e_stack.py`、没连真 PG/MinIO/Redis；
   语料侧的检索库探针是在 SQLite in-memory 上验的，PG 上 `chunks` 存在但
   pgvector 索引缺失的形态**没有覆盖**。
3. **没跑 control 侧的 Go 测试**，也没有把本层的真实响应喂给 `ProjectProfiles`
   跑一遍。control 的规则是用等价的 Python 断言 + 共享 JSON schema 复现的，
   这两者可能漂开。跨服务的真实握手要等 control 那边的改动落定后一起验。
4. **节点级能力 ≠ 集合级可检索。** `observe_store()` 只证明检索库现在答得出一次
   查询，**不证明**某个集合的索引建完了、是新的。那是 CollectionDescriptor 的
   `index_revision` 与证据探测（§5.2 / §6.4）的事，本轮没有实现。
5. **`accepting_admissions` 恒 false**，队列水位不进 readiness——受理器还没有
   （I06）。所以这份清单只回答"干得了吗"，不回答"现在接不接"。
6. **`corpus.locate` 两侧都不产出**，因为没有实现。不列 ≠ 声明不支持，
   consumer 侧应按"未知/需预检"处理。
7. 网关不可达时整份报 unknown 是取舍（§二·3），不是唯一正确解。
8. 本记录由本次作者自验，**没有 commit、没有 push**，不替代提交前的独立 agent 验收。
