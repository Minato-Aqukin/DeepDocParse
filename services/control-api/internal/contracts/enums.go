// 由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。
// 改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。

package contracts

// Severity 是语义色，不是 UI 框架的颜色名 —— 映射在前端一处完成。
type Severity string

const (
	SeverityNeutral   Severity = "neutral"
	SeverityProgress  Severity = "progress"
	SeverityOk        Severity = "ok"
	SeverityWarn      Severity = "warn"
	SeverityError     Severity = "error"
)

// EnumMeta 是一个枚举取值的全部对外信息。
type EnumMeta struct {
	Value    string   `json:"value"`
	Label    string   `json:"label"`
	Severity Severity `json:"severity"`
	Active   bool     `json:"active,omitempty"`
}

// 问答 / 检索 / 抽取平面的降级原因。落在 `messages.degraded`、
// DDP-Extract 的 `degraded`、以及检索响应里。
//
// **一次只报一个**（最先命中的那个）。需要同时报多个的场合请用
// `compile_degraded` 那种列表形状，不要往这里塞逗号分隔串。
type Degraded string

const (
	// 检索一条都没命中
	DegradedNoHits Degraded = "no_hits"
	// 裁图上的文字与解析出的块文本对不上（相似度低于
	// QA_PARSE_MISMATCH_THRESHOLD / EXTRACT_MISMATCH_THRESHOLD，实测标定 0.55）。
	// 它是**假出处**的主要探测手段，不是小问题。
	DegradedParseMismatch Degraded = "parse_mismatch"
	// 向量化服务不可达，只走了关键词路。**这条是本项目吃过最大亏的地方**：
	// M4a 时向量检索静默退回 BM25，没人发现。必须可见。
	DegradedEmbeddingUnavailable Degraded = "embedding_unavailable"
	// 视觉模型不可用，本轮没做视觉核对
	DegradedVisionUnavailable Degraded = "vision_unavailable"
	// 该文件类型不支持按 bbox 裁图（例如非 PDF 原件）
	DegradedCropUnsupported Degraded = "crop_unsupported"
	// 裁图渲染失败。**注意**：依赖缺失不走这条，见 ddp_core/crops.py 的 _DEP_NOTE
	DegradedCropFailed Degraded = "crop_failed"
	// 客户端在流式回答途中断开
	DegradedClientAborted Degraded = "client_aborted"
	// 上游模型服务返回错误
	DegradedUpstreamError Degraded = "upstream_error"
	// 上游在流式输出中途断流（拿到的是半截答案）
	DegradedUpstreamInterrupted Degraded = "upstream_interrupted"
	// 回答生成期间索引 generation 变了，本轮出处已标失效
	DegradedIndexChangedDuringAnswer Degraded = "index_changed_during_answer"
	// 「这轮要不要检索」的判定模型不可用，已保守地执行检索
	DegradedDecisionUnavailable Degraded = "decision_unavailable"
	// 本轮既没检索到证据也没有可继承证据，拒绝脱离文档作答
	DegradedNoEvidenceInTurn Degraded = "no_evidence_in_turn"
	// 上一轮的证据部分失效，不能直接沿用
	DegradedInheritedEvidenceIncomplete Degraded = "inherited_evidence_incomplete"
	// 候选全部没通过逐篇质量门控（有候选但都不够格，与 no_hits 不同）
	DegradedGateRejectedAll Degraded = "gate_rejected_all"
	// 出处写库失败，相关结论已标为无证据支持
	DegradedCitationPersistFailed Degraded = "citation_persist_failed"
	// 原文自动核对没得出结论
	DegradedVerificationUnavailable Degraded = "verification_unavailable"
	// 模型输出反复不合 schema（已按 EXTRACT_MAX_RETRIES 重试仍失败）。
	// **绝不能被静默当成 not_found** —— 那会把系统故障伪装成"文档里没有"。
	DegradedSchemaViolation Degraded = "schema_violation"
	// 配了精排但上游没注册 rerank 模型，本轮没重排
	DegradedRerankUnavailable Degraded = "rerank_unavailable"
	// 注册表里只有 OCR 专用模型（`capabilities` 含 `no_instruct`），
	// 抽值无处可调。同样绝不能伪装成 not_found。
	DegradedNoInstructModel Degraded = "no_instruct_model"
	// MCP `search` 收到空查询串，直接返回空结果
	DegradedEmptyQuery Degraded = "empty_query"
	// MCP `ask` 调上游生成时非 200，本轮没有答案（证据仍然返回）
	DegradedAnswerUnavailable Degraded = "answer_unavailable"
)

// DegradedValues 保持 enums.yaml 里的声明顺序。
var DegradedValues = []Degraded{
	DegradedNoHits,
	DegradedParseMismatch,
	DegradedEmbeddingUnavailable,
	DegradedVisionUnavailable,
	DegradedCropUnsupported,
	DegradedCropFailed,
	DegradedClientAborted,
	DegradedUpstreamError,
	DegradedUpstreamInterrupted,
	DegradedIndexChangedDuringAnswer,
	DegradedDecisionUnavailable,
	DegradedNoEvidenceInTurn,
	DegradedInheritedEvidenceIncomplete,
	DegradedGateRejectedAll,
	DegradedCitationPersistFailed,
	DegradedVerificationUnavailable,
	DegradedSchemaViolation,
	DegradedRerankUnavailable,
	DegradedNoInstructModel,
	DegradedEmptyQuery,
	DegradedAnswerUnavailable,
}

var DegradedMeta = map[Degraded]EnumMeta{
	DegradedNoHits: {Value: "no_hits", Label: "未在本文档中检索到相关内容", Severity: SeverityNeutral},
	DegradedParseMismatch: {Value: "parse_mismatch", Label: "出处存疑（图上内容与解析文本对不上）", Severity: SeverityWarn},
	DegradedEmbeddingUnavailable: {Value: "embedding_unavailable", Label: "仅关键词检索（向量化服务不可用）", Severity: SeverityWarn},
	DegradedVisionUnavailable: {Value: "vision_unavailable", Label: "未做视觉验证（视觉模型不可用）", Severity: SeverityWarn},
	DegradedCropUnsupported: {Value: "crop_unsupported", Label: "未做视觉验证（该文件不支持区域截图）", Severity: SeverityNeutral},
	DegradedCropFailed: {Value: "crop_failed", Label: "未做视觉验证（区域截图失败）", Severity: SeverityWarn},
	DegradedClientAborted: {Value: "client_aborted", Label: "回答被中断", Severity: SeverityNeutral},
	DegradedUpstreamError: {Value: "upstream_error", Label: "问答服务异常", Severity: SeverityError},
	DegradedUpstreamInterrupted: {Value: "upstream_interrupted", Label: "回答生成中途断流", Severity: SeverityError},
	DegradedIndexChangedDuringAnswer: {Value: "index_changed_during_answer", Label: "回答生成期间索引版本已变化，出处已标为失效", Severity: SeverityWarn},
	DegradedDecisionUnavailable: {Value: "decision_unavailable", Label: "是否检索判定不可用，已保守执行检索", Severity: SeverityNeutral},
	DegradedNoEvidenceInTurn: {Value: "no_evidence_in_turn", Label: "本轮没有可继承证据，已拒绝脱离文档作答", Severity: SeverityWarn},
	DegradedInheritedEvidenceIncomplete: {Value: "inherited_evidence_incomplete", Label: "上一轮证据已部分失效，需重新检索后再回答", Severity: SeverityWarn},
	DegradedGateRejectedAll: {Value: "gate_rejected_all", Label: "检索候选均未通过逐篇质量门控", Severity: SeverityWarn},
	DegradedCitationPersistFailed: {Value: "citation_persist_failed", Label: "出处保存失败，相关结论已标为无证据支持", Severity: SeverityError},
	DegradedVerificationUnavailable: {Value: "verification_unavailable", Label: "原文自动核对未得出结论，请人工复核", Severity: SeverityWarn},
	DegradedSchemaViolation: {Value: "schema_violation", Label: "模型输出不符合 schema（已重试仍失败）", Severity: SeverityError},
	DegradedRerankUnavailable: {Value: "rerank_unavailable", Label: "未做精排（重排序服务不可用）", Severity: SeverityNeutral},
	DegradedNoInstructModel: {Value: "no_instruct_model", Label: "未抽取（后端没有可用的指令模型）", Severity: SeverityError},
	DegradedEmptyQuery: {Value: "empty_query", Label: "查询词为空", Severity: SeverityNeutral},
	DegradedAnswerUnavailable: {Value: "answer_unavailable", Label: "生成服务不可用（证据已返回，结论未生成）", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 degraded 取值。
func (s Degraded) Valid() bool {
	_, ok := DegradedMeta[s]
	return ok
}

// 版面编译（DDP-Compile v1）的降级。与 `degraded` 分开是因为它是
// **列表**：一次编译可以同时有好几种降级，而且它落在
// `documents.compile_degraded`（JSON 数组）上。
type CompileDegraded string

const (
	// 当前版面引擎报不出代码块
	CompileDegradedCodeDetectionUnavailable CompileDegraded = "code_detection_unavailable"
	// 部分视觉原子没有可定位的裁图
	CompileDegradedCropUnsupported CompileDegraded = "crop_unsupported"
	// 部分视觉原子裁图失败
	CompileDegradedCropFailed CompileDegraded = "crop_failed"
	// 视觉理解模型不可用
	CompileDegradedVisionUnavailable CompileDegraded = "vision_unavailable"
	// 视觉模型返回的结构不合规
	CompileDegradedVisionInvalidOutput CompileDegraded = "vision_invalid_output"
	// 上游实际模型没解析出来，本次编译版本不可比较
	CompileDegradedProviderUnresolved CompileDegraded = "provider_unresolved"
	// 存在历史出处，需先校验并人工确认后才能重建
	CompileDegradedReindexValidationRequired CompileDegraded = "reindex_validation_required"
	// 版面编译整体失败
	CompileDegradedCompileFailed CompileDegraded = "compile_failed"
)

// CompileDegradedValues 保持 enums.yaml 里的声明顺序。
var CompileDegradedValues = []CompileDegraded{
	CompileDegradedCodeDetectionUnavailable,
	CompileDegradedCropUnsupported,
	CompileDegradedCropFailed,
	CompileDegradedVisionUnavailable,
	CompileDegradedVisionInvalidOutput,
	CompileDegradedProviderUnresolved,
	CompileDegradedReindexValidationRequired,
	CompileDegradedCompileFailed,
}

var CompileDegradedMeta = map[CompileDegraded]EnumMeta{
	CompileDegradedCodeDetectionUnavailable: {Value: "code_detection_unavailable", Label: "当前版面引擎不能识别代码块", Severity: SeverityNeutral},
	CompileDegradedCropUnsupported: {Value: "crop_unsupported", Label: "部分视觉原子没有可定位裁图", Severity: SeverityNeutral},
	CompileDegradedCropFailed: {Value: "crop_failed", Label: "部分视觉原子裁图失败", Severity: SeverityWarn},
	CompileDegradedVisionUnavailable: {Value: "vision_unavailable", Label: "视觉理解模型不可用", Severity: SeverityWarn},
	CompileDegradedVisionInvalidOutput: {Value: "vision_invalid_output", Label: "视觉理解模型返回的结构不合规", Severity: SeverityWarn},
	CompileDegradedProviderUnresolved: {Value: "provider_unresolved", Label: "上游实际模型未解析，当前编译版本不可比较", Severity: SeverityWarn},
	CompileDegradedReindexValidationRequired: {Value: "reindex_validation_required", Label: "存在历史出处，需先校验并确认后重建", Severity: SeverityWarn},
	CompileDegradedCompileFailed: {Value: "compile_failed", Label: "版面编译失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 compile_degraded 取值。
func (s CompileDegraded) Valid() bool {
	_, ok := CompileDegradedMeta[s]
	return ok
}

// 解析任务状态。契约（`/v1/parse/{id}`）只承诺四态；
// `archiving` 是**产品层**多出来的一态：网关已完成但归档还没落地，
// 对用户是"还在动"。
type ParseStatus string

const (
	// 已受理，排队中
	ParseStatusPending ParseStatus = "pending"
	// 引擎正在解析
	ParseStatusRunning ParseStatus = "running"
	// 引擎已完成，产品层正在归档结果
	ParseStatusArchiving ParseStatus = "archiving"
	// 解析完成且结果已可取
	ParseStatusSucceeded ParseStatus = "succeeded"
	// 解析失败，error 里有原因
	ParseStatusFailed ParseStatus = "failed"
)

// ParseStatusValues 保持 enums.yaml 里的声明顺序。
var ParseStatusValues = []ParseStatus{
	ParseStatusPending,
	ParseStatusRunning,
	ParseStatusArchiving,
	ParseStatusSucceeded,
	ParseStatusFailed,
}

var ParseStatusMeta = map[ParseStatus]EnumMeta{
	ParseStatusPending: {Value: "pending", Label: "排队中", Severity: SeverityNeutral, Active: true},
	ParseStatusRunning: {Value: "running", Label: "解析中", Severity: SeverityProgress, Active: true},
	ParseStatusArchiving: {Value: "archiving", Label: "归档中", Severity: SeverityProgress, Active: true},
	ParseStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	ParseStatusFailed: {Value: "failed", Label: "失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 parse_status 取值。
func (s ParseStatus) Valid() bool {
	_, ok := ParseStatusMeta[s]
	return ok
}

// 向量索引状态。索引失败必须能在 UI 上看到，不许静默。
type IndexStatus string

const (
	// 还没建过索引
	IndexStatusNone IndexStatus = "none"
	// 已排队等待索引
	IndexStatusPending IndexStatus = "pending"
	// 正在建索引
	IndexStatusIndexing IndexStatus = "indexing"
	// 索引可用，可以问答
	IndexStatusReady IndexStatus = "ready"
	// 索引失败，index_error 里有原因
	IndexStatusFailed IndexStatus = "failed"
)

// IndexStatusValues 保持 enums.yaml 里的声明顺序。
var IndexStatusValues = []IndexStatus{
	IndexStatusNone,
	IndexStatusPending,
	IndexStatusIndexing,
	IndexStatusReady,
	IndexStatusFailed,
}

var IndexStatusMeta = map[IndexStatus]EnumMeta{
	IndexStatusNone: {Value: "none", Label: "未索引", Severity: SeverityNeutral},
	IndexStatusPending: {Value: "pending", Label: "待索引", Severity: SeverityNeutral, Active: true},
	IndexStatusIndexing: {Value: "indexing", Label: "索引中", Severity: SeverityProgress, Active: true},
	IndexStatusReady: {Value: "ready", Label: "可问答", Severity: SeverityOk},
	IndexStatusFailed: {Value: "failed", Label: "索引失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 index_status 取值。
func (s IndexStatus) Valid() bool {
	_, ok := IndexStatusMeta[s]
	return ok
}

// 版面编译状态。**索引 ready 不代表视觉理解完整** —— 编译状态与降级
// 必须单列并在前端展示。
type CompileStatus string

const (
	// 还没编译
	CompileStatusNone CompileStatus = "none"
	// 已排队等待编译
	CompileStatusPending CompileStatus = "pending"
	// 正在编译
	CompileStatusCompiling CompileStatus = "compiling"
	// 编译完整、无降级
	CompileStatusReady CompileStatus = "ready"
	// 编译完成但有降级，见 compile_degraded
	CompileStatusPartial CompileStatus = "partial"
	// 编译失败
	CompileStatusFailed CompileStatus = "failed"
)

// CompileStatusValues 保持 enums.yaml 里的声明顺序。
var CompileStatusValues = []CompileStatus{
	CompileStatusNone,
	CompileStatusPending,
	CompileStatusCompiling,
	CompileStatusReady,
	CompileStatusPartial,
	CompileStatusFailed,
}

var CompileStatusMeta = map[CompileStatus]EnumMeta{
	CompileStatusNone: {Value: "none", Label: "未编译", Severity: SeverityNeutral},
	CompileStatusPending: {Value: "pending", Label: "待编译", Severity: SeverityNeutral, Active: true},
	CompileStatusCompiling: {Value: "compiling", Label: "编译中", Severity: SeverityProgress, Active: true},
	CompileStatusReady: {Value: "ready", Label: "编译完整", Severity: SeverityOk},
	CompileStatusPartial: {Value: "partial", Label: "编译有降级", Severity: SeverityWarn},
	CompileStatusFailed: {Value: "failed", Label: "编译失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 compile_status 取值。
func (s CompileStatus) Valid() bool {
	_, ok := CompileStatusMeta[s]
	return ok
}

// 抽取批次状态。`partial` **不是"有点问题"的委婉说法**：它明确表示
// 必填字段没抽全，或批次里个别文档失败。一批 200 份里有 3 份失败
// 报成"成功"，会让人直接拿去用。
type RunStatus string

const (
	// 已受理，排队中
	RunStatusPending RunStatus = "pending"
	// 正在抽取
	RunStatusRunning RunStatus = "running"
	// 全部文档全部字段都完成
	RunStatusSucceeded RunStatus = "succeeded"
	// 部分文档或部分字段失败
	RunStatusPartial RunStatus = "partial"
	// 整批失败
	RunStatusFailed RunStatus = "failed"
)

// RunStatusValues 保持 enums.yaml 里的声明顺序。
var RunStatusValues = []RunStatus{
	RunStatusPending,
	RunStatusRunning,
	RunStatusSucceeded,
	RunStatusPartial,
	RunStatusFailed,
}

var RunStatusMeta = map[RunStatus]EnumMeta{
	RunStatusPending: {Value: "pending", Label: "排队中", Severity: SeverityNeutral, Active: true},
	RunStatusRunning: {Value: "running", Label: "抽取中", Severity: SeverityProgress, Active: true},
	RunStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	RunStatusPartial: {Value: "partial", Label: "部分完成", Severity: SeverityWarn},
	RunStatusFailed: {Value: "failed", Label: "失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 run_status 取值。
func (s RunStatus) Valid() bool {
	_, ok := RunStatusMeta[s]
	return ok
}

// DDP-Extract 的字段三态。**必须分开对待**：`not_found` 是"我们看过了，
// 文档里确实没有"，是一种正确答案；`error` 才是系统问题。
// 界面上 not_found 绝不能显示成空白或 "—"（那让人以为是没渲染出来），
// error 也绝不能显示成"未提及"（那是把系统故障伪装成事实）。
type FieldStatus string

const (
	// 抽到了值，且有出处
	FieldStatusFound FieldStatus = "found"
	// 文档里确实没有这个字段
	FieldStatusNotFound FieldStatus = "not_found"
	// 抽取过程本身出错
	FieldStatusError FieldStatus = "error"
)

// FieldStatusValues 保持 enums.yaml 里的声明顺序。
var FieldStatusValues = []FieldStatus{
	FieldStatusFound,
	FieldStatusNotFound,
	FieldStatusError,
}

var FieldStatusMeta = map[FieldStatus]EnumMeta{
	FieldStatusFound: {Value: "found", Label: "已抽取", Severity: SeverityOk},
	FieldStatusNotFound: {Value: "not_found", Label: "文档中未提及", Severity: SeverityNeutral},
	FieldStatusError: {Value: "error", Label: "抽取失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 field_status 取值。
func (s FieldStatus) Valid() bool {
	_, ok := FieldStatusMeta[s]
	return ok
}

// 代码块识别的来源。启发式与原生要分开，因为它决定了代码检索的可信度。
type CodeDetection string

const (
	// 版面引擎直接报出了 code 块
	CodeDetectionNative CodeDetection = "native"
	// 靠启发式规则判出来的
	CodeDetectionHeuristic CodeDetection = "heuristic"
	// 当前引擎识别不了代码块
	CodeDetectionUnavailable CodeDetection = "unavailable"
)

// CodeDetectionValues 保持 enums.yaml 里的声明顺序。
var CodeDetectionValues = []CodeDetection{
	CodeDetectionNative,
	CodeDetectionHeuristic,
	CodeDetectionUnavailable,
}

var CodeDetectionMeta = map[CodeDetection]EnumMeta{
	CodeDetectionNative: {Value: "native", Label: "代码识别：原生", Severity: SeverityOk},
	CodeDetectionHeuristic: {Value: "heuristic", Label: "代码识别：启发式", Severity: SeverityNeutral},
	CodeDetectionUnavailable: {Value: "unavailable", Label: "代码识别：不可用", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 code_detection 取值。
func (s CodeDetection) Valid() bool {
	_, ok := CodeDetectionMeta[s]
	return ok
}

// 证据是原文还是生成物。**第三条不变式**：生成物与原文必须可区分，
// 且生成物的引用最终仍要指回原始原子 bbox（`derived_from`）。
// 判据是 `evidence.derived_from` 是否为空 —— 不要在别处另立标志位。
type SourceType string

const (
	// 直接来自版面的原子（derived_from 为空）
	SourceTypeSource SourceType = "source"
	// 模型生成的理解（derived_from 指向原子）
	SourceTypeGenerated SourceType = "generated"
)

// SourceTypeValues 保持 enums.yaml 里的声明顺序。
var SourceTypeValues = []SourceType{
	SourceTypeSource,
	SourceTypeGenerated,
}

var SourceTypeMeta = map[SourceType]EnumMeta{
	SourceTypeSource: {Value: "source", Label: "原文", Severity: SeverityNeutral},
	SourceTypeGenerated: {Value: "generated", Label: "生成理解", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 source_type 取值。
func (s SourceType) Valid() bool {
	_, ok := SourceTypeMeta[s]
	return ok
}

// DDP-Layout v1.1 的块类型词汇表 —— **契约的一部分**。
// 每个引擎的 normalizer 都必须产出这八个值之一；认不出来的归 `other`
// （不是丢弃 —— 丢弃会让新引擎的块凭空消失）。
// 规范实现在 `ddp_core.blocks.normalize_type`，守卫在
// `scripts/check_blocktype_parity.py`。
type BlockType string

const (
	// 正文段落。也是"压根没有 type"时的默认
	BlockTypeText BlockType = "text"
	// 各级标题
	BlockTypeTitle BlockType = "title"
	// 代码块
	BlockTypeCode BlockType = "code"
	// 表格（table_html 可能有值）
	BlockTypeTable BlockType = "table"
	// 图。**无 caption 也要产出原子**，否则视觉链路没输入
	BlockTypeFigure BlockType = "figure"
	// 行间公式
	BlockTypeEquation BlockType = "equation"
	// 列表
	BlockTypeList BlockType = "list"
	// 有 type 但不在映射表里 —— 与「压根没有 type」要分开，后者归 text
	BlockTypeOther BlockType = "other"
)

// BlockTypeValues 保持 enums.yaml 里的声明顺序。
var BlockTypeValues = []BlockType{
	BlockTypeText,
	BlockTypeTitle,
	BlockTypeCode,
	BlockTypeTable,
	BlockTypeFigure,
	BlockTypeEquation,
	BlockTypeList,
	BlockTypeOther,
}

var BlockTypeMeta = map[BlockType]EnumMeta{
	BlockTypeText: {Value: "text", Label: "正文", Severity: SeverityNeutral},
	BlockTypeTitle: {Value: "title", Label: "标题", Severity: SeverityNeutral},
	BlockTypeCode: {Value: "code", Label: "代码", Severity: SeverityNeutral},
	BlockTypeTable: {Value: "table", Label: "表格", Severity: SeverityNeutral},
	BlockTypeFigure: {Value: "figure", Label: "图", Severity: SeverityNeutral},
	BlockTypeEquation: {Value: "equation", Label: "公式", Severity: SeverityNeutral},
	BlockTypeList: {Value: "list", Label: "列表", Severity: SeverityNeutral},
	BlockTypeOther: {Value: "other", Label: "其它", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 block_type 取值。
func (s BlockType) Valid() bool {
	_, ok := BlockTypeMeta[s]
	return ok
}

// 计量流水的种类。`extract` 按**字段数**计 requests：一次抽取 = N 次检索
// + N 次模型调用，按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜。
type UsageKind string

const (
	// 文档解析，按页计
	UsageKindParse UsageKind = "parse"
	// 对外 chat 代理，按次计
	UsageKindChat UsageKind = "chat"
	// 对外向量化代理
	UsageKindEmbeddings UsageKind = "embeddings"
	// MCP 工具调用
	UsageKindMcp UsageKind = "mcp"
	// 站内问答
	UsageKindQa UsageKind = "qa"
	// 索引时的向量化
	UsageKindEmbed UsageKind = "embed"
	// 编译期的视觉理解调用
	UsageKindCompileVision UsageKind = "compile_vision"
	// 结构化抽取，按字段数计
	UsageKindExtract UsageKind = "extract"
)

// UsageKindValues 保持 enums.yaml 里的声明顺序。
var UsageKindValues = []UsageKind{
	UsageKindParse,
	UsageKindChat,
	UsageKindEmbeddings,
	UsageKindMcp,
	UsageKindQa,
	UsageKindEmbed,
	UsageKindCompileVision,
	UsageKindExtract,
}

var UsageKindMeta = map[UsageKind]EnumMeta{
	UsageKindParse: {Value: "parse", Label: "解析", Severity: SeverityNeutral},
	UsageKindChat: {Value: "chat", Label: "对话", Severity: SeverityNeutral},
	UsageKindEmbeddings: {Value: "embeddings", Label: "向量化", Severity: SeverityNeutral},
	UsageKindMcp: {Value: "mcp", Label: "MCP 调用", Severity: SeverityNeutral},
	UsageKindQa: {Value: "qa", Label: "问答", Severity: SeverityNeutral},
	UsageKindEmbed: {Value: "embed", Label: "索引向量化", Severity: SeverityNeutral},
	UsageKindCompileVision: {Value: "compile_vision", Label: "视觉理解", Severity: SeverityNeutral},
	UsageKindExtract: {Value: "extract", Label: "结构化抽取", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 usage_kind 取值。
func (s UsageKind) Valid() bool {
	_, ok := UsageKindMeta[s]
	return ok
}

// 调用者身份类型。corpus-api **不自己验用户凭据**，它只信任 control-api
// 在内部调用里下发的 `X-DDP-Actor-Kind` + `X-DDP-Actor`。
type ActorKind string

const (
	// 浏览器会话（JWT / OIDC）
	ActorKindUser ActorKind = "user"
	// sk- 开头的对外 key
	ActorKindApiKey ActorKind = "api_key"
	// 服务间调用（服务凭据）
	ActorKindService ActorKind = "service"
)

// ActorKindValues 保持 enums.yaml 里的声明顺序。
var ActorKindValues = []ActorKind{
	ActorKindUser,
	ActorKindApiKey,
	ActorKindService,
}

var ActorKindMeta = map[ActorKind]EnumMeta{
	ActorKindUser: {Value: "user", Label: "用户", Severity: SeverityNeutral},
	ActorKindApiKey: {Value: "api_key", Label: "API Key", Severity: SeverityNeutral},
	ActorKindService: {Value: "service", Label: "服务", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 actor_kind 取值。
func (s ActorKind) Valid() bool {
	_, ok := ActorKindMeta[s]
	return ok
}

// 组织内角色（RBAC）。**首发是单组织独占部署**，一次部署 = 一份语料，
// 组织内成员共享语料；角色控制的是"能做什么"，不是"能看见什么"。
type Role string

const (
	// 只读：检索、问答、看证据
	RoleViewer Role = "viewer"
	// viewer + 上传、重解析、发起抽取
	RoleContributor Role = "contributor"
	// contributor + 复核队列、确认/驳回知识条目
	RoleReviewer Role = "reviewer"
	// 全部 + 成员管理、API key、配额、删除
	RoleAdmin Role = "admin"
)

// RoleValues 保持 enums.yaml 里的声明顺序。
var RoleValues = []Role{
	RoleViewer,
	RoleContributor,
	RoleReviewer,
	RoleAdmin,
}

var RoleMeta = map[Role]EnumMeta{
	RoleViewer: {Value: "viewer", Label: "只读成员", Severity: SeverityNeutral},
	RoleContributor: {Value: "contributor", Label: "贡献者", Severity: SeverityNeutral},
	RoleReviewer: {Value: "reviewer", Label: "复核员", Severity: SeverityNeutral},
	RoleAdmin: {Value: "admin", Label: "管理员", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 role 取值。
func (s Role) Valid() bool {
	_, ok := RoleMeta[s]
	return ok
}

// 持久任务的状态机（§10）。**领取必须带 generation fencing**：
// lease 只解决"谁可以接管"，最终写入还要比 generation —— 否则被判死的
// 旧 worker 迟到写入会覆盖新结果。
type TaskStatus string

const (
	// 已落库等待领取
	TaskStatusQueued TaskStatus = "queued"
	// 已被某个 worker 领取（带 lease_until）
	TaskStatusClaimed TaskStatus = "claimed"
	// 正在执行，靠 heartbeat 续租
	TaskStatusRunning TaskStatus = "running"
	// 完成
	TaskStatusSucceeded TaskStatus = "succeeded"
	// 失败，失败原因必须持久化并在 UI 可见
	TaskStatusFailed TaskStatus = "failed"
)

// TaskStatusValues 保持 enums.yaml 里的声明顺序。
var TaskStatusValues = []TaskStatus{
	TaskStatusQueued,
	TaskStatusClaimed,
	TaskStatusRunning,
	TaskStatusSucceeded,
	TaskStatusFailed,
}

var TaskStatusMeta = map[TaskStatus]EnumMeta{
	TaskStatusQueued: {Value: "queued", Label: "排队中", Severity: SeverityNeutral, Active: true},
	TaskStatusClaimed: {Value: "claimed", Label: "已领取", Severity: SeverityProgress, Active: true},
	TaskStatusRunning: {Value: "running", Label: "执行中", Severity: SeverityProgress, Active: true},
	TaskStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	TaskStatusFailed: {Value: "failed", Label: "失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 task_status 取值。
func (s TaskStatus) Valid() bool {
	_, ok := TaskStatusMeta[s]
	return ok
}

// 持久任务的种类。每种**分别设并发与队列**，不共用一个无量纲总并发。
type TaskKind string

const (
	// 轮询解析引擎并归档结果
	TaskKindParsePoll TaskKind = "parse_poll"
	// 版面编译（含视觉理解）
	TaskKindCompile TaskKind = "compile"
	// 分块 + 向量化 + 写索引
	TaskKindIndex TaskKind = "index"
	// 结构化抽取批次
	TaskKindExtract TaskKind = "extract"
	// 图谱 / wiki 生成
	TaskKindKnowledge TaskKind = "knowledge"
	// 对象回收（带宽限期）
	TaskKindGc TaskKind = "gc"
)

// TaskKindValues 保持 enums.yaml 里的声明顺序。
var TaskKindValues = []TaskKind{
	TaskKindParsePoll,
	TaskKindCompile,
	TaskKindIndex,
	TaskKindExtract,
	TaskKindKnowledge,
	TaskKindGc,
}

var TaskKindMeta = map[TaskKind]EnumMeta{
	TaskKindParsePoll: {Value: "parse_poll", Label: "解析归档", Severity: SeverityNeutral},
	TaskKindCompile: {Value: "compile", Label: "版面编译", Severity: SeverityNeutral},
	TaskKindIndex: {Value: "index", Label: "建立索引", Severity: SeverityNeutral},
	TaskKindExtract: {Value: "extract", Label: "结构化抽取", Severity: SeverityNeutral},
	TaskKindKnowledge: {Value: "knowledge", Label: "知识生成", Severity: SeverityNeutral},
	TaskKindGc: {Value: "gc", Label: "对象回收", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 task_kind 取值。
func (s TaskKind) Valid() bool {
	_, ok := TaskKindMeta[s]
	return ok
}

// 直传上传会话的状态（§9.1）。**`verifying` 不能跳过**：服务端没校验完
// 对象大小与摘要之前，文档不得进入解析 —— 否则等于信任客户端声明的哈希。
type UploadStatus string

const (
	// 会话已创建，预签名已下发
	UploadStatusCreated UploadStatus = "created"
	// 客户端正在分片上传
	UploadStatusUploading UploadStatus = "uploading"
	// 已 finalize，服务端正在校验摘要
	UploadStatusVerifying UploadStatus = "verifying"
	// 校验通过，已发出 DocumentSubmitted
	UploadStatusReady UploadStatus = "ready"
	// 校验失败或客户端放弃
	UploadStatusFailed UploadStatus = "failed"
	// 预签名过期未完成
	UploadStatusExpired UploadStatus = "expired"
)

// UploadStatusValues 保持 enums.yaml 里的声明顺序。
var UploadStatusValues = []UploadStatus{
	UploadStatusCreated,
	UploadStatusUploading,
	UploadStatusVerifying,
	UploadStatusReady,
	UploadStatusFailed,
	UploadStatusExpired,
}

var UploadStatusMeta = map[UploadStatus]EnumMeta{
	UploadStatusCreated: {Value: "created", Label: "待上传", Severity: SeverityNeutral, Active: true},
	UploadStatusUploading: {Value: "uploading", Label: "上传中", Severity: SeverityProgress, Active: true},
	UploadStatusVerifying: {Value: "verifying", Label: "校验中", Severity: SeverityProgress, Active: true},
	UploadStatusReady: {Value: "ready", Label: "已就绪", Severity: SeverityOk},
	UploadStatusFailed: {Value: "failed", Label: "失败", Severity: SeverityError},
	UploadStatusExpired: {Value: "expired", Label: "已过期", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 upload_status 取值。
func (s UploadStatus) Valid() bool {
	_, ok := UploadStatusMeta[s]
	return ok
}
