# DeepDocParse corpus MCP v1

状态：**v1 冻结**。作用域是一台服务器的整份共享语料，不要求先指定文档。
所有成功返回的知识结论都带 `evidence_id + page_idx + bbox + page_size + crop_url`；
无法定位时必须显式 `resolved=false` 或 `unsupported=true`。

## 工具

### `search(query, limit=10)`

返回跨语料混合检索结果：

```json
{"results":[{"evidence_id":"...","document_id":"...","page_idx":0,
"bbox":[0,0,10,10],"page_size":[612,792],"crop_url":"...",
"snippet":"...","score":0.03,"similarity":0.82,"source_type":"source"}],
"degraded":null}
```

### `ask(question)`

返回 DDP-Agent v1 `Assertion[]`，每条包含 `text / evidence_ids / verification /
unsupported / citations`。不得退回无类型的整段字符串。

### `get_evidence(evidence_id)`

返回证据元数据、原文和裁图。HTTP/JSON 结果包含 `crop_url`；MCP 响应同时附带
原生 image content（裁图存在时），让外部 agent 能自行核对。

`crop_degraded` 区分「这条证据本来就没有裁图」与「裁图取不到」：

| 值 | 含义 |
|---|---|
| `null` | 没有降级：要么图已随响应返回，要么这条证据本就没有 `crop_key` |
| `crop_store_unavailable` | 有 `crop_key`，但对象存储没配/依赖没装，拿不到像素 |
| `crop_read_failed` | 有 `crop_key`，对象存储可达但这一次读取失败 |

**取不到图不许静默退化成"没有图"** —— 外部 agent 会据此以为这条证据无法核对。

### `read_wiki(entry_id_or_title)`

返回 DDP-Graph v1 Wiki 形状。每个句子必须有有效 `evidence_ids`，否则显式
`unsupported=true`；冲突句以 `conflict_group` 并列。

### `graph_neighbors(entity_id_or_name, depth=1)`

返回中心节点、N 跳节点与边。每条有效边带证据详情；`depth` 范围 `1..3`。

## 兼容工具

`ask_document(file_url, question)` 保留原签名并标 deprecated，不删除、不改语义。

## 错误与降级

- 查无实体、wiki 或证据：结构化 `not_found`，不得编造空壳内容。
- embedding/rerank/VLM 不可用：结果仍可返回，但 `degraded` 必须给稳定原因码。
- 数据库或对象存储不可用：工具失败并给明确错误；不得返回看似成功的空数组。

