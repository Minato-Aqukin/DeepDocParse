# 历史解析版本恢复（v3 / T08）

历史任务缺少 `initiated_by` 时，不再用合并后 `Document.uploaded_by` 猜测归属。
0006 曾把其他上传者的任务改指同一个 Document，且没有保存任务原始发起人；
因此内容哈希相同、当前指针相同或能读某个公开副本，都不足以恢复任务权限。

## 自动迁移

0018 只绑定有明确发起人、原上传者组织和唯一原始资源的历史任务。
0024 在 0023 之后添加 `ResourceVersion.binding_provenance`，只恢复满足以下全部条件的任务：

- `ParseJob.resource_id` 已明确绑定；`initiated_by` 等于资源所有者，也等于历史 Document 首上传者。
- 资源组织与历史 Document 组织一致且非空，没有 `migration:` 隔离标记；资源不是副本或墓碑。
- 任务已 `succeeded`，同时存在 `archived_at` 和非空 `result_prefix`。
- 有属于该资源的未绑定原版本，其已验证 SHA-256 与 Document 完全一致。

迁移逐条追加固定版本，保留原 v1 的未绑定状态，也不修改已有固定版本。
若资源所选文档的 `current_job_id` 自身满足上述证明条件，将它作为最后追加的版本；
当前任务已经绑定时，可以追加一次同任务的选择版本，以免恢复更旧的任务改变默认选择。
重复运行不再新增版本。`current_job_id` 只决定已证明集合内的显示顺序，绝不证明归属。

来源依据保存在版本的 `binding_provenance`：`method=migration:0024`、确定性绑定摘要、
认定所有者、资源/任务/文档/组织/源摘要、记录时间及是否保留当前选择。

## 操作员核验清单

自动证明不足时，先核对原始上传事件、成员归属、任务日志或经过对账的历史记录。
若资源组织本身尚未恢复，先使用 `scripts/resolve_resource_migration.py` 处理隔离资源。
核验后的清单示例：

```json
{
  "version": "ddp-parse-binding-recovery/1",
  "reviewed_by": "operator-account-or-audit-record",
  "bindings": [
    {
      "resource_id": "verified-resource-id",
      "source_version_id": "existing-fixed-source-version",
      "parse_job_id": "verified-archived-job-id",
      "document_id": "verified-document-id",
      "source_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "owner_id": "verified-owner-id",
      "organization_id": "verified-organization-id"
    }
  ]
}
```

从仓库根运行，`DATABASE_URL` 应由迁移环境提供，不要把实际凭据写入清单或 Git：

```bash
.venv/bin/python scripts/restore_parse_bindings.py reviewed-bindings.json
.venv/bin/python scripts/restore_parse_bindings.py reviewed-bindings.json --apply
```

第一条命令只校验，不写入。第二条在一个事务中锁定文档、资源和任务后追加版本，
记录规范化清单 SHA-256、核验人、认定所有者、原版本定位和时间；全部记录成功才提交。
任务原 `initiated_by`、API key 与计费记录保持原样，未知发起人仍为 NULL。
这里的 `reviewed_by` 是离线操作员的审计声明，不是服务器已验证的用户会话。
持有迁移数据库权限的操作员须保存核验依据和原清单，不能仅凭同内容推断许可。

脚本拒绝错误 owner/org/doc/digest/version、已知发起人冲突、未归档任务、其他资源的绑定、
副本、墓碑及隔离资源。已存在的固定版本与其来源依据不能被清单覆盖。
相同清单重复提交返回 `existing_bindings`，不新增版本；任何记录失败都会回滚全部写入。
操作员恢复会追加一个可明确选择的新版本，不改 Document 的全局当前指针。

版本存在恢复依据时，0024 的 downgrade 会拒绝丢弃审计字段；需要先由操作员显式归档、
对账恢复记录，再安排数据库回退。迁移不会为让 downgrade 成功而删除已恢复的历史。

## 验证证据（2026-09-12）

环境：Linux x86_64、Python 3.14.7、PostgreSQL 16.15。未调用模型，模型版本不适用。

`services/corpus-api/tests/test_parse_binding_recovery.py` 验证：已知旧 Evidence 在迁移后
通过指定固定版本得到 HTTP 200，未知任务保持 404；原 v1 不变；恢复旧任务保留已知当前版本；
重复迁移/清单幂等；错误 owner/org/doc/hash/version 被拒；失败清单原子回滚；
不可覆盖已有绑定；未知 initiator 与历史 API key 不被改写。
`test_review_asset_boundaries.py` 保留 NULL initiator 越权复现，修复后期待 HTTP 404。

真 PostgreSQL 使用独立一次性数据库 `ddp_v3_recovery_review`：
先升级 0014，插入一个已知 owner/org/document、两个明确 owner 的已归档任务
`known-old` / `known-current`，以及 `initiated_by=NULL` 的 `unknown-job`，再升级 0024。
实测版本表为：v1 未绑定、v2→known-old、v3→known-current；unknown-job 的 resource_id 仍为 NULL。
对 unknown-job 运行已核验清单，dry run 报 new=1，首次 apply 报 new=1，再次 apply 报 existing=1/new=0；
仅新增 v4，`method=operator_manifest`，原 initiated_by 仍为 NULL。
该演练清单摘要为 `d32baeae3bba67f889fa37594594827e2413dfb2b713c9cc6f172fa8125780a6`。

这份记录只覆盖历史绑定恢复，不代表桌面端、联邦路由或全部 P1 的最终验收已完成。
