# 版本兼容矩阵（v3）

> 2026-09-13。范围：P7 兼容性切片。判据全部来自本工作树里已跑过的测试；
> **没有跑过的平台/路径一律标 ⬜ 未验证**，不把"代码读起来应该支持"写成已验证。
> 与本文相关的守卫：`scripts/check_federation_contracts.py`（7 份 schema、
> 71 个夹具）、`services/control-api/internal/discovery/protocol_versions_test.go`、
> `packages/client-runtime/test/http.test.mjs`、
> `python/ddp_core/tests/test_compatibility.py`。

## 0. 怎么读

| 记号 | 含义 |
|---|---|
| ✅ 接受 | 有正向测试或真实路径 |
| 🚫 拒绝 | 有否定测试，且拒绝码被断言 |
| ⚠️ 降级 | 接受但降级可见（落在字段/原因上） |
| ⬜ 未验证 | 本机跑不了或本切片没跑；**不要当绿** |
| ⛔ 出范围 | 计划明确不做，或没有产物可验 |

版本兼容的总策略是 **Fail Closed**：只有"确切认识的那个版本"会被执行；
未知必填字段、未知版本、未声明的必需能力都在产生副作用之前拒绝。理由是
T85 那一族事故 —— 收下看不懂的东西再"尽力执行"，最后表现成静默降级。

## 1. 协议版本一览

| 层 | 版本 / 标识 | 定义处 | 兼容策略 |
|---|---|---|---|
| UI（Web） | `0.1.0`（package.json） | `apps/web/package.json` | 只消费下面这些契约版本 |
| Desktop（Electron 宿主） | `0.1.0`（package.json） | `apps/desktop/package.json` | 与本地运行时握手 `ddp-client/1`；Windows 宿主 Tier A 直连中心、Tier C 经 WSL2 桥，握手协议相同（真机 ⬜，见 §4） |
| client-runtime | `0.1.0`，协议 `ddp-client/1` | `packages/client-runtime/` | 未知协议版本 / 缺必需能力 → `protocol_incompatible`，**在 snapshot 之前**；Windows 宿主复用同一实现（真机 ⬜） |
| 本地运行时 handshake | `ddp-client/1` + 能力清单 | `ddp_local/runtime.py:client_handshake`、`/api/v1/client/handshake` | 与 client-runtime 同一份判据；Windows 本地模式由 WSL2 内的同一运行时提供（垫片 + 真 tarball 在 Linux 全链，真 WSL ⬜） |
| 节点握手（center） | `ddp-discovery/1`、`ddp-client/1` | `control-api handleFederationNode` | 公开广播；注册校验必须含 `ddp-discovery/1` |
| 节点证明 | `ddp-node-proof/1` | `client-runtime-format.md`、`discovery/proof.go` | nonce + 端点 + 60s 有效期，缺一不接受 |
| Center API | OpenAPI `1.0.0`（control/discovery/scope/collections/bundle/wiki/federation-tasks）、gateway `1.1.0` | `packages/contracts/openapi/` | 冻结契约：只许向后兼容新增 |
| 联邦任务 | `federation-tasks-v1.yaml` 1.0.0，19 端点 | 同上 | 路由守卫双向核对 |
| Bundle | `ddp-bundle/1` | `ddp-bundle/v1.json` | 非 `1` 或 `required_features != []` → `bundle_schema_unsupported` |
| Bundle layout | `ddp-bundle-layout/1` + `ddp-layout/1` | `ddp-bundle/v1.json` | 非这两个值拒绝 |
| Evidence | `ddp-evidence/1#FederatedEvidence` / `#EvidenceSet` / `#FederatedAnswer` / `#DeliveryReceipt` | `ddp-evidence/v1.json` | `schema` 常量不匹配 → 拒收 |
| TaskSpec / Probe | `ddp-task-probe/1#TaskSpec`（`protocol: ddp-task/1`）、`ddp-probe/1` | `ddp-task-probe/v1.json` | 旧/新版本整体拒绝，不做字段嗅探 |
| 许可 | `ddp-task-probe/1#ExplorationConsent`、`ddp-plan-admission/1#ExecutionConsent` | 同上 / `ddp-plan-admission/v1.json` | 未知字段 → `egress_denied` / `protocol_incompatible` |
| 接单 | `ddp-plan-admission/1#AdmissionReceipt` | `ddp-plan-admission/v1.json` | 未知字段或旧 schema → `protocol_incompatible` |
| 覆盖 | `ddp-scope-coverage/1#ScopeManifest` / `CoverageLedger` | `ddp-scope-coverage/v1.json` | 摘要/枚举校验失败 → `plan_changed` / `scope_expired` |
| Wiki 生成协议 | `ddp-wiki-generation/5`（内部） | `ddp_core/application/wiki.py` | 旧生成协议不落库（attempt 记录原协议） |
| 索引 | `index_revision`（不透明字符串）+ `EMBEDDING_DIM=1024` | `corpus-api/CONFIG.md` | 修订不一致 → 探测不可复用（重新探测） |
| DB | corpus alembic head `0030`；control 迁移 `0010` | `database/` | 版本不匹配由迁移单写者拒绝 |

## 2. 握手与拒绝行为（逐条有测试）

| 行为 | 结果 | 证据 |
|---|---|---|
| 客户端握手返回未知 `protocol_version` | 连接 blocked，`protocol_incompatible`，**不发 snapshot** | `packages/client-runtime/test/http.test.mjs`（本轮新增）；桌面 `runtime.mjs` 同判据由 `apps/desktop/test/boundaries.test.mjs`（本轮新增）覆盖 |
| 握手能力清单缺 `client.snapshot/events/receipt` 任一 | 同上，且在应用权威数据之前 | 同上 |
| 想用 chat-only 服务冒充完整 corpus API | 同上 | 既有用例 `a chat-only service cannot impersonate the complete corpus API` |
| 节点注册描述符不含 `ddp-discovery/1` | `Validate` 报"unsupported discovery protocol" | `internal/discovery/protocol_versions_test.go`（本轮新增） |
| 节点描述符带未知字段 | JSON 解码失败（`DisallowUnknownFields`），不静默丢弃 | 同上 |
| 公开 `/api/v1/federation/node` 广播 | `protocol_versions=["ddp-discovery/1","ddp-client/1"]` | `internal/api/discovery_handshake_compat_test.go`（本轮新增） |
| 旧/未知 Bundle 版本或非空 `required_features` | `bundle_schema_unsupported`，整包拒收 | `python/ddp_core/tests/test_compatibility.py`（本轮新增）+ 既有 `test_t12_unknown_required_schema` |
| Bundle 内证据是旧 schema 或含未知字段 | `bundle_schema_unsupported` | 同上 |
| 旧 TaskSpec 协议（`ddp-task/0`） | `invalid_plan`（"unsupported task schema"），不产生可执行计划 | 同上；夹具 `invalid/task-spec-old-protocol.json` |
| 旧 Probe / Admission 版本或未知字段 | `protocol_incompatible` | 同上；夹具 `invalid/evidence-old-schema.json` 等 |
| Probe 有 `internal_limits` 却写 `succeeded` | `partial_retrieval`（T85 的机器版） | 同上 + 既有守卫 |
| 未知/旧证据 schema 出现在 API 出口 | `ddp-evidence/v1.json` 的 `const` 拒绝 | `check_federation_contracts.py` 的夹具校验 |

## 3. 降级（接受但必须可见）

| 场景 | 降级表现 | 证据 |
|---|---|---|
| 向量/embedding 不可用 | `corpus.retrieve` 的 `profile=keyword_only`，检索结果带 `degraded` | `capabilities.py` + `test_capabilities.py` |
| 视觉不可用（compile） | `doc.compile` 的 `profile=text_only`，`compile_degraded` 落库 | `test_compilation.py` |
| 生成模型未就绪 | 计划无 `answer` 步 / `answer_reason=local_model_missing` | `test_federation_answer.py` |
| 远端正文缺失/越界 | `evidence_excerpt_unavailable` / `excerpt_over_contract_bound`，不生成 | 同上（F4/N5/N6） |
| 模型输出无引用/引用越界 | `unsupported_generation`，绑定为空，证据保留 | `test_federation_answer.py` + `test_federation_injection.py`（本轮新增） |
| 关键词检索路 | 中文分词软依赖缺失时 `backend()` 如实上报 | `test_tokenize*` |

## 4. ⬜ 未验证的平台格

以下格子**没有**被本切片验证，切换前不要当成兼容：

- ⬜ **真实 GPU 生成**（vLLM / DeepSeek-OCR-2）：answer 委托的真实模型输出、
  两节点跨节点生成、vision 核对阈值重标定。本机无 N 卡。
- ⬜ **mineru / TEI / rerank 容器**：解析与向量索引的真实上游版本兼容。
- **Electron 打包产物与本地模型的自动安装**：Linux 目录包/Arch 包已有实测
  （`RELEASE-MANUAL-v3.md`）；`P3-LOCAL-MODEL-VALIDATION-v3.md` 已记录 CPU 路径，
  但打包后行为未验。Windows 侧当前状态（macOS 未动，仍 ⬜）：

  | 平台 / 模式 | 状态 | 证据 |
  |---|---|---|
  | Windows 桌面 Tier A（远程） | 代码路径 + 宿主单测；⬜ 真机 | `WINDOWS-AC-VALIDATION-v1.md` §4 |
  | Windows 桌面 Tier C（WSL2 本地） | 垫片测试 + 真 W3 tarball 在 Linux 全链；⬜ 真 WSL2 | `WINDOWS-AC-VALIDATION-v1.md` §5 |
  | Windows 原生本地运行时 | ⛔ 明确不做 | `WINDOWS-AC-PLAN-v1.md`「明确不做」 |
  | Windows ARM64 | ⛔ 无可验产物（映射/拒绝分支在 `scripts/update_check.py`） | 同上 |
  | macOS | ⬜ 未构建 | `RELEASE-MANUAL-v3.md` §8.3 |
- ⬜ **P6 递归目录 / 根预算层级 / 有界缓存**：`plan` 尚在别人手上，没有可测版本面。
- ⬜ **生产快照上的迁移与旧系统对拍**：本矩阵只保证库内迁移链能跑
  （见 `RECOVERY-DRILL-v3.md`），没有旧系统录制响应可对拍。
- ⬜ **跨语言摘要**只对 Go `FinalizeScope` 的 `manifest_digest` 有冻结值
  （`test_go_produced_manifest_digest_is_accepted`）；其它跨语言字段未对拍。
- ⬜ **旧客户端（真的旧二进制）连新中心**：本切片只测了"缺能力/版本不符的
  握手被拒"，没有旧版客户端产物可跑。

## 5. 已知缺口（本轮发现，未修）

- `source_revoked` 只有状态映射（`federation._STATUS_BY_CODE`），**没有任何
  生产者**（`grep source_revoked services python` 只命中该映射行）。当前
  "来源被撤"由集合目录的 `catalog_snapshot_invalid` + `revoked_collection_ids`
  表达。要不要让联邦端点也产出 `source_revoked` 是产品决定。
- ~~`GET /api/v1/federation/probes/{id}` 只做**组织级**过滤；同组织内其它用户
  能读到别人的探测回执。~~ **已失效（2026-09-14 更正）**：`federation.get_probe`
  早已按 `organization_id + actor_id == acting_actor(actor)` 双重过滤（越权与
  不存在同形 404）；同轮复核把 `require_execution` 也收紧到受理行的 actor 绑定
  （原查询只比 organization_id，见 §6）。

## 6. 第二轮独立复核修复（2026-09-14）

- **`federation_error` 新增 `task_cancelled`**（`enums.yaml` 三语言生成物 +
  `federation-tasks-v1.yaml` 的 `FederatedError` 枚举）：对已取消任务调用
  `resume` / `POST /tasks` 返回 409 `task_cancelled`。这是向后兼容的**新增
  错误码**（冻结契约只许向后兼容新增）；老客户端不认识它时按通用 409 处理，
  不会误判成成功。
- **执行读取/取消收紧到调用者绑定**：`require_execution` 现在 join 受理行并按
  `admission.actor_id == acting_actor(actor)` 过滤（组织管理员仍可复核），
  越权与不存在同形 404。这是对冻结 API 的**收紧**（原先同组织可见），
  协调者用受理时的原始 actor 轮询不受影响；`python/ddp_local` 的
  `center_ref` 派发路径不变。
- **清扫新增第三支**（队列任务已死的 queued 执行落 `failed/queue_task_failed`），
  不改变任何协议形状；`ExecutionStatus.state` 仍是 `task_status` 枚举。
- 复跑：`./scripts/check.sh` **29/29**；corpus-api **749 passed / 17 skipped**；
  real-PG `check_federation_pg.sh` **12 + 5 passed**；ddp_local **108 passed**。
