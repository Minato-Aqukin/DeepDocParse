# P4 ScopeManifest / Collection catalog 独立验收

2026-09-13。范围限定为当前未提交工作树中的本地 ScopeManifest、直接成员目录、Corpus Collection catalog，以及 control 0007/0008、corpus 0026 和相应协议扩展。依据工作区升级计划 v3 §5、§7.3–7.4、T71–75，交叉核对三份 P4 作者验证记录与 collection/scope-control 契约。**这不是整个 P4/P5 或某次 commit 的正式通过结论。**

初验发现 2 项阻塞与 1 项较小契约偏差，作者正在修复；以下初验 FAIL 不能由已有测试通过抵销。复验结果会追加在本文件，保留原始反例。

## 环境与独立性

- PostgreSQL 仅用已授权 scratch 容器 `127.0.0.1:15439`；新建 `ddp_p4_review_control` 与 `ddp_p4_review_corpus`。不连接开发库、不读 `.env`。
- Control 复制到 `/tmp/ddp-p4-independent/control-api`，明确排除 `.env*` 与上传作者尚未冻结的 0009；真实迁移仅 0001→0008。Corpus 用真实 Alembic 0001→0026；没有 `create_all` 代替 PG 验证。
- 独立反例只写 scratch 测试文件；实现、原迁移和作者数据库均未修改。该验收只新增本报告及其复现附件。初验 Control 源文件摘要见 [initial-control-sha256.txt](artifacts/p4-independent-review/initial-control-sha256.txt)。
- API 异常目录场景使用受控 HTTP producer，验证消费者对不一致契约的防御。另单独运行真实 Go→Python HTTP producer 集成，不把异常 fixture 当作真实 Corpus 行为。

## 初验发现

### F1 — FAIL / P1：未核对目录 total，遗漏成员仍可封存

`internal/api/scope_handlers.go` 的 `scopeCatalogPage` 没有读取 `total`，`collectScopeCatalog` 仅凭空 terminal 页设置 complete。受控 producer 保持调用者、scope、snapshot、revision、时间和游标一致，以下三种输入都被真实 Scope POST 保存为 `sealed`：

1. 两页均无集合，首个空 data 页 next=terminal，terminal complete=true；两页 `total=2` → **sealed、total_targets=0**。
2. data 页仅含集合 A，随后正常空 terminal；两页 `total=2` → **sealed、total_targets=1**。
3. data 页含 A 且 total=1，terminal 改为 total=2 → **sealed、total_targets=1**。

违反 T71/T73：已观察数量与生产者自己声明的固定目录数量矛盾，却作出完整枚举承诺。复现：[api_test.go.txt](artifacts/p4-independent-review/api_test.go.txt)，`TestIndependentRejectCatalogCountMismatch/{empty_data,omitted_collection,changing_total}`。预期均为 partial；保留已经一致观察到的目标。

修复建议：total 必须存在、非负、有界、跨页稳定，并与最终唯一集合数匹配；验证空 data 页与终止页语义。重复路径可以去重，但不能借去重掩盖 total 不一致。缺失 total 与合法的 total=0 必须区分。

### F2 — FAIL / P2：撤回再授予可使旧成员快照复活

`internal/store/discovery.go:MemberSnapshotPage` 只比较当前 state/visible，没有把冻结成员的授权代际绑定到当前记录。全部使用正常 Store 操作，无 SQL 篡改：

1. 登记并批准公开成员 descriptor revision=1；Alice 建快照 S。
2. RegisterNode revision=2、visible_to_org=false，再批准；Alice 读 S，成员正确显示 revoked，无 descriptor/route。
3. RegisterNode revision=3、visible_to_org=true，再批准；再次读同一个 S，成员变回 **approved，且返回旧 descriptor revision=1**。

违反 T74 和 §5.5 的“旧修订不能覆盖新撤销”。新的授权应通过新快照表达，不能恢复旧撤销记录。复现：[store_test.go.txt](artifacts/p4-independent-review/store_test.go.txt)，`TestIndependentSnapshotPermissionEpochDoesNotRevive`。显式永久 `SetNodeState(revoked)` 路径本身拒绝复活；此反例走可见性收回/再授予路径。当前实现尚无远端执行，因此没有宣称该问题已导致实际远端内容外泄。

修复建议：冻结成员须绑定单调的授权代际；撤回期即使无人读取也应使旧快照保持撤销。当前 RegisterNode 会退回 pending 并递增 public_revision，重新批准再次递增，因此可将它用作该实现中的代际，要求与冻结 Member.Revision 相等。正常描述重登记也会使旧快照失效，需明确记录此语义。

### F3 — FAIL / P3：错误游标遇过期快照，错误码偏离契约

同一合法调用者传入已过期 snapshot_id 与错误 cursor，`catalog.snapshot_page` 在匹配 cursor 前检查 expiry，返回 `catalog_snapshot_expired`。collection-catalog 契约要求错误 scope/caller/cursor 统一 `catalog_snapshot_invalid`，仅合法的已过期快照读取返回 expired。

复现：[test_catalog.py.txt](artifacts/p4-independent-review/test_catalog.py.txt)，`test_independent_expired_invalid_cursor`。这次没有发现跨用户存在性或 collection ID 泄漏；属于校验顺序与契约一致性问题。先验证 snapshot/caller/scope/cursor 绑定，再区分 expiry。

## 已执行的逐项检查

| 项目 | 初验 | 独立证据 / 限制 |
| --- | --- | --- |
| T71 固定分页、独立终止页、去重分母 | FAIL | 正常分页通过；F1 表明 total 不一致仍可 sealed。 |
| T72 下级不可展开 | PASS（本地切片） | 重新运行 PG direct-members 测试；已批准远端保留 unknown/unsupported，隐藏成员不计数，未调用远端。没有验证真实递归网络。 |
| T73 修订混接、循环、缺终止、超预算 | 部分 PASS / 总体 FAIL | 作者对应异常测试独立重跑通过；F1 是此前漏掉的 total 矛盾分支。 |
| T74 固定目标/摘要及撤销 | 部分 PASS / 总体 FAIL | 真实 Go→Python withdraw 链路保留旧两个目标与 digest，新 scope 一个目标；F2 使成员授权代际仍不合格。 |
| T75 成员/目录快照与全文快照分离 | PASS（语义） | 始终显式 not_frozen / content_snapshot_complete=false，索引变更不会报告检索成功。未验历史全文或检索回执，这些尚未实现。 |
| 私有集合变更不泄漏 count/revision | PASS | 独立 PG 用同组织另一私有 owner 创建、替换带秘密标签的 collection；公开 total/revision 不变、隐藏 ID 不出响应。 |
| 索引代际变更 | PASS | 独立 PG 改 ParseJob.index_generation；原终止证明返回 changed、无 revoked IDs，新目录保留集合且 index_revision 更新。 |
| 当前身份与权限 | PASS | 独立 PG 对已撤回 source 的快照测试其他 key、组织、scope、错误 cursor，均无 revoked IDs；合法原调用者只收到快照内对应 ID。现有 HTTP key 失效/跨凭据测试重跑通过。 |
| 撤销与过期分开 | FAIL（F3） | Scope 已观察的 revoked 在重启/expiry 后保留；变化/过期不推断 revoked。但 Corpus 错 cursor + expiry 顺序偏差。 |
| JSONB 后 digest 可复算 | PASS | 独立真实 PG CreateScope→ScopeManifest→FinalizeScope，operation 含中文、HTML 转义字符和 U+2028；摘要完全一致。不是用 Go map 重排模拟 JSONB。 |
| SQL 写入职责 | PASS | 实际 SET LOCAL ROLE：ddp_control 能 SELECT/无副作用 DELETE 四张新 Scope 表，ddp_corpus SELECT 被拒；真实迁移后执行仓库 grants.sql，六张 collection 表反向验得同样隔离。未修改共享角色口令。 |
| 真实跨服务链 | PASS | 独立两库运行 TestScopeRealCorpusPublishedCatalogAndWithdrawal；仅调整 scratch 测试中的 Python 可执行文件/fixture 路径，没有改 producer 实现或新增 mutation backdoor。 |

现有验证重新执行：Control `TestScope|TestDiscovery` 及实际 JSONB 独立检查通过（真实 Corpus 集成另行启用）；Corpus collection 两文件 **10 passed**。独立新 Corpus 检查 **2 passed / 1 failed**，独立新 Go 初验 **4 个异常子场景 FAIL、JSONB PASS**。这些数字仅描述本轮所跑的切片，不代表整个仓库全绿。

## 复现方式

附件的 Go 测试分别复制到 scratch Control 的 `internal/api/independent_review_test.go`、`internal/store/independent_review_test.go`；Python 测试复制到 scratch 工作目录。先迁移专用数据库，再通过环境变量提供 scratch DSN，文档不保存口令：

```bash
# 在排除未冻结迁移的 scratch Control 副本中
GOWORK=off CONTROL_TEST_DATABASE_URL="$P4_REVIEW_CONTROL_DSN" go test -count=1 -run TestIndependent -v ./internal/api ./internal/store
# 在无 .env 的 scratch 目录中，PYTHONPATH 指向仓库 services/corpus-api/tests
COLLECTION_CATALOG_TEST_DATABASE_URL="$P4_REVIEW_CORPUS_DSN" python -m pytest -p conftest -o asyncio_mode=auto -q test_independent_catalog.py
```

实际临时副本在 `/tmp/ddp-p4-independent/`。生产者只接受固定配置的 Corpus endpoint、服务身份和 Control 推导的调用者上下文；本轮没有外部网络委托、GPU 调用或远端资源读取。当前直接目录完整性通过也不能替代计划要求的 App Provider A→B/C、多级拓扑、Task/Probe、范围覆盖账本与检索回执验收。

## 修复复验

待作者完成 F1/F2/F3 修复后，用原独立反例和“撤销间隔无人读取”的附加变体复验；在此之前本报告不作切片通过结论。
