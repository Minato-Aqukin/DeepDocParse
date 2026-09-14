# P1 Bundle / 引用安全 GC 验证（2026-09-12）

范围：计划 PR04 的中心端导入/导出、共享的纯 ZIP 校验、固定出处和引用回收。
这些结果不代表 P2 完整本地运行时、真实模型质量或跨节点许可已验收。

## 可重复验证

| 输入 / 检查 | 期望 | 实测 |
|---|---|---|
| `python/ddp_core/tests/test_bundle.py`：固定原文、来源节点、版本与证据；ZIP 往返 | 身份、摘要、布局与出处不变 | 27 passed |
| 同组恶意 ZIP：绝对/父目录/Windows 路径、符号链接、重复成员、压缩炸弹、未知 schema/必需特性、缺文件、错误摘要、伪证据/循环来源/非法 Unicode | 在发布前拒绝 | 包含在上述 27 例 |
| `services/corpus-api/tests/test_bundles.py` | 真 HTTP 路径验证上传者私有资产、授权、幂等、原文缺失、存储失败、固定解析修订、GC 共享/任务/答案/Wiki引用与失败续扫 | 16 passed |
| 同一次回归 `tests/test_ops.py tests/test_storage.py` | 原有 GC 与存储行为保持 | 14 passed；合计 30 passed |
| `scripts/check_federation_contracts.py` | 新 Bundle JSON Schema、正反例及生成物同步 | 7 schema / 66 fixtures / 47 definitions PASS |
| `scripts/gen_config_docs.py --check` | 三份配置参考与代码同步 | PASS |
| 修改模块 ruff F/B | 没有新增 bug 类静态错误 | PASS |

固定协议夹具：`tests/ddp_bundle_fixture.py`。原文是用于传输契约的固定字节，
解析布局是明确的协议夹具，**没有使用 mock 宣称真实 PDF 引擎或模型能力通过**。
HTTP 测试使用真实路由和生产 actor/resource ACL，SQLite 外键开启、MemoryStorage；
不跳过 service credentials。

## PostgreSQL 行锁与并发

复现脚本 `scripts/verify_bundle_gc_pg.py`，通过
`DDP_GC_TEST_DATABASE_URL` 指向明确选择的测试库。脚本在随机临时 schema 中
建表、检查并清理；对象存储为能暂停删除的 MemoryStorage，不接用户对象桶。

已在临时 PostgreSQL 测试库实跑，Linux x86_64、Python 3.14.7、SQLAlchemy 2.0.52：

1. A 资源删除，B 活版本仍共享内容：GC 返回 0，原文保留，PASS。
2. 所有资产删除并过宽限期，GC 正在删对象：写新引用的事务阻塞于 Document 行锁，PASS。
3. GC 提交后重新上传的事务观察到旧 object_key 已清空，绑定新对象；下一轮 GC 不删它，PASS。

回收先持久化 `documents.gc_pending_keys`，再重新锁行并重新检查所有引用。
删除失败保留 `gc_error` 和精确剩余 key，不增加成功计数；即使 `object_key` 已清空，
下一轮仍继续回收。迁移 `0020` 在仍有待删清单时拒绝降级，避免丢失未完成工作。

## 已明确的行为与限制

- 原文缺失的包可校验/导出；中心导入返回 `409 bundle_source_missing`，不凭摘要
  借用其他用户原文，不发布 READY 资产。
- 导入必须提供完整原文字节。新本地资产 owner/uploader 来自当前可信 principal，
  默认为 private；原始来源 tuple 和 evidence_id 保存在版本快照，不替换为本地身份。
- 远端来源用 `remote:<digest>` 命名空间登记 `copied_from`，不会因 ID 碰撞命中本地
  published 祖先。远端来源许可尚未落地，发布因此保守拒绝；来源声明本身不等于身份认证。
- 本地原生导出需配置唯一且持久的 `BUNDLE_NODE_ID`；缺配置返回明确 503。
  `ResourceVersion.parse_job_id` 未冻结时明确声明布局缺失，不偷取 current_job。
- 导入的证据可从受授权的 Bundle evidence 端点读取及再次导出；不会自动替换中心已有解析、
  索引或把远端声明认作已复核证据。索引响应为 `rebuild_required`；真正本地检索/模型路径属 P2。
- 当前支持一个版本的一份原文及 layout/evidence/provenance；不包含 Wiki 修订打包、
  向量缓存复用、完整 Blob/Replica 目录或跨节点权限凭证。
- 对无显式输入清单的活任务、仍有持久 Citation/Wiki 依赖的内容，GC 保守保留。
  服务正常入口的新资源引用须与 GC 使用同一 Document 行锁；存储接口有界读取避免
  不可信对象长度使 Bundle 导出越过读取预算。
- native MinIO 真实网络故障和完整 Go→Corpus→MinIO 往返尚未在本子任务验证；
  PostgreSQL 检查明确只证明数据库锁与引用关系。
