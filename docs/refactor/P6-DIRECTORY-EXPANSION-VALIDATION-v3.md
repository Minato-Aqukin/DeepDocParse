# P6 递归目录展开与对等目录读自验（2026-09-13）

当前状态：**A→P→B/C 的目录展开、预算/环路/去重与对等目录读切片自验通过**。
执行权威是工作区 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md` §5.3/§5.4 与 P6；
契约形状见 `discovery-control-format.md`、`scope-control-format.md` 与三份
OpenAPI（`discovery-v1.yaml` peer 成员读、`collections-v1.yaml` peer 目录读、
`scope-v1.yaml` 展开预算）。**这不是 commit 前的独立验收，也不代表 P6 全部出口。**

## 实现范围

### 对等目录读（控制面入口，Fail Closed）

- `GET /api/v1/federation/members`：`X-DDP-Peer-Token` 与
  `FEDERATION_PEER_TOKEN` constant-time 比较；**留空一律 401**。
  只有 approved 且 `visible_to_org` 的直接成员进入持久分页快照；hidden、pending、
  撤销成员一个都不出现；route、公钥、组织共享名单不外发。
  `X-DDP-Target-Node` 指名别的节点返回 409。续页必须带 `snapshot_id`+`cursor`，
  改 `limit` 返回 400，终止游标返回空 complete 页。
- `GET /api/v1/federation/collections`：控制面用服务身份把请求代理到
  corpus 新增的 `GET /internal/federation/published-collections`。该 corpus 路由
  **只接受服务 actor**，scope 与 caller digest 由服务端常量固定、origin 取自本节点
  持久身份；`bundle_node_id` 为空直接 503，绝不发出来源缺失的描述符。
  控制面在返回 peer 前逐条校验 `origin_node_id == 本节点`，重复 corpus 的
  `caller_scope_hash` / `scope_id` / `index_readiness` 一律剥离。
  既有 `/internal/federation/collections`（caller-scoped）路径与语义未改。

### 出站展开（scope collector）

每个 approved 直接成员：本地目录有 `federation` endpoint、且在
`FEDERATION_PEERS` 中登记了凭据时才会被联系；联系地址用**登记配置里的
endpoint**，不信任描述符里的地址。展开算法：

1. 冻结成员快照经 `AuthorizedSnapshotMembers` 读出（撤销/可见性 overlay 生效），
   revoked 成员绝不联系。
2. `pullMembers` 跟随 peer 快照到独立终止页；`pullCatalog` 跟随 peer 目录页到
   终止页。任一步失败都保留**已观察到的成员/目标**，并把该子域记入
   `unexpanded_subtrees`，理由为 `timeout` / `denied` / `enumeration_unsupported`
   / `budget_exhausted` / `unknown`。
3. 每个查询过的目录写 `registry_revision_vector`（`directory_ref` = members /
   collections + `snapshot_ref`），每个成员目录写 `child_manifests`
   （`scope_ref` + `sealed`/`partial`）。
4. 队列 BFS + visited/scheduled 集合：环路（P↔B、P 列 A）与重复路径只展开一次；
   目标按 `(origin_node_id, collection_id, operation)` 去重。
5. `max_discovery_requests`（默认 64，出站 peer 请求数）与
   `max_remote_members`（默认 32，联系的远端节点数）严格计数；耗尽即停止并
   标记 `partial` + `budget_exhausted`，已观察目标保留。
6. 发现来源（B 经 P 到达）没有本地登记。`scope_remote_sources` 记录其授权根
   （直接成员 P）；读取 target 时 P 仍 approved+可见才可用，P 被撤销则整棵
   已发现子树一并 `revoked`，不伪造本地登记也不误判为立即撤销。

### 持久化（control 0010）

`control.scope_manifests.child_manifests JSONB`（从 manifest 回填，旧 scope
可读）、`control.member_snapshots.page_size`（peer 快照续页校验，用户快照保留
默认 50）、`control.scope_remote_sources(scope_id, origin_node_id, via_node_id)`。
manifest JSON 仍是响应的唯一来源；两份迁移副本字节一致。

### 凭据与出站安全

`FEDERATION_PEERS` JSON：`{node_id: {endpoint, service_token, peer_token}}`，
启动即解析，坏配置拒绝启动。endpoint 必须 HTTPS、无 userinfo/query/fragment；
`FEDERATION_ALLOW_LOOPBACK=false` 默认，只有显式打开且 host 是字面
`127.0.0.1`/`[::1]` 才放行（`localhost` 不给过）。出站客户端
`Proxy: nil`、`CheckRedirect` 直接返回 `ErrUseLastResponse`、响应字节有上限，
去重、重定向、跨 host 都不会把凭据交给未登记节点。

## 验证环境与结果

只使用一次性容器 `ddp-v3-p6-pg`（`pgvector/pgvector:pg16`，回环端口 15450），
数据库 `deepdocparse`，跑完 control 0001–0010；未连接开发库。Go 为
`/home/minatoaqukin/.local/opt/go/bin/go`（go1.27）。连接串只通过
`CONTROL_TEST_DATABASE_URL` 环境变量传入，文档不保存口令。

| 项目 | 结果与证据 |
| --- | --- |
| 控制面全量（真 PG） | PASS：`CONTROL_TEST_DATABASE_URL=… go test ./... -count=1`、`go vet ./...`、`gofmt -l .` 全绿。 |
| A→P→B 展开与落库 | PASS：`TestScopeCreateExpandsRemoteDirectoryAndPersistsChildManifests`。目标含 P/p-col、B/b-col 且 origin 正确；registry vector 含 local/P/B 的 members+collections；child_manifests 有 P、B 且 `sealed`；GET scope 与 `scope_manifests.child_manifests` 列都回读得到；B 无本地登记而其 target 首次读取为 `not_attempted`；撤销 P 后 P/B 目标全变 `revoked`，分母与 digest 不变。 |
| 环路与重复路径 | PASS：`TestExpandScopeStopsCyclesAndDuplicatePaths`（P↔B、B 列 A/R、P 与 R 都列 B；B 只查询一次，无假 unknown）、`TestScopeCreateToleratesMutualMemberDirectoriesAndSeals`（HTTP 层 P↔B 互列，scope 仍 `sealed`、2 个目标、2 份 child manifest、每个目录只查一次）。 |
| 请求预算 | PASS：`TestExpandScopeMemberPageBudgetKeepsObservedMembers`（成员链第 3 页前停止，不再发 catalog 请求）、`TestExpandScopeBudgetKeepsObservedTargetsAndMarksPartial`（观察到的 p-col 保留）、`TestScopeCreateRemoteBudgetStopsRecursionHonestly`（HTTP 层 `max_remote_members=1`，B 未被联系、无 b-col、理由 `budget_exhausted`）。 |
| 节点预算 | PASS：`TestExpandScopeNodeBudgetBoundsContactedNodes`。 |
| 超时 / 401 / 未登记 | PASS：`TestExpandScopeTimeoutIsReportedHonestly`、`TestScopeCreateRemoteTimeoutIsHonestAndPartial`、`TestExpandScopeDeniedAndUnregisteredStayUncontacted`、`TestScopeCreateRemoteDeniedKeepsObservedTargets`；未登记成员零请求、理由 `unknown`。 |
| 撤销成员不联系 | PASS：`TestScopeCreateNeverContactsRevokedMember`（撤销后 fake peer 请求数为 0，scope 不含该成员）。 |
| 对等 members 读 | PASS：`TestPeerMembersFailsClosedAndServesOnlyVisibleApproved`（空配置/错 token 401、hidden/pending 不出现、终止页、改 limit 400、错 target 409）。 |
| 对等 collections 读 | PASS：`TestPeerCollectionsProxiesCorpusAndNeverLeaksCallerScope`（服务身份代理、剥离 caller scope、错误 descriptor origin 与错误信封 origin 均 502）。 |
| 配置解析与出站凭据 | PASS：`TestParsePeersFailsClosedOnMalformedDirectory`、`TestPeerClientNeverFollowsRedirectOrForwardsCredentials`、`TestPeerDirectoryUnknownNodeIsNeverContacted`；重定向未跟、未登记 host 零请求、注册 endpoint 确实收到凭据。 |
| 摘要与旧 scope 兼容 | PASS：`TestScopeDigestCoversChildManifestsDeterministically`（child_manifests 排序/去重/入摘要；空列表时字段省略，保持旧摘要 preimage 字节兼容）。 |
| corpus 目录生产者 | PASS：`test_node_published_snapshot_is_service_only_and_scope_is_server_derived`、`…_stable_pages_total_and_limit`、`…_withdrawal_is_not_completion`、`…_fails_closed_without_node_identity`；既有 caller-scoped `test_collection_catalog*.py` 14 passed / 2 skipped。 |
| 迁移与守卫 | PASS：0010 在真 PG 应用；`scripts/check_control_migrations.py` 两副本一致；`check_federation_contracts.py`、`gen_config_docs.py --check`、`ruff` 全绿。 |

### 变异确认（改掉被守的那一行，确认测试真的红，再还原）

| 变异 | 结果 |
| --- | --- |
| 对等凭据同时去掉空值与 constant-time 判定 | 红（空配置空 token 会放行） |
| peer 快照查询去掉 `visible_to_org` | 红（hidden 成员泄露） |
| `pullMembers` / `pullCatalog` 去掉请求预算检查 | 各自红 |
| `ScopeTargets` 去掉 `scope_remote_sources` 授权根 | 红（B 的目标被误判 revoked） |
| peer collections 校验去掉描述符 / 信封 origin 检查 | 各自红 |
| 出站 `CheckRedirect` 改为默认跟随 | 红（未登记 host 收到请求） |
| child_manifests 去掉排序/去重 | 红 |
| 展开算法同时去掉全部环路/重复路径守卫 | 红（测试超时终止） |

## 限制与后续

本次没有做真实多主机部署，也没有跨节点密钥交换：`FEDERATION_PEER_TOKEN` 仍是
所有已登记同伴共享的单值凭据（与 corpus P5 的已知局限相同），管理员登记不代表
对端持有私钥。peer 目录读按 `defaultOrg` 服务单组织部署；多组织下"本节点发布集合"
的聚合口径需要新的服务端 scope，不在本切片内。

远端目录的**读取期再验证**没有实现：scope 里远端目标的 revoked 判定只依据本地
授权根（直接成员）的当前状态，远端在冻结后撤回集合不会自动传播；远端快照
在展开期间失效只会得到 `unknown` + partial，不会伪造 completion。scope 有效期被
远端快照 `valid_until` 收紧，但不保存远端全文快照。`enumerate_members=false` 的
已登记子域仍会读取其自发布集合（子树标 `enumeration_unsupported`），这是刻意的
叶节点语义。

契约偏差：`scope-v1.yaml` 的 POST body 增补了 `max_discovery_requests` /
`max_remote_members` 两个可选项（超出任务列出的文件清单，但属于同一次契约先行的
必要更新）；`scope-control-format.md` 的 digest preimage 字段顺序说明尚未包含
`child_manifests`（该文件不在本次写入范围），建议后续补一句：
非空时位于 `registry_revision_vector` 与 `expanded_members` 之间，为空时字段省略。

没有 commit/push。本记录由本次作者自验，不替代独立 agent 的提交验收。
