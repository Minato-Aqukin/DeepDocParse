# DDP-Graph v1

状态：**v1 冻结**。此文件是阶段 7 的实现契约；字段只能向后兼容地新增。

## 1. 不变式

1. 每条有效边必须有至少一条 `Citation(source_kind="graph_edge")`，并能回到
   `Evidence.page_idx + bbox + page_size`；否则 `unsupported=true`。
2. 负样本返回 `not_found`，不得为了连通图而编边。
3. 实体合并必须记录 `merged_by` 与 `merge_confidence`。低于阈值的合并标
   `entity_merge_uncertain=true`，并可由人工复核拆开。
4. 自动生成的边、摘要与 wiki 句子都是生成物，不得冒充 Evidence 原文。
5. 冲突不自动消解：互相矛盾的 wiki 句子用同一 `conflict_group` 并列呈现，
   各自保留出处。

## 2. 规范形状

```json
{
  "graph_version": "ddp-graph/1",
  "entities": [{
    "id": "entity-id",
    "canonical_name": "DeepDocParse",
    "entity_type": "system",
    "aliases": ["DDP"],
    "merged_by": "exact",
    "merge_confidence": 1.0,
    "entity_merge_uncertain": false,
    "review_state": "unreviewed"
  }],
  "edges": [{
    "id": "edge-id",
    "subject_id": "entity-id",
    "predicate": "uses",
    "object_id": "other-entity-id",
    "confidence": 0.91,
    "evidence_ids": ["evidence-id"],
    "unsupported": false,
    "review_state": "unreviewed",
    "provider": {"model": "model-id", "revision": "revision"}
  }]
}
```

词汇表：

- `merged_by`: `exact | alias | model | human | none`
- `review_state`: `unreviewed | passed | rejected | questioned`
- `provider` 至少包含实际生成边的模型或规则名；规则生成也不能留成空对象。
- `evidence_ids` 是当前有效支持集；失效 Citation 仍留作审计，但不出现在此数组。

## 3. Wiki

Wiki 采用 STORM 两阶段产物，而不是一段无结构长文本：

1. `outline`：按实体收集多文档视角，产出章节标题与待回答问题；
2. `write`：逐句生成 `WikiSentence`，每句单独挂 Citation。

```json
{
  "entry": {"id": "wiki-id", "entity_id": "entity-id", "title": "DeepDocParse"},
  "sections": [{
    "heading": "检索链路",
    "sentences": [{
      "id": "sentence-id",
      "text": "系统采用混合检索。",
      "evidence_ids": ["evidence-id"],
      "unsupported": false,
      "conflict_group": null,
      "review_state": "unreviewed"
    }]
  }]
}
```

没有有效 Evidence 的句子必须 `unsupported=true`；渲染器不得隐藏此标记。

## 4. 复核

复核是标注，不是编辑。允许动作仅为 `pass | reject | question | split_merge`，
并记录枚举理由、自由文本、复核人和时间。禁止人工新增无出处边或改写 wiki 正文。
被驳回或标疑的样本必须可导出到固定评测集；导出记录带数据 revision，重复导出幂等。

生成接口即使仍产出实体或 Wiki，也必须单独返回
`relation_status="not_found"` 表示本轮没有任何合法关系，不能用笼统 `status=ok`
把负样本状态藏起来。
