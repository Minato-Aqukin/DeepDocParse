# DDP-Agent v1

DDP-Agent 是语料问答六段链路的内部契约。它把“是否检索、为什么丢候选、哪一句由
哪条证据支持、谁核对过”从 prompt 约定升格为可持久化、可评测的类型。

## 1. QueryDecision

```json
{
  "need_retrieval": false,
  "reason": "follow_up_with_evidence",
  "inherited_evidence_ids": ["evidence-id"],
  "retrieval_queries": [],
  "degraded": null
}
```

`need_retrieval=false` 只允许用于追问、澄清、改写、格式转换或解释已有引用，并且
`inherited_evidence_ids` 必须非空。否则本轮必须拒答，降级码为
`no_evidence_in_turn`。模型判定不可用时必须保守回退到检索并标
`decision_unavailable`；不允许回退到模型常识作答。
中心判定调用可同时产生最多3条、每条不超过512字符的 `retrieval_queries`：
仅翻译或聚焦原问题中的明确子问题，不补答案、数值或新的任务；中文问题可生成英文检索词，
以支持英文技术资料。原问题始终参与同一有界候选合并，版本/授权门控、最终上下文预算不变。
这些检索表达随消息元数据持久化，并在 QueryDecision 的 SSE meta 与历史消息投影中返回，
不作为证据或主张；未记录过的历史轮次省略该字段，不事后猜测或重建。
判定提示可使用当前授权固定版本的文件名消解指代，但文件名不充当事实证据。
判定关闭时不做模型改写；判定输出格式不合法时显式 `decision_unavailable`，只检索原问题。


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
启用交叉编码器精排时，种子选择直接采用精排名次截断到同样的种子数：混合召回的
名次已经在候选池形成时投过一票，种子选择是交叉编码器的一票，不再把两路名次做
第二次 RRF 融合——融合会把精排前列但混合居中的答案块平均挤出固定名额
（已知开发回归 M3-D03 落选“of 194 languages”先行词与“more than 100”块）。
名额、字符预算、邻块规则与降级口径都不变。
中心回答可在剩余上下文字符预算内补充同文档、同解析任务、同物理页且 `seq±1`
的原子证据；同一原文几何区域内的续块可沿已接纳邻块再补一跳
（标题→正文首块→同区续块），总邻块数仍不超过种子数的二倍且字符预算不变；
不替换、重排已选命中，不跨版本/权限边界拼接正文，不补跨区域无关段落。
邻块保持独立 evidence ID、页码和bbox，`score=0 / similarity=null`，审计原因
`adjacent_context_of:<seed-evidence-id>`；邻接不是相关性分数或语义验证。
每个邻块必须完整装入剩余预算，候选审计与最终真实引用仍分开。
中心问答先按检索排名完成字符预算选择，再把**已经选中**的模型输入片段按物理页与
`fragment_seq` 呈现为原文阅读顺序；不因此增减候选、改变预算或覆盖候选排名。
模型提示要求只回答请求的事实、联合阅读相邻片段，并引用支持主语、限定范围及单位的
必要片段：主张中的每个命名实体、数值、单位、比较基线与条件都必须出现在所引片段中，
一行/一节/一主体的资料不能为另一行/节/主体作证；缺答说明逐条限定在所供资料范围内，
不扩张为整篇文档不存在；归因到被请求出处位置（如摘要）的主张必须引用该位置的片段，
不用他节复述代替。顺序与提示约束不是语义验证：数值、引用支持和子问题覆盖仍需逐项复核。



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

回答的规范形态是 `Assertion[]`，不再以整段字符串作为语义真相。Web、本机及联邦问答模型
共用 `ddp_core.answer` 的受约束 JSON 输出（grounded-claims/2 生成器线协议）。
模型只写 `claims` 与 `unanswered`，不产生 `status` 键：

```json
{"claims":[{"text":"设备的额定电压是 220 V。","evidence_ids":["evidence-id"]}],"unanswered":[]}
{"claims":[],"unanswered":["现有证据不足以确定保修年限。"]}
```

- `claims` 与 `unanswered` 都是必填数组，至少一个非空：全可答（claims 非空、
  unanswered 为空）、部分可答（两者都非空）、整题无依据（claims 为空、unanswered
  非空）。两个数组都空是非法空回答；缺数组、未知/重复键（旧版 `status` 亦然，
  不做兼容）、空/越界引用、截断输出都是 `schema_violation`。
- 每条 claim 必须显式给出非空、属于本次可见上下文的 `evidence_ids`。正文里的
  `[1]` 等文本不产生引用，也不能把整组候选自动挂到回答上。
- 复合问题的可答部分放入 `claims`；缺证据的子问题逐条放入 `unanswered` 纯字符串，
  明确命名该子问题并说明现有证据不足。这是缺答说明，不是事实主张，不接受引用字段；
  正文不得用常识填补缺项。每条非空说明受主张文本长度上限约束，与事实主张合计最多64项；
  只有完整文档校验后才投影为无引用、`unsupported=true` 的展示项，不随流式中途输出。
  结构检查不保证缺项划分或语义正确，仍需按完整问题复核。
- 每条完整 claim 校验后才输出文本增量；引用绑定直接持久化，不再按标点重新分句或
  从自由文本猜来源。原始 JSON 不暴露给前端。
- 整题无依据是正常拒答，不是空回答：解码器在 `finish` 完整校验成功后从"事实主张
  为空"派生内部 `insufficient_evidence`，并返回全部显式缺答说明作为
  `unsupported=true` 的展示断言；没有可用证据时不调用回答模型。拒答不附引用、
  不继承视觉核对结果。
- 合法部分回答不等于整题拒答：中心仍消费事实主张对应原始证据的视觉核对，
  持久化并返回逐条核对记录。发现不一致继续报告 `parse_mismatch`，核对不可用
  继续保留降级；无引用的缺答项不继承核对结果，整条消息保持 `verified=false`。
- 生成器线协议的"状态"是派生值，不是模型字段：历史 v1 的 `status` 键已废除。
  公共回答/模型元数据里的 `insufficient_evidence`、`evidence_sufficiency`、
  `answer_reason` 等取值保持不变，它们描述的是解码后结论，不是线协议输入。
- 非法 JSON、缺失或越界 ID、空引用、截断输出是 `schema_violation`，不能冒充
  “文档中没有”。已输出的完整断言可作为显式标记失败的部分回答保留。
- 历史自由文本断言与其引用保持不变；新生成不再兼容自由文本模型输出。
- 联邦调用者显式启用矛盾能力时，有事实主张的回答可带
  `conflicts: [{"evidence_ids": ["id-a", "id-b"]}]`；每组至少两条不同且本次提供的证据
  ID，每个 ID 必须被至少一条回答 claim 引用。矛盾的双方必须作为普通 claims 各自说明，
  不选择一方；内容是否确实说明双方仍需人审。元数据不产生额外主张。
  事实主张为空时不得带矛盾。单中心 schema 与解码默认不启用此能力。
- 联邦执行者必须完整解码后才采用答案：任一违规整份拒收为 `unsupported_generation`。
  C→A 的 `FederatedAnswer` 形状不变：以已解码 claims 投影带编号引用的 answer 文本、
  `claim_evidence_bindings` 与 `generation_reported` conflicts；所有语义核对仍需人审。
  当前联邦 `FederatedAnswer` 没有部分缺答投影：遇到非空 `unanswered` 明确返回
  `answer_reason=insufficient_evidence` 并保留检索证据，不把部分生成误当完整联邦答案。
- 本机私有回答与会话使用同一解码入口。供应方请求携带本次可见 evidence ID 的
  `grounded_answer_schema`；只有整份文档完成校验才返回或持久化，不采纳失败前的部分主张。
  本机非法模型输出沿用 `unsupported_generation`；不能把协议错误记成正常拒答。
- 本机私有回答的 `evidence_sufficiency` 来自完整解码后的派生状态，不由“候选非空”推断：
  事实主张为空对应 `insufficient`，返回全部显式缺答说明作为无引用、
  `unsupported=true` 的展示断言，主引用为空；检索候选仍独立保留。
  对合法部分回答也记 `insufficient`，但保留已有事实主张及显式缺答项；
  会话不得因充分性不足而覆盖掉合法可答部分，也不得在解码器已返回显式缺答项后
  再拼接一条通用拒答造成重复。
  状态写入消息 `model_meta.evidence_sufficiency` 并在 SSE `meta.retrieval` 返回。
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
