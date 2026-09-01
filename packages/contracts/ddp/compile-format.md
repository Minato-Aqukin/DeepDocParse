# DDP-Compile v1 —— 语料编译产物

DDP-Compile 定义“归档版面怎样变成可检索、可复核、可更新的语料”。它不是新的解析格式；
输入仍是 DDP-Layout，输出落在 `chunks`、`evidence` 与对象存储中。

## 一条源原子，两种内容

每个可定位的版面原子先产生一条**源证据**：

```text
source evidence
  atom_key       source:<seq>:<anchor>
  seq/page/bbox/page_size
  content        原文
  crop_key       编译期裁图；裁不出必须留降级原因
  provider       见下文
  derived_from   null
```

VLM 对 `code / equation / table / figure` 的理解是生成物。成功时另建一条**派生证据**：

```text
derived evidence
  atom_key       vision:<seq>:<content_digest_prefix>
  seq/page/bbox/page_size  与源原子相同
  content        VLM 描述与结构化要素
  crop_key       与源原子相同
  provider       在源 provider 上追加 content_role=generated
  derived_from   source evidence.id
```

派生内容可以参与 embedding 与关键词索引，但进入问答上下文时必须标
`[生成理解，原子证据 <evidence_id>]`；引用指向派生 evidence，最终沿 `derived_from`
回到源原子的 bbox。生成内容不得冒充原文。

## 四条索引路

| kind | 编译后的检索文本 |
|---|---|
| `text / title / list / other` | 原文；沿用通用分词 |
| `code` | 原文；关键词列同时保留完整标识符并拆分 camelCase、snake_case、点号路径 |
| `equation` | 原公式 + `\\alpha ↔ α` 归一化别名 + 同页前后相邻句 |
| `table / figure` | caption / 原文 + `table_html`（如有）+ VLM 派生理解 |

源 `text` 永远保留原样；上表只影响 `search_text`。因此重建索引不会把生成物写回原文，
`content_digest` 也始终对源内容计算。

## Provider 与指纹

每个源 chunk 与源 evidence 都记录同一份 provider：

```jsonc
{
  "layout_engine": "mineru",
  "layout_version": "ddp-layout/1",
  "parse_options_hash": "...",
  "compiler": "ddp-compile/1",
  "chunker": "ddp-chunk/2",
  "tokenizer": "jieba|bigram",
  "embedding_model": "BAAI/bge-m3",
  "vision_model": "Qwen3-VL-8B",
  "provider_resolved": true
}
```

`provider_fingerprint` 是上述对象按键排序、紧凑 JSON 序列化后的 SHA-256。
模型名必须是调用方明确指定的有效模型才可比较。调用方把选择委托给上游注册表时，
模型字段写 `<upstream-default:unresolved>`、`provider_resolved=false`；版本校验必须返回
`provider_unresolved`，不得把两次无法观测的默认选择称为 `current`。
派生 evidence 在这份完整 provider 上另加 `content_role=generated`，因此它的
`provider_fingerprint` 与源原子不同，且可机械证明内容不是原文。

## 版本校验动作

版本校验是**只读动作**：用当前配置重新计算期望 provider 指纹，与文档现有 chunk 指纹比较，
返回：

```jsonc
{
  "status": "current|stale|unresolved|uncompiled",
  "observed_fingerprints": ["..."],
  "expected_fingerprint": "...",
  "reasons": ["embedding_model_changed"],
  "safe_to_reindex": false
}
```

校验动作绝不自动重建。`code_detection` 缺失与 compiler/chunker 变化进入 `reasons`；
同时用新分块规则做一次 dry-run，逐条检查历史 citation。只要有一条源出处接不回，或引用了
可能随 VLM 再生成而变化的派生描述，`safe_to_reindex=false`。调用方必须明确确认这些出处将
**显式失效**后才能单独触发重建，绝不能把旧引用悄悄接到新 seq。

索引执行还必须携带单调递增的 `index_generation` fencing token 与可续租 lease：
活 worker 定期续租；崩溃后只有 lease 过期才允许对账接管；成功/失败写回均须同时匹配
`current_job_id + index_generation`。问答在检索时捕获同一 generation，落库时若已变化，
必须保留显式 `evidence_id` 的 Citation、标为未核验并报告
`index_changed_during_answer`，不得把检索时仍有效的出处静默丢掉。

## 可见降级

编译允许部分成功，但每种缺口都要进入文档的 `compile_degraded[]` 并由前端显示：

- `code_detection_unavailable`
- `crop_unsupported` / `crop_failed`
- `vision_unavailable` / `vision_invalid_output`
- `provider_unresolved`
- `reindex_validation_required`（存在历史出处的复活文档，尚未获人工确认）

如果连可检索源文本都没有，索引仍失败；不得用空描述或伪造整页 bbox 把状态装成 ready。
