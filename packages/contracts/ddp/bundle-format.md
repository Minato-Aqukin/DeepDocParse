# DDP-Bundle v1

`application/zip` 可移植快照；所有 JSON UTF-8，拒绝重复键、非有限数字。摘要是
`sha256:<64 lowercase hex>`，计算原始文件字节，manifest 自身不列入文件表。
Bundle 验证代表传输和结构完整，不代表远端声明的作者身份或内容真实。

根文件 `manifest.json`：

```json
{
  "schema": "ddp-bundle/1", "required_features": [],
  "source": {
    "origin_node_id": "node-a", "authority_node_id": "node-a",
    "resource_id": "resource-a", "source_version_id": "version-a",
    "source_digest": "sha256:<64 hex>", "parse_revision": null,
    "filename": "manual.pdf", "mime": "application/pdf",
    "original": "present", "missing_reason": null,
    "uploader_ref": null, "policy_revision": "private:revision"
  },
  "files": [
    {"path": "source.bin", "size": 123, "digest": "sha256:<64 hex>", "role": "original"},
    {"path": "layout.json", "size": 123, "digest": "sha256:<64 hex>", "role": "layout"},
    {"path": "evidence.json", "size": 2, "digest": "sha256:<64 hex>", "role": "evidence"},
    {"path": "provenance.json", "size": 2, "digest": "sha256:<64 hex>", "role": "provenance"}
  ]
}
```

`layout.json` 为 `{schema:"ddp-bundle-layout/1", state:"present"|"missing",
layout:<DDP-Layout object>|null, reason:string|null}`。`parse_revision=null` 必须
声明 layout missing，不能从可变的 current_job 随意取一个修订。布局 present
必须具备 `layout_version:"ddp-layout/1"` 和 `pdf_info` 页列表。

`evidence.json` 是数组，每项 `{evidence:<DDP-EVIDENCE FederatedEvidence>,
excerpt:string}`；excerpt 摘要按精确 UTF-8 字节验证。证据 source 身份与 manifest
一致，页序为从零起物理页，bbox 采用既有解析布局坐标，必须携带 page_size。
生成证据必须指向包中源证据，拒绝环、未知来源与伪造摘要。当前只接受 PDF 定位。
`provenance.json` 为 JSON 数组，只保存来源记录，不执行其中任何指令。

`original:"missing"` 时必须有非空 missing_reason，不能带 source.bin。
这类包可通过验证及导出，但中心导入返回 409 `bundle_source_missing`、
`state=source_missing`，不因所声明摘要去借用已有私有原文、不创建 READY 资源。
上传完整原文后可再次导入。向量索引不随包复用，响应明确 `index_state=rebuild_required`。

P1 固定限制：压缩文件 64 MiB、总解压 128 MiB、单文件 64 MiB、manifest
1 MiB、最多 32 项、最大压缩比 200。拒绝绝对/父目录/Windows/反斜杠路径、
符号链接/特殊文件、加密项、重复项、未声明项、缺文件、未知 schema 或必需特性。
只读取归档成员，不调用 extract/extractall。

端点（入口认证并注入 actor，上游另验 service credential）：

- `GET /api/resources/{resource_id}/versions/{version_id}/bundle`：资源读授权；返回 ZIP。
- `POST /api/bundles/import`：contributor，原始 ZIP 请求体；必须 `Idempotency-Key`。
  服务有界流式暂存，完整验证后创建当前 actor 私有的新资源（显式“另存为我的资源”）；
  所有者和公开策略不从包继承。返回 resource_id、source_version_id（本地版）、
  source（原始身份）、state=ready、index_state=rebuild_required。来源 manifest 保留，
  再导出不改来源身份。重复键同正文返回原结果，异正文 409。
- `GET /api/resources/{resource_id}/versions/{version_id}/bundle/evidence`：相同资源读授权，
  返回冻结的原始证据信封和原文缺失状态。导入证据不能自动变成中心已复核证据。

导入对象使用新的本地版本前缀。全部写入成功后同事务发布 Resource/Version/UploadEvent；
写入失败不发布资源并清理已写入对象。存储读写使用同一副本，读回摘要校验后才发布。
ResourceVersion.parse_job_id 固定本地解析修订，bundle_prefix 指向已验证快照。
GC 跳过仍被活版本、活任务、未结束解析或合法长期引用使用的内容；共享对象不得因一人删除而消失。
