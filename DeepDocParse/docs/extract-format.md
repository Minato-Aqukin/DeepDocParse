# DDP-Extract v1 —— 结构化抽取结果格式

`/v1/extract/{task_id}/result` 与 Web 层 `/api/extractions/{run_id}` 返回的就是这个格式。

这份格式要回答的问题只有一个：**这个字段的值，是从原件的哪一块抽出来的？**
抽取产品普遍只给「字段 + 置信度」，指不回原文——本项目把出处从 chunk 级下沉到**字段级**，
差异化全押在这里。

- 抽取编排：`gateway/app/services/extraction.py`（service 侧）
  与 `DeepDocParse-Web/backend/app/extraction.py`（产品侧，按铁律 1 各写一份）
- 消费方：`openapi.yaml` 的 `/v1/extract*`、前端 `components/extract/`、`scripts/eval_extraction.py`

## 输入：受限的 JSON Schema

**刻意只支持 JSON Schema 的一个子集。** 支持的：

```jsonc
{
  "type": "object",              // 或 "array"（多记录/表格抽取）
  "properties": {
    "buyer_name": {
      "type": "string",
      "description": "买方（甲方）单位全称"   // ← 同时是检索 query 与抽取提示，写清楚很值钱
    },
    "total_amount": { "type": "number", "description": "价税合计金额，只要数字" },
    "signed_at":    { "type": "string", "format": "date", "description": "合同签订日期" }
  },
  "required": ["buyer_name"]
}
```

| 支持 | 说明 |
|---|---|
| 顶层 `type: object` + `properties` | 单记录抽取（合同、发票、报告封面） |
| 顶层 `type: array` + `items.properties` | 多记录抽取（表格行、条目清单） |
| 叶子 `type`：`string` / `number` / `integer` / `boolean` | 值会按类型强制转换，转不动判 `error` |
| `format`：`date` / `date-time` / `email` / `uri` | 仅作抽取提示与格式校验，不做时区换算。**`uri` 只作提示，不校验**（URL 的合法形态太多，正则校验会误杀） |
| `description` | **最重要的一项**：它就是这个字段的检索 query |
| `enum` | 值必须落在枚举内。**越界判 `error` + `schema_violation`**，不是 `not_found` —— 模型给了一个枚举外的值是"它答错了"，不是"文档里没有"。不许把没有的选项硬塞一个 |
| `required` | 缺了它整体 `status` 降为 `partial` |

**明确不支持**（不是没来得及做，是刻意的）：

| 不支持 | 为什么 |
|---|---|
| 嵌套 object（`properties` 里再套 object） | 每加一层嵌套，检索次数与出处归属的歧义都乘一次。拍平写成 `buyer.name` 这样的扁平键 |
| `oneOf` / `anyOf` / `allOf` / `$ref` | 分支语义没法映射到「一个字段一次定位」，出处会指到多个互斥的块 |
| 数组套数组 | 同上 |
| 无 `description` 的字段 | **会被拒绝**：没有 description 就只能拿字段名当 query，`f1`/`amt` 这种名字检索必然打偏，而失败会表现为"抽不到"，看起来像模型不行 |

## 输出

```jsonc
{
  "extract_version": "ddp-extract/1",
  "doc_hash": "9f2c…",              // 文档身份（ADR #11），与解析平面同一个键
  "status": "ok",                    // ok | partial | failed
  "degraded": null,                  // 整体降级标记，取值见下表
  "fields": {                        // 顶层是 object 时用这个
    "buyer_name": {
      "status": "found",             // found | not_found | error
      "value": "北京某某科技有限公司",
      "citations": [ /* 见下 */ ],
      "verified": true,              // 做过视觉核对且一致
      "degraded": null,              // 字段级降级
      "confidence": {"level": "high", "top_similarity": 0.78, "warn_below": 0.60}
    },
    "total_amount": {
      "status": "not_found",         // 文档里确实没有 —— 这是正确答案的一种，不是失败
      "value": null, "citations": [], "verified": false,
      "degraded": null, "confidence": {"level": "unknown", "top_similarity": null}
    }
  },
  "records": [ { "fields": { … } } ], // 顶层是 array 时用这个，元素形状与上面的 fields 一致
  "usage": {"fields": 12, "retrievals": 12, "chat_calls": 3}
}
```

### 字段三态（**这份格式最要紧的地方**）

| status | 含义 | 判据 |
|---|---|---|
| `found` | 抽到了值，且有出处 | 检索命中过相似度下限，模型从块里抽出了值 |
| `not_found` | **文档里确实没有这个字段** | 检索零命中，或模型明确回答未找到 |
| `error` | 没能抽成（系统问题） | 类型转换失败、schema 校验不过、上游不可达 |

**`not_found` 与 `error` 必须分开。** 合成一个的话，"这份合同没写违约金"和"我们的检索挂了"
长得一模一样，用户没法判断该不该信这个空值 —— 而空值恰恰是抽取里最危险的输出：
它看起来像一个结论。评测里的「空值正确率」量的就是这一条。

**永远不许为了填满 schema 而编值。** 抽不到就是 `not_found`，
这是 `docs/EVAL.md` 里「拒答正确率」在抽取上的对应物。

### 出处（citation）

形状与问答平面的 citation **完全一致**，前端 `CitationChip.vue` 不用改就能复用：

```jsonc
{
  "chunk_id": "…",        // 即时引用，reindex 后失效，不要存
  "parse_job_id": "…",    // 稳定定位键之一（service 侧为 null）
  "doc_hash": "…",        // service 侧的稳定定位键（Web 侧为 null）
  "seq": 3,               // 稳定定位键之二
  "page_idx": 2,          // 0 基页码
  "bbox": [72, 300, 540, 330],
  "page_size": [612, 792],
  "snippet": "…",
  "similarity": 0.78,     // 余弦相似度，有量纲。**不是 RRF 分**
  "crop_url": "…"         // 区域截图，可选
}
```

**稳定定位键是 `(parse_job_id, seq)`（Web 侧）/ `(doc_hash, seq)`（service 侧）**，
与问答平面同一套。理由见 ADR #11 与 P0：`chunk_id` 每次重建索引都会重铸，
只存它等于历史抽取结果一次 reindex 就永久失去原文依据。

### 降级标记

字段级 `degraded` 与整体 `degraded` 取值同一张表。前七种沿用问答平面，后两种是新增的：

| 值 | 含义 |
|---|---|
| `no_hits` | 检索零命中 |
| `embedding_unavailable` | 向量化不可用，只走了关键词路 |
| `vision_unavailable` | 视觉模型不可用，没做出处核对 |
| `crop_unsupported` | 非 PDF，裁不出区域图 |
| `crop_failed` | 能裁但渲染失败 |
| `parse_mismatch` | 裁出来的图与块文本对不上（解析本身可疑） |
| `upstream_error` | 上游模型报错 |
| **`schema_violation`** | **新增**：模型输出反复不合 schema，重试用尽 |
| **`rerank_unavailable`** | **新增**：配了精排但上游没注册 rerank 模型。照常返回融合名次，但如实说明没精排 |

`schema_violation` 是抽取平面独有的洞：模型可以流利地输出一段**不是合法 JSON**、
或者字段名对不上、或者类型不对的东西。重试 `EXTRACT_MAX_RETRIES` 次仍不合规就打这个标，
**绝不静默丢弃该字段** —— 静默丢弃会让结果看起来像"文档里没有"。

## 自检

```python
from app.services import extract_format
problems = extract_format.validate_schema(user_schema)   # 返回问题清单，空 = 通过
problems = extract_format.validate_result(result)
```

`validate_schema` **在请求路径上强制**（与版面的 `validate` 不同）：
schema 是调用方给的输入，坏输入要当场 400 拒掉，而不是跑完一轮抽取再说。
`validate_result` 只在测试里是硬断言。
