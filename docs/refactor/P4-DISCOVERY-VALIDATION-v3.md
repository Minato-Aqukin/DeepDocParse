# P4 discovery control 自验记录（2026-09-12）

当前状态：控制域直接成员目录切片自验通过，**不是 P4 完成，也不是 commit 前的独立验收**。执行权威是工作区上级 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md`；控制契约见 `packages/contracts/ddp/discovery-control-format.md`。

## 本次收尾

- 持久节点 Ed25519 身份、管理员登记/批准/撤销、固定调用者范围的持久分页快照和公开配对 nonce 证明已接线。
- 修复目录隐私旁路：组织全局修订计数只留在内部目录锁/管理路径。快照 `registry_revision` 改为 `(organization_id, caller_scope_hash)` 下持久可见集合 fingerprint 的观测修订；隐藏成员写入不改变可见修订。返回成员 `revision` 使用该成员自己的计数。
- fingerprint 不包含每次快照新生成的路线观测时间；分页大小、TTL、服务/连接池重启不制造目录变更。
- `0005_discovery` 保持已应用内容。追加 `0006_discovery_scoped_revision`，新增成员计数及 control 独占可见视图表，并使升级前携带全局修订号的旧快照过期。0006 已应用后同样冻结。
- 同一用户的不同 key、key 与会话、不同主体之间不能移植快照。重新鉴权得到不同 key scope 时旧快照不可用；撤销 key 后不能继续读快照。

## 验证环境与结果

仅使用一次性容器 `ddp-v3-review-pg`，端口 `127.0.0.1:15439`，数据库 `ddp_v3_discovery`；没有连接用户开发数据库。Go 为 `/home/minatoaqukin/.local/opt/go/bin/go`。测试连接串通过环境变量传入，文档不保存密码。

| 项目 | 结果与证据 |
| --- | --- |
| control-api 全量真实 PG + race | PASS：在 `services/control-api` 执行 `CONTROL_TEST_DATABASE_URL` 已指向上述一次性库的 `go test -race -count=1 ./...`。日志 `/tmp/ddp-v3-discovery-finish-all.log`。|
| 隐藏成员变更隔离 | PASS：隐藏登记、pending、批准、描述更新、撤销、新增不改变普通调用者视图修订及已可见成员修订；管理员自己的可见视图正常推进。`TestDiscoveryScopedRevisionDoesNotCountHiddenChanges` 与 HTTP 权限测试。|
| 并发可见修订 | PASS：12 个并发快照与 8 个隐藏成员登记/批准交错运行，所有快照保持 revision 1；数据库仅持久一行范围视图。`TestDiscoveryConcurrentSnapshotsShareScopedRevisionDuringHiddenWrites`。|
| 稳定分页与可见变更竞争 | PASS：快照与可见目录写入竞争得到一致的旧/新集合；另一个连接池读取原快照；稳定终止游标；新增不进入旧集合；撤销保留成员位置并移除描述/路线。`TestDiscoverySnapshotSurvivesPoolRestartAndConcurrentDirectoryChange`、`TestDiscoverySnapshotsFreezeVisibleMembershipAndRetainRevocations`。|
| 调用者、凭据、当前权限 | PASS：跨主体、组织、快照游标返回 404；不同 key/会话及 key scope 变化返回 404；撤销 key 返回 401；本人过期返回 410，其他主体不能借过期状态识别快照。`TestDiscoveryHTTPSnapshotBindsCredentialAndFreshScope` 等。|
| 数据库物理职责边界 | PASS：六张 discovery 表的权限断言覆盖 `node_directory_views`。另在事务中实际 `SET LOCAL ROLE ddp_control` 成功 SELECT/INSERT/UPDATE/DELETE 新表；切换 `ddp_corpus` 的 SELECT/INSERT 均以 insufficient_privilege 失败。测试事务全部回滚。|
| 迁移账本与两份 SQL | PASS：`control-migrate status` 报 0001–0006 全部已应用、无漂移；0005 SHA256 为 `11f7bd3d19ef65bbbe9d3c3efb9161964cb2c93e2e11ededc77472907db19abb`；0006 仓库、内嵌与账本均为 `b50cf285d18e6f988288f2831b54bc2a0dbec5997283a72293244def64d54e57`。|
| 持久身份和配对证明 | PASS：重启/目录恢复保持身份；缺失/不安全/畸形/符号链接 seed 拒绝；48 个并发初始化只发布一把完整密钥；Go 响应被真实 TypeScript `HttpProvider.inspect` 验签，验签前未读取凭据。|
| 禁止隐式远端请求 | PASS：登记/批准受控测试端点的请求计数始终为零；能力只读固定 corpus `/internal/capabilities`，拒绝重定向、未知时空 profiles，不把静态配置当作可接单。|

Go↔TypeScript 的验证使用当前 `packages/client-runtime/src/http-provider.ts`，包含端点路径中的 `&`，验证 Go JSON 关闭 HTML 转义后与 JavaScript 签名消息一致。没有用手写签名替代真实 Provider。

## 限制与后续

本次没有实现节点间 credential/受限委托、远端信任准入握手、资源 ACL 授权或远端读取；管理员批准不代表远端已经证明密钥，也不授予资源权限。没有递归目录展开、增量/撤销事件流、ScopeManifest 封存、CollectionDescriptor/精确版本定位或受控文件中继。下级未展开时保留 `unexpanded_subtree`，配置成员 `health=unknown`、`accepting_admissions=false`。

这证明本中心已批准直接成员目录的隐私隔离、分页和身份协议切片，不证明 P4 全部出口、P5–P7 业务链或跨中心网络恢复。阶段验收仍需真实 App API Provider 完成 A→B/C 目录流程，并完成上述剩余出口。没有 commit/push，本记录由本次作者自验，不替代独立 agent 的提交验收。
