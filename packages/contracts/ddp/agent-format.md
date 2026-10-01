# DDP-Agent v1

DDP-Agent 是语料问答六段链路的内部契约。它把“是否检索、为什么丢候选、哪一句由
哪条证据支持、谁核对过”从 prompt 约定升格为可持久化、可评测的类型。

## 1. QueryDecision

```json
{
  "need_retrieval": false,
  "reason": "follow_up_with_evidence",
  "inherited_evidence_ids": ["evidence-id"],
  "degraded": null
}
```

`need_retrieval=false` 只允许用于追问、澄清、改写、格式转换或解释已有引用，并且
`inherited_evidence_ids` 必须非空。否则本轮必须拒答，降级码为
`no_evidence_in_turn`。模型判定不可用时必须保守回退到检索并标
`decision_unavailable`；不允许回退到模型常识作答。

继承集合必须**完整可解析**。任一 ID 因重建、删除或版本漂移失效时，本轮拒答并标
`inherited_evidence_incomplete`；不能静默丢掉失效项后拿缩减过的上下文继续生成。

## 2. CandidateDecision

```json
{
  "evidence_id": "evidence-id",
  "document_id": "document-id",
  "rank": 0,
  "score": 0.032787,
  "similarity": 0.73,
  "accepted": true,
  "reason": "document_gate_passed"
}
```

候选按文档分组门控。每篇文档先看其最高可校准相似度；未过阈值则该文档候选全部
保留为 `accepted=false / document_below_similarity`，而不是从运行记录中消失。
向量不可用时只走关键词路，候选可进入回答，但必须继承
`embedding_unavailable` 降级。候选记录不可冒充 Citation；只有被断言实际引用的证据
才写 primary Citation。

## 3. Assertion

```json
{
  "id": "assertion-id",
  "position": 0,
  "text": "设备的额定电压是 220 V。",
  "evidence_ids": ["evidence-id"],
  "verification": {"state": "passed", "mode": "auto"},
  "unsupported": false
}
```

回答的规范形态是 `Assertion[]`，不再以整段字符串作为语义真相。Web 与联邦问答模型
共用 `ddp_core.answer` 的受约束 JSON 输出，二选一：

```json
{"status":"answered","claims":[{"text":"设备的额定电压是 220 V。","evidence_ids":["evidence-id"]}]}
{"status":"insufficient_evidence"}
```

- `answered` 必须有非空 claims；每条 claim 必须显式给出非空、属于本次可见上下文的
  `evidence_ids`。正文里的 `[1]` 等文本不产生引用，也不能把整组候选自动挂到回答上。
- 每条完整 claim 校验后才输出文本增量；引用绑定直接持久化，不再按标点重新分句或
  从自由文本猜来源。原始 JSON 不暴露给前端。
- `insufficient_evidence` 是正常拒答；没有可用证据时不调用回答模型。拒答不附引用、
  不继承视觉核对结果，其展示断言必须 `unsupported=true`。
- 非法 JSON、缺失或越界 ID、空引用、截断输出是 `schema_violation`，不能冒充
  “文档中没有”。已输出的完整断言可作为显式标记失败的部分回答保留。
- 历史自由文本断言与其引用保持不变；新生成不再兼容自由文本模型输出。
- 联邦调用者显式启用矛盾能力时，`answered` 可带
  `conflicts: [{"evidence_ids": ["id-a", "id-b"]}]`；每组至少两条不同且本次提供的证据
  ID，每个 ID 必须被至少一条回答 claim 引用。矛盾的双方必须作为普通 claims 各自说明，
  不选择一方；内容是否确实说明双方仍需人审。元数据不产生额外主张。
  `insufficient_evidence` 不得带矛盾。单中心 schema 与解码默认不启用此能力。
- 联邦执行者必须完整解码后才采用答案：任一违规整份拒收为 `unsupported_generation`。
  C→A 的 `FederatedAnswer` 形状不变：以已解码 claims 投影带编号引用的 answer 文本、
  `claim_evidence_bindings` 与 `generation_reported` conflicts；所有语义核对仍需人审。
- 展示用 `Message.content` 是 Assertion.text 的有序投影，只为兼容旧客户端。
- Citation 的 `source_kind` 为 `assertion`，`source_id` 指向断言主键。

## 4. Verification

```json
{
  "id": "verification-id",
  "evidence_id": "evidence-id",
  "mode": "human",
  "verdict": "question",
  "reason_code": "hard_to_read",
  "reason_text": "原件扫描模糊",
  "created_at": "2026-08-28T00:00:00Z"
}
```

自动核对与人工核对写入同一张 verification 表，`mode` 只能是 `auto|human`，
`verdict` 只能是 `pass|reject|question`。人工操作只允许通过、驳回、标疑并记录理由；
不允许编辑 Evidence、Assertion 内容或 bbox。人工结果同步写回
`Evidence.review_state = passed|rejected|questioned`，自动结果保留审计记录但不覆盖人工状态。

## 5. SSE 与历史读取

流式问答保留 `meta/delta/citations/done/error`，新增：

- `meta.query_decision`：是否检索、理由、是否继承证据。
- `meta.retrieval.candidates`：完整门控结果，含 rejected 与理由。
- `assertions`：持久化后的 `Assertion[]`；每条断言内嵌自己的 Citation 输出。

历史消息必须返回同样的 `assertions`。迁移前的 assistant message 回填成一条断言，
原 message Citation 改挂该断言；无法支持的历史文本显式 `unsupported=true`。

## 6. 证据预览四层

点击 Assertion Citation 后必须能展开：

1. 文档：文件名与 document id；
2. 页：1-based 页码与整页原件；
3. 块：稳定 `(parse_job_id, seq)` 与原文块；
4. 原子：Evidence kind、source/generated 身份、bbox、裁图与核对记录。

整页左栏叠 2px cite bbox；右栏裁图以原始像素 `1 CSS px : 1 image px` 展示，容器溢出
时滚动，不得用 `object-fit: cover/contain` 缩放冒充 1:1。

## 7. 降级码

| code | 含义 |
|---|---|
| `decision_unavailable` | 是否检索判定不可用，已保守执行检索 |
| `no_evidence_in_turn` | 判定不检索但无可继承证据，已拒答 |
| `inherited_evidence_incomplete` | 上一轮只有部分证据仍有效，已拒答并要求重新检索 |
| `gate_rejected_all` | 检索有候选但逐篇门控全部拒绝 |
| `citation_persist_failed` | 断言引用未能完整持久化，断言已强制标 unsupported |
| `schema_violation` | 回答模型未满足逐条证据绑定协议；仅保留此前已校验的完整断言 |

既有检索、视觉、解析与索引降级码继续有效。多个降级同时发生时，对外 `degraded`
保留对结论风险更高的一项，完整链路原因同时保存在 QueryDecision/CandidateDecision 中。
