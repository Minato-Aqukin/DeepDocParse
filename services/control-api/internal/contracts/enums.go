// 由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。
// 改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。

package contracts

// Severity 是语义色，不是 UI 框架的颜色名 —— 映射在前端一处完成。
type Severity string

const (
	SeverityNeutral  Severity = "neutral"
	SeverityProgress Severity = "progress"
	SeverityOk       Severity = "ok"
	SeverityWarn     Severity = "warn"
	SeverityError    Severity = "error"
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
	// 授权资源的固定解析版本尚无可用索引
	DegradedResourceIndexUnavailable Degraded = "resource_index_unavailable"
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
	DegradedResourceIndexUnavailable,
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
	DegradedNoHits:                      {Value: "no_hits", Label: "未在本文档中检索到相关内容", Severity: SeverityNeutral},
	DegradedParseMismatch:               {Value: "parse_mismatch", Label: "出处存疑（图上内容与解析文本对不上）", Severity: SeverityWarn},
	DegradedResourceIndexUnavailable:    {Value: "resource_index_unavailable", Label: "该资源版本索引尚不可用，请查看解析任务", Severity: SeverityWarn},
	DegradedEmbeddingUnavailable:        {Value: "embedding_unavailable", Label: "仅关键词检索（向量化服务不可用）", Severity: SeverityWarn},
	DegradedVisionUnavailable:           {Value: "vision_unavailable", Label: "未做视觉验证（视觉模型不可用）", Severity: SeverityWarn},
	DegradedCropUnsupported:             {Value: "crop_unsupported", Label: "未做视觉验证（该文件不支持区域截图）", Severity: SeverityNeutral},
	DegradedCropFailed:                  {Value: "crop_failed", Label: "未做视觉验证（区域截图失败）", Severity: SeverityWarn},
	DegradedClientAborted:               {Value: "client_aborted", Label: "回答被中断", Severity: SeverityNeutral},
	DegradedUpstreamError:               {Value: "upstream_error", Label: "问答服务异常", Severity: SeverityError},
	DegradedUpstreamInterrupted:         {Value: "upstream_interrupted", Label: "回答生成中途断流", Severity: SeverityError},
	DegradedIndexChangedDuringAnswer:    {Value: "index_changed_during_answer", Label: "回答生成期间索引版本已变化，出处已标为失效", Severity: SeverityWarn},
	DegradedDecisionUnavailable:         {Value: "decision_unavailable", Label: "是否检索判定不可用，已保守执行检索", Severity: SeverityNeutral},
	DegradedNoEvidenceInTurn:            {Value: "no_evidence_in_turn", Label: "本轮没有可继承证据，已拒绝脱离文档作答", Severity: SeverityWarn},
	DegradedInheritedEvidenceIncomplete: {Value: "inherited_evidence_incomplete", Label: "上一轮证据已部分失效，需重新检索后再回答", Severity: SeverityWarn},
	DegradedGateRejectedAll:             {Value: "gate_rejected_all", Label: "检索候选均未通过逐篇质量门控", Severity: SeverityWarn},
	DegradedCitationPersistFailed:       {Value: "citation_persist_failed", Label: "出处保存失败，相关结论已标为无证据支持", Severity: SeverityError},
	DegradedVerificationUnavailable:     {Value: "verification_unavailable", Label: "原文自动核对未得出结论，请人工复核", Severity: SeverityWarn},
	DegradedSchemaViolation:             {Value: "schema_violation", Label: "模型输出不符合 schema（已重试仍失败）", Severity: SeverityError},
	DegradedRerankUnavailable:           {Value: "rerank_unavailable", Label: "未做精排（重排序服务不可用）", Severity: SeverityNeutral},
	DegradedNoInstructModel:             {Value: "no_instruct_model", Label: "未抽取（后端没有可用的指令模型）", Severity: SeverityError},
	DegradedEmptyQuery:                  {Value: "empty_query", Label: "查询词为空", Severity: SeverityNeutral},
	DegradedAnswerUnavailable:           {Value: "answer_unavailable", Label: "生成服务不可用（证据已返回，结论未生成）", Severity: SeverityError},
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
	CompileDegradedCodeDetectionUnavailable:  {Value: "code_detection_unavailable", Label: "当前版面引擎不能识别代码块", Severity: SeverityNeutral},
	CompileDegradedCropUnsupported:           {Value: "crop_unsupported", Label: "部分视觉原子没有可定位裁图", Severity: SeverityNeutral},
	CompileDegradedCropFailed:                {Value: "crop_failed", Label: "部分视觉原子裁图失败", Severity: SeverityWarn},
	CompileDegradedVisionUnavailable:         {Value: "vision_unavailable", Label: "视觉理解模型不可用", Severity: SeverityWarn},
	CompileDegradedVisionInvalidOutput:       {Value: "vision_invalid_output", Label: "视觉理解模型返回的结构不合规", Severity: SeverityWarn},
	CompileDegradedProviderUnresolved:        {Value: "provider_unresolved", Label: "上游实际模型未解析，当前编译版本不可比较", Severity: SeverityWarn},
	CompileDegradedReindexValidationRequired: {Value: "reindex_validation_required", Label: "存在历史出处，需先校验并确认后重建", Severity: SeverityWarn},
	CompileDegradedCompileFailed:             {Value: "compile_failed", Label: "版面编译失败", Severity: SeverityError},
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
	ParseStatusPending:   {Value: "pending", Label: "排队中", Severity: SeverityNeutral, Active: true},
	ParseStatusRunning:   {Value: "running", Label: "解析中", Severity: SeverityProgress, Active: true},
	ParseStatusArchiving: {Value: "archiving", Label: "归档中", Severity: SeverityProgress, Active: true},
	ParseStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	ParseStatusFailed:    {Value: "failed", Label: "失败", Severity: SeverityError},
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
	IndexStatusNone:     {Value: "none", Label: "未索引", Severity: SeverityNeutral},
	IndexStatusPending:  {Value: "pending", Label: "待索引", Severity: SeverityNeutral, Active: true},
	IndexStatusIndexing: {Value: "indexing", Label: "索引中", Severity: SeverityProgress, Active: true},
	IndexStatusReady:    {Value: "ready", Label: "可问答", Severity: SeverityOk},
	IndexStatusFailed:   {Value: "failed", Label: "索引失败", Severity: SeverityError},
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
	CompileStatusNone:      {Value: "none", Label: "未编译", Severity: SeverityNeutral},
	CompileStatusPending:   {Value: "pending", Label: "待编译", Severity: SeverityNeutral, Active: true},
	CompileStatusCompiling: {Value: "compiling", Label: "编译中", Severity: SeverityProgress, Active: true},
	CompileStatusReady:     {Value: "ready", Label: "编译完整", Severity: SeverityOk},
	CompileStatusPartial:   {Value: "partial", Label: "编译有降级", Severity: SeverityWarn},
	CompileStatusFailed:    {Value: "failed", Label: "编译失败", Severity: SeverityError},
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
	RunStatusPending:   {Value: "pending", Label: "排队中", Severity: SeverityNeutral, Active: true},
	RunStatusRunning:   {Value: "running", Label: "抽取中", Severity: SeverityProgress, Active: true},
	RunStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	RunStatusPartial:   {Value: "partial", Label: "部分完成", Severity: SeverityWarn},
	RunStatusFailed:    {Value: "failed", Label: "失败", Severity: SeverityError},
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
	FieldStatusFound:    {Value: "found", Label: "已抽取", Severity: SeverityOk},
	FieldStatusNotFound: {Value: "not_found", Label: "文档中未提及", Severity: SeverityNeutral},
	FieldStatusError:    {Value: "error", Label: "抽取失败", Severity: SeverityError},
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
	CodeDetectionNative:      {Value: "native", Label: "代码识别：原生", Severity: SeverityOk},
	CodeDetectionHeuristic:   {Value: "heuristic", Label: "代码识别：启发式", Severity: SeverityNeutral},
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
	SourceTypeSource:    {Value: "source", Label: "原文", Severity: SeverityNeutral},
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
	BlockTypeText:     {Value: "text", Label: "正文", Severity: SeverityNeutral},
	BlockTypeTitle:    {Value: "title", Label: "标题", Severity: SeverityNeutral},
	BlockTypeCode:     {Value: "code", Label: "代码", Severity: SeverityNeutral},
	BlockTypeTable:    {Value: "table", Label: "表格", Severity: SeverityNeutral},
	BlockTypeFigure:   {Value: "figure", Label: "图", Severity: SeverityNeutral},
	BlockTypeEquation: {Value: "equation", Label: "公式", Severity: SeverityNeutral},
	BlockTypeList:     {Value: "list", Label: "列表", Severity: SeverityNeutral},
	BlockTypeOther:    {Value: "other", Label: "其它", Severity: SeverityNeutral},
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
	// 图谱 / wiki 生成，按次计
	UsageKindKnowledge UsageKind = "knowledge"
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
	UsageKindKnowledge,
}

var UsageKindMeta = map[UsageKind]EnumMeta{
	UsageKindParse:         {Value: "parse", Label: "解析", Severity: SeverityNeutral},
	UsageKindChat:          {Value: "chat", Label: "对话", Severity: SeverityNeutral},
	UsageKindEmbeddings:    {Value: "embeddings", Label: "向量化", Severity: SeverityNeutral},
	UsageKindMcp:           {Value: "mcp", Label: "MCP 调用", Severity: SeverityNeutral},
	UsageKindQa:            {Value: "qa", Label: "问答", Severity: SeverityNeutral},
	UsageKindEmbed:         {Value: "embed", Label: "索引向量化", Severity: SeverityNeutral},
	UsageKindCompileVision: {Value: "compile_vision", Label: "视觉理解", Severity: SeverityNeutral},
	UsageKindExtract:       {Value: "extract", Label: "结构化抽取", Severity: SeverityNeutral},
	UsageKindKnowledge:     {Value: "knowledge", Label: "知识生成", Severity: SeverityNeutral},
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
	ActorKindUser:    {Value: "user", Label: "用户", Severity: SeverityNeutral},
	ActorKindApiKey:  {Value: "api_key", Label: "API Key", Severity: SeverityNeutral},
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
	RoleViewer:      {Value: "viewer", Label: "只读成员", Severity: SeverityNeutral},
	RoleContributor: {Value: "contributor", Label: "贡献者", Severity: SeverityNeutral},
	RoleReviewer:    {Value: "reviewer", Label: "复核员", Severity: SeverityNeutral},
	RoleAdmin:       {Value: "admin", Label: "管理员", Severity: SeverityNeutral},
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
	// 被显式取消。**终态，迟到的成功/失败写入一律被 generation + 状态守卫拒绝**。
	// 与 failed 分开是因为"用户不想要了"和"系统做砸了"对用户是两件事：
	// 前者不该进失败告警，后者必须留失败原因。
	TaskStatusCancelled TaskStatus = "cancelled"
)

// TaskStatusValues 保持 enums.yaml 里的声明顺序。
var TaskStatusValues = []TaskStatus{
	TaskStatusQueued,
	TaskStatusClaimed,
	TaskStatusRunning,
	TaskStatusSucceeded,
	TaskStatusFailed,
	TaskStatusCancelled,
}

var TaskStatusMeta = map[TaskStatus]EnumMeta{
	TaskStatusQueued:    {Value: "queued", Label: "排队中", Severity: SeverityNeutral, Active: true},
	TaskStatusClaimed:   {Value: "claimed", Label: "已领取", Severity: SeverityProgress, Active: true},
	TaskStatusRunning:   {Value: "running", Label: "执行中", Severity: SeverityProgress, Active: true},
	TaskStatusSucceeded: {Value: "succeeded", Label: "已完成", Severity: SeverityOk},
	TaskStatusFailed:    {Value: "failed", Label: "失败", Severity: SeverityError},
	TaskStatusCancelled: {Value: "cancelled", Label: "已取消", Severity: SeverityWarn},
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
	// 联邦节点侧的单步执行（`federation.execute`）。受理与执行行先提交、
	// 再排这个任务 —— 进程重启后由别的 worker 按租约接管，已受理的执行
	// 不会永远停在 queued/running（不变式 7）。
	TaskKindFederationExecute TaskKind = "federation_execute"
	// 联邦协调者推进一个已批准计划（`federation_tasks._execute_plan`）。
	// 与节点侧分开成两种任务，协调者等待本地执行时不会占满执行池
	// （否则单池会被"等子任务的父任务"堵死）。
	TaskKindFederationPlan TaskKind = "federation_plan"
)

// TaskKindValues 保持 enums.yaml 里的声明顺序。
var TaskKindValues = []TaskKind{
	TaskKindParsePoll,
	TaskKindCompile,
	TaskKindIndex,
	TaskKindExtract,
	TaskKindKnowledge,
	TaskKindGc,
	TaskKindFederationExecute,
	TaskKindFederationPlan,
}

var TaskKindMeta = map[TaskKind]EnumMeta{
	TaskKindParsePoll:         {Value: "parse_poll", Label: "解析归档", Severity: SeverityNeutral},
	TaskKindCompile:           {Value: "compile", Label: "版面编译", Severity: SeverityNeutral},
	TaskKindIndex:             {Value: "index", Label: "建立索引", Severity: SeverityNeutral},
	TaskKindExtract:           {Value: "extract", Label: "结构化抽取", Severity: SeverityNeutral},
	TaskKindKnowledge:         {Value: "knowledge", Label: "知识生成", Severity: SeverityNeutral},
	TaskKindGc:                {Value: "gc", Label: "对象回收", Severity: SeverityNeutral},
	TaskKindFederationExecute: {Value: "federation_execute", Label: "联邦执行", Severity: SeverityNeutral},
	TaskKindFederationPlan:    {Value: "federation_plan", Label: "联邦计划执行", Severity: SeverityNeutral},
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
	UploadStatusCreated:   {Value: "created", Label: "待上传", Severity: SeverityNeutral, Active: true},
	UploadStatusUploading: {Value: "uploading", Label: "上传中", Severity: SeverityProgress, Active: true},
	UploadStatusVerifying: {Value: "verifying", Label: "校验中", Severity: SeverityProgress, Active: true},
	UploadStatusReady:     {Value: "ready", Label: "已就绪", Severity: SeverityOk},
	UploadStatusFailed:    {Value: "failed", Label: "失败", Severity: SeverityError},
	UploadStatusExpired:   {Value: "expired", Label: "已过期", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 upload_status 取值。
func (s UploadStatus) Valid() bool {
	_, ok := UploadStatusMeta[s]
	return ok
}

// `ScopeManifest` 的成员枚举状态（计划 §5.4）。**这是「查了哪里」这句话
// 的分母**：分母没封上就没有百分比可言。
//
// `partial` 与 `expired` 必须与 `sealed` 严格分开：把无法展开的子域
// 当成空目录，等于用"那里没有资料"冒充"我没能去看"。
type EnumerationState string

const (
	// 正在逐个目录取分页快照，还没封存
	EnumerationStateBuilding EnumerationState = "building"
	// 全部获准目录都取到稳定快照且已去重封存，可重放。
	// **只有这个值允许后续声明 retrieval=complete。**
	EnumerationStateSealed EnumerationState = "sealed"
	// 有子目录超时、拒绝或不支持枚举。未展开子域记在
	// `unexpanded_subtrees[]`，**不得当成空集**，也不得给出真实总数。
	EnumerationStatePartial EnumerationState = "partial"
	// 快照有效期已过或枚举游标失效。不能把不同分页时代的列表拼成
	// "完整快照"（§5.5）—— 要重新枚举生成新 scope。
	EnumerationStateExpired EnumerationState = "expired"
)

// EnumerationStateValues 保持 enums.yaml 里的声明顺序。
var EnumerationStateValues = []EnumerationState{
	EnumerationStateBuilding,
	EnumerationStateSealed,
	EnumerationStatePartial,
	EnumerationStateExpired,
}

var EnumerationStateMeta = map[EnumerationState]EnumMeta{
	EnumerationStateBuilding: {Value: "building", Label: "正在确定检索范围", Severity: SeverityProgress, Active: true},
	EnumerationStateSealed:   {Value: "sealed", Label: "检索范围已确定", Severity: SeverityOk},
	EnumerationStatePartial:  {Value: "partial", Label: "检索范围不完整（部分下级目录无法展开）", Severity: SeverityWarn},
	EnumerationStateExpired:  {Value: "expired", Label: "检索范围已过期，需重新确定", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 enumeration_state 取值。
func (s EnumerationState) Valid() bool {
	_, ok := EnumerationStateMeta[s]
	return ok
}

// 覆盖账本里**单个目标**的状态（计划 §7.4）。目标键是
// `(origin_node_id, collection_id, operation)` —— 一台服务器有多个集合时，
// 探测了其中一个**不能**把整台标成完成（计划 T85）。
//
// `unsupported` 可以结束对该目标的发现处理，但**它不表示在那里完成了
// 全文检索**；报告时它进"排除数"，不进"成功检索数"。
type CoverageTargetState string

const (
	// 已进入本次范围，尚未发出请求
	CoverageTargetStatePlanned CoverageTargetState = "planned"
	// 请求已发出，还没有回执
	CoverageTargetStateInFlight CoverageTargetState = "in_flight"
	// 拿到有效且完成的检索回执
	CoverageTargetStateSucceeded CoverageTargetState = "succeeded"
	// 目标自己报了内部限制（分片失败、索引落后、只查了子集）。
	// **算缺口，不算完成** —— 节点外层写 completed 而内部有 partial
	// 是计划 §6.4 明确禁止的。
	CoverageTargetStatePartial CoverageTargetState = "partial"
	// 鉴权通过但该目标拒绝本次操作
	CoverageTargetStateDenied CoverageTargetState = "denied"
	// 请求出错（非超时）
	CoverageTargetStateFailed CoverageTargetState = "failed"
	// 已核实该目标不支持所需 operation。**只有可核验依据才能记这个值** ——
	// 能力元数据过期或缺失一律算 unknown/未完成，不得直接排除（§7.3）。
	CoverageTargetStateUnsupported CoverageTargetState = "unsupported"
	// 超时或连不上
	CoverageTargetStateUnreachable CoverageTargetState = "unreachable"
	// 预算耗尽 / 任务取消 / 范围过期导致压根没发出。**不是"没有资料"**
	CoverageTargetStateNotAttempted CoverageTargetState = "not_attempted"
	// 成员在范围封存后被撤销。**留在分母里**（§5.5）——
	// 从分母删掉来把完成率做漂亮是明确禁止的。
	CoverageTargetStateRevoked CoverageTargetState = "revoked"
)

// CoverageTargetStateValues 保持 enums.yaml 里的声明顺序。
var CoverageTargetStateValues = []CoverageTargetState{
	CoverageTargetStatePlanned,
	CoverageTargetStateInFlight,
	CoverageTargetStateSucceeded,
	CoverageTargetStatePartial,
	CoverageTargetStateDenied,
	CoverageTargetStateFailed,
	CoverageTargetStateUnsupported,
	CoverageTargetStateUnreachable,
	CoverageTargetStateNotAttempted,
	CoverageTargetStateRevoked,
}

var CoverageTargetStateMeta = map[CoverageTargetState]EnumMeta{
	CoverageTargetStatePlanned:      {Value: "planned", Label: "待检索", Severity: SeverityNeutral, Active: true},
	CoverageTargetStateInFlight:     {Value: "in_flight", Label: "检索中", Severity: SeverityProgress, Active: true},
	CoverageTargetStateSucceeded:    {Value: "succeeded", Label: "已检索", Severity: SeverityOk},
	CoverageTargetStatePartial:      {Value: "partial", Label: "部分检索（对方报告内部不完整）", Severity: SeverityWarn},
	CoverageTargetStateDenied:       {Value: "denied", Label: "对方拒绝", Severity: SeverityWarn},
	CoverageTargetStateFailed:       {Value: "failed", Label: "检索失败", Severity: SeverityError},
	CoverageTargetStateUnsupported:  {Value: "unsupported", Label: "对方不支持该操作", Severity: SeverityNeutral},
	CoverageTargetStateUnreachable:  {Value: "unreachable", Label: "无法连接", Severity: SeverityError},
	CoverageTargetStateNotAttempted: {Value: "not_attempted", Label: "未检索（预算或取消）", Severity: SeverityWarn},
	CoverageTargetStateRevoked:      {Value: "revoked", Label: "成员已撤销（保留在范围内）", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 coverage_target_state 取值。
func (s CoverageTargetState) Valid() bool {
	_, ok := CoverageTargetStateMeta[s]
	return ok
}

// 整个任务的检索完成度（计划 §7.4）。
//
// `complete` 的判据是**合取**，缺一条都不许写：
//
//	① `enumeration_state == sealed` 且无未展开子域；
//	② 所有适用且已授权的目标都返回有效、完成的回执；
//	③ 没有 in_flight / not_attempted / unreachable / denied / revoked，
//	   也没有任何目标自报 partial；
//	④ 每个被排除的目标都有可核验依据。
//
// 快速模式**永远不允许**写 complete（§7.2）：它只完成了自己选中的候选，
// 所以它报的是 `partial` 加上"未检索范围"。
type RetrievalCompleteness string

const (
	// 范围还没封存或还没开始检索
	RetrievalCompletenessNotStarted RetrievalCompleteness = "not_started"
	// 有目标未完成，或本轮是 fast 模式。**fast 模式的成功结局也是这个值**
	// —— 它必须同时给出未检索范围，不能因为选中的候选全成功就报完成。
	RetrievalCompletenessPartial RetrievalCompleteness = "partial"
	// 上述四条合取全部成立。**这仍然不代表证据充分或结论正确。**
	RetrievalCompletenessComplete RetrievalCompleteness = "complete"
)

// RetrievalCompletenessValues 保持 enums.yaml 里的声明顺序。
var RetrievalCompletenessValues = []RetrievalCompleteness{
	RetrievalCompletenessNotStarted,
	RetrievalCompletenessPartial,
	RetrievalCompletenessComplete,
}

var RetrievalCompletenessMeta = map[RetrievalCompleteness]EnumMeta{
	RetrievalCompletenessNotStarted: {Value: "not_started", Label: "尚未检索", Severity: SeverityNeutral, Active: true},
	RetrievalCompletenessPartial:    {Value: "partial", Label: "部分范围已检索", Severity: SeverityWarn},
	RetrievalCompletenessComplete:   {Value: "complete", Label: "声明范围内已全部检索", Severity: SeverityOk},
}

// Valid 报告 s 是不是一个已知的 retrieval_completeness 取值。
func (s RetrievalCompleteness) Valid() bool {
	_, ok := RetrievalCompletenessMeta[s]
	return ok
}

// 证据充分性（计划 §7.4 第三轴）。与检索完成度**严格分开**：
// 「该查的都查了」和「查到的够回答」是两件事，而
// 「够回答」和「答对了」又是第三件事（那一件靠人工评审，不进这个枚举）。
//
// `conflicting` 不是 `insufficient` 的变体：矛盾证据意味着拿到了实质内容
// 但来源互相打架，界面上要让用户看见冲突，而不是折叠成"资料不足"。
type EvidenceSufficiency string

const (
	// 按当次策略判定证据足够。名字里的 `by_policy` 是刻意的 ——
	// 它是**按规则判的**，不是"客观上充分"，更不是 LLM 自报信心
	// （§7.2 明确禁止把自报信心当唯一早停条件）。
	EvidenceSufficiencySufficientByPolicy EvidenceSufficiency = "sufficient_by_policy"
	// 没有足够证据支撑结论，必须如实说不足
	EvidenceSufficiencyInsufficient EvidenceSufficiency = "insufficient"
	// 多来源证据互相矛盾（含同一资料的不同版本）。要展示冲突，不要挑一个。优先级低于 insufficient / unknown：证据本身不足时报不足，矛盾记录照样保留
	EvidenceSufficiencyConflicting EvidenceSufficiency = "conflicting"
	// 还没评估（检索未完成 / 评估器不可用）
	EvidenceSufficiencyUnknown EvidenceSufficiency = "unknown"
)

// EvidenceSufficiencyValues 保持 enums.yaml 里的声明顺序。
var EvidenceSufficiencyValues = []EvidenceSufficiency{
	EvidenceSufficiencySufficientByPolicy,
	EvidenceSufficiencyInsufficient,
	EvidenceSufficiencyConflicting,
	EvidenceSufficiencyUnknown,
}

var EvidenceSufficiencyMeta = map[EvidenceSufficiency]EnumMeta{
	EvidenceSufficiencySufficientByPolicy: {Value: "sufficient_by_policy", Label: "证据满足本次策略要求", Severity: SeverityOk},
	EvidenceSufficiencyInsufficient:       {Value: "insufficient", Label: "证据不足", Severity: SeverityWarn},
	EvidenceSufficiencyConflicting:        {Value: "conflicting", Label: "证据存在矛盾", Severity: SeverityWarn},
	EvidenceSufficiencyUnknown:            {Value: "unknown", Label: "证据充分性未知", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 evidence_sufficiency 取值。
func (s EvidenceSufficiency) Valid() bool {
	_, ok := EvidenceSufficiencyMeta[s]
	return ok
}

// 一条证据矛盾记录**凭什么**成立（计划 §7.6：同时处理矛盾证据，不挑一个）。
// 两种依据都只能把 `sufficient_by_policy` 压成 `conflicting`，**不能**抬高它，
// 也**不能**把 `insufficient` / `unknown` 改写成 `conflicting`（那会藏掉"证据不足"
// 并放行不该发生的生成）—— 这两种情况下矛盾记录照样保留、照样可见。
// 模型说"有矛盾"最多让界面多一个警告，而模型说"没矛盾"不改变任何东西。
// 每条记录都要人看（`semantic_review=needs_review`），这里记的是"值得复核的
// 矛盾"，不是裁决。
type EvidenceConflictBasis string

const (
	// 规则判定：同一来源（同节点、同资源）的不同固定版本在**同一定位**
	// （物理页 + 块序）上取回了不同正文。只看结构，不读语义。
	EvidenceConflictBasisVersionDivergence EvidenceConflictBasis = "version_divergence"
	// 带出处生成时模型标出的矛盾引用对。只在引用全部落在本次证据编号域、
	// 且至少指向两条不同证据时才采信；引用不成立则整份答案作废。
	EvidenceConflictBasisGenerationReported EvidenceConflictBasis = "generation_reported"
)

// EvidenceConflictBasisValues 保持 enums.yaml 里的声明顺序。
var EvidenceConflictBasisValues = []EvidenceConflictBasis{
	EvidenceConflictBasisVersionDivergence,
	EvidenceConflictBasisGenerationReported,
}

var EvidenceConflictBasisMeta = map[EvidenceConflictBasis]EnumMeta{
	EvidenceConflictBasisVersionDivergence:  {Value: "version_divergence", Label: "同一资料的版本不一致", Severity: SeverityWarn},
	EvidenceConflictBasisGenerationReported: {Value: "generation_reported", Label: "生成时标出的矛盾", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 evidence_conflict_basis 取值。
func (s EvidenceConflictBasis) Valid() bool {
	_, ok := EvidenceConflictBasisMeta[s]
	return ok
}

// 联邦任务结果里 `answer_reason` 的取值：**为什么这次没有答案**（有答案时为 null）。
// 它和 `evidence_sufficiency` 分开：充分性说"证据够不够"，这里说"答案这一步
// 具体卡在哪" —— 证据充分但模型没装、远端答案引用越界、预算超了，都要让用户
// 看得出区别，而不是统一显示成"没有答案"。
//
// **代码是封闭集合，细节是后缀**：其中五个代码允许带 `:细节`
// （`receipt_binding_mismatch:plan_digest`、`delegated_execution_failed:peer_execution_timeout`、
// `delegated_admission_not_accepted:waiting_input`、`delegated_answer_rejected:…`、
// `peer_unavailable:http_503`），细节是对端的状态/错误码或出错字段，经过字符集与长度
// 清洗；其余代码不带后缀。对端给的字符串**永远只进细节**，不会变成新的代码。
// 查文案前先去掉冒号后缀；协调者写出之前会检查代码已声明（`federation.unavailable_answer`）。
type FederatedAnswerReason string

const (
	// 证据不足（或没有可引用证据），不给模型凭常识补答的机会
	FederatedAnswerReasonInsufficientEvidence FederatedAnswerReason = "insufficient_evidence"
	// 本节点与计划内远端都没有可用的生成能力（或生成预算为 0）
	FederatedAnswerReasonLocalModelMissing FederatedAnswerReason = "local_model_missing"
	// 某条证据取不到正文（空白或缺失），不能拿无根片段生成
	FederatedAnswerReasonEvidenceExcerptUnavailable FederatedAnswerReason = "evidence_excerpt_unavailable"
	// 证据正文超过契约上限（2000 字符），显式拒绝而不是静默截断
	FederatedAnswerReasonExcerptOverContractBound FederatedAnswerReason = "excerpt_over_contract_bound"
	// 调生成模型的请求失败或返回非 200
	FederatedAnswerReasonUpstreamError FederatedAnswerReason = "upstream_error"
	// 模型没有返回可用文本
	FederatedAnswerReasonNoModelOutput FederatedAnswerReason = "no_model_output"
	// 生成结果超出计划的生成 token 预算
	FederatedAnswerReasonBudgetExceeded FederatedAnswerReason = "budget_exceeded"
	// 生成文本的引用结构不成立（无引用、越界引用、矛盾标注不成立）
	FederatedAnswerReasonUnsupportedGeneration FederatedAnswerReason = "unsupported_generation"
	// 远端执行完成但没有返回答案文档
	FederatedAnswerReasonDelegatedAnswerMissing FederatedAnswerReason = "delegated_answer_missing"
	// 远端答案校验未通过；远端自报的原因认不出来时放进细节
	FederatedAnswerReasonDelegatedAnswerRejected FederatedAnswerReason = "delegated_answer_rejected"
	// 远端答案没有任何主张绑定
	FederatedAnswerReasonDelegatedBindingsMissing FederatedAnswerReason = "delegated_bindings_missing"
	// 远端答案的引用不在本次发送的证据里
	FederatedAnswerReasonDelegatedBindingOutOfScope FederatedAnswerReason = "delegated_binding_out_of_scope"
	// 远端标出的矛盾引用不在本次发送的证据里
	FederatedAnswerReasonDelegatedConflictOutOfScope FederatedAnswerReason = "delegated_conflict_out_of_scope"
	// 要委托的证据条数超过受理上限，不截断证据去凑数
	FederatedAnswerReasonEvidenceDelegationOverLimit FederatedAnswerReason = "evidence_delegation_over_limit"
	// 远端受理回执缺执行任务号
	FederatedAnswerReasonInvalidAdmissionReceipt FederatedAnswerReason = "invalid_admission_receipt"
	// 远端回执与本次 root/step/幂等键/计划修订/执行者对不上；细节是出错字段（回执不是对象时为 schema）
	FederatedAnswerReasonReceiptBindingMismatch FederatedAnswerReason = "receipt_binding_mismatch"
	// 远端没有受理答案步骤；细节是回执状态（如 waiting_input / rejected）
	FederatedAnswerReasonDelegatedAdmissionNotAccepted FederatedAnswerReason = "delegated_admission_not_accepted"
	// 远端答案执行没有成功；细节是对端错误码或状态（含本节点轮询超时 peer_execution_timeout）
	FederatedAnswerReasonDelegatedExecutionFailed FederatedAnswerReason = "delegated_execution_failed"
	// 远端生成节点未登记、连不上、回 HTTP 错误或返回非法响应；细节是对端错误码、http_状态或 transport
	FederatedAnswerReasonPeerUnavailable FederatedAnswerReason = "peer_unavailable"
)

// FederatedAnswerReasonValues 保持 enums.yaml 里的声明顺序。
var FederatedAnswerReasonValues = []FederatedAnswerReason{
	FederatedAnswerReasonInsufficientEvidence,
	FederatedAnswerReasonLocalModelMissing,
	FederatedAnswerReasonEvidenceExcerptUnavailable,
	FederatedAnswerReasonExcerptOverContractBound,
	FederatedAnswerReasonUpstreamError,
	FederatedAnswerReasonNoModelOutput,
	FederatedAnswerReasonBudgetExceeded,
	FederatedAnswerReasonUnsupportedGeneration,
	FederatedAnswerReasonDelegatedAnswerMissing,
	FederatedAnswerReasonDelegatedAnswerRejected,
	FederatedAnswerReasonDelegatedBindingsMissing,
	FederatedAnswerReasonDelegatedBindingOutOfScope,
	FederatedAnswerReasonDelegatedConflictOutOfScope,
	FederatedAnswerReasonEvidenceDelegationOverLimit,
	FederatedAnswerReasonInvalidAdmissionReceipt,
	FederatedAnswerReasonReceiptBindingMismatch,
	FederatedAnswerReasonDelegatedAdmissionNotAccepted,
	FederatedAnswerReasonDelegatedExecutionFailed,
	FederatedAnswerReasonPeerUnavailable,
}

var FederatedAnswerReasonMeta = map[FederatedAnswerReason]EnumMeta{
	FederatedAnswerReasonInsufficientEvidence:          {Value: "insufficient_evidence", Label: "证据不足，未生成答案", Severity: SeverityWarn},
	FederatedAnswerReasonLocalModelMissing:             {Value: "local_model_missing", Label: "没有可用的生成模型，只返回证据", Severity: SeverityWarn},
	FederatedAnswerReasonEvidenceExcerptUnavailable:    {Value: "evidence_excerpt_unavailable", Label: "有证据取不到原文片段，未生成答案", Severity: SeverityWarn},
	FederatedAnswerReasonExcerptOverContractBound:      {Value: "excerpt_over_contract_bound", Label: "证据片段超出长度上限，未生成答案", Severity: SeverityWarn},
	FederatedAnswerReasonUpstreamError:                 {Value: "upstream_error", Label: "生成服务出错，只返回证据", Severity: SeverityError},
	FederatedAnswerReasonNoModelOutput:                 {Value: "no_model_output", Label: "模型没有输出，只返回证据", Severity: SeverityError},
	FederatedAnswerReasonBudgetExceeded:                {Value: "budget_exceeded", Label: "超出生成预算，答案作废", Severity: SeverityError},
	FederatedAnswerReasonUnsupportedGeneration:         {Value: "unsupported_generation", Label: "生成的答案引用不成立，已作废", Severity: SeverityError},
	FederatedAnswerReasonDelegatedAnswerMissing:        {Value: "delegated_answer_missing", Label: "远端没有返回答案", Severity: SeverityError},
	FederatedAnswerReasonDelegatedAnswerRejected:       {Value: "delegated_answer_rejected", Label: "远端答案未通过校验", Severity: SeverityError},
	FederatedAnswerReasonDelegatedBindingsMissing:      {Value: "delegated_bindings_missing", Label: "远端答案没有引用，已作废", Severity: SeverityError},
	FederatedAnswerReasonDelegatedBindingOutOfScope:    {Value: "delegated_binding_out_of_scope", Label: "远端答案引用了未发送的证据，已作废", Severity: SeverityError},
	FederatedAnswerReasonDelegatedConflictOutOfScope:   {Value: "delegated_conflict_out_of_scope", Label: "远端标注的矛盾引用不成立，答案已作废", Severity: SeverityError},
	FederatedAnswerReasonEvidenceDelegationOverLimit:   {Value: "evidence_delegation_over_limit", Label: "证据条数超过委托上限，未生成答案", Severity: SeverityWarn},
	FederatedAnswerReasonInvalidAdmissionReceipt:       {Value: "invalid_admission_receipt", Label: "远端受理回执无效", Severity: SeverityError},
	FederatedAnswerReasonReceiptBindingMismatch:        {Value: "receipt_binding_mismatch", Label: "远端回执与本次任务对不上，未采用", Severity: SeverityError},
	FederatedAnswerReasonDelegatedAdmissionNotAccepted: {Value: "delegated_admission_not_accepted", Label: "远端未受理生成请求", Severity: SeverityError},
	FederatedAnswerReasonDelegatedExecutionFailed:      {Value: "delegated_execution_failed", Label: "远端生成步骤未完成", Severity: SeverityError},
	FederatedAnswerReasonPeerUnavailable:               {Value: "peer_unavailable", Label: "远端生成节点不可用", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 federated_answer_reason 取值。
func (s FederatedAnswerReason) Valid() bool {
	_, ok := FederatedAnswerReasonMeta[s]
	return ok
}

// 协调者入口（`POST /api/v1/task-intents`）**受理哪些 TaskSpec.operation**。
// 这是一个闭集：认不出来的 operation 当场拒绝，不许落库。
//
// 为什么必须闭集：规划只按 operation 决定要不要加生成步骤。以前不看 operation，
// 本地模型就绪时**任何** operation 都会被追加一个 `answer` 步 —— 提交
// `corpus.retrieve`（只取证据）会白跑一次生成，提交一个没实现的 operation
// （例如 `wiki.pages`）会拿回一个 RAG 答案。那是静默错义，不是报错。
//
// 本地运行时的 TaskSpec 还有别的 operation（本机自己的计划许可），不受这里约束。
type FederationTaskOperation string

const (
	// 只按范围取证据，不生成结论
	FederationTaskOperationCorpusRetrieve FederationTaskOperation = "corpus.retrieve"
	// 取证据并生成带出处的回答
	FederationTaskOperationRagAnswerCited FederationTaskOperation = "rag.answer.cited"
)

// FederationTaskOperationValues 保持 enums.yaml 里的声明顺序。
var FederationTaskOperationValues = []FederationTaskOperation{
	FederationTaskOperationCorpusRetrieve,
	FederationTaskOperationRagAnswerCited,
}

var FederationTaskOperationMeta = map[FederationTaskOperation]EnumMeta{
	FederationTaskOperationCorpusRetrieve: {Value: "corpus.retrieve", Label: "只取证据", Severity: SeverityNeutral},
	FederationTaskOperationRagAnswerCited: {Value: "rag.answer.cited", Label: "带出处的回答", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 federation_task_operation 取值。
func (s FederationTaskOperation) Valid() bool {
	_, ok := FederationTaskOperationMeta[s]
	return ok
}

// 联邦任务事件流（`GET /api/v1/tasks/{root_task_id}/events`）里 `Event.type` 的取值。
// 事件是**可恢复的进度记录**，不是状态真相：状态以 `TaskStatus` 各轴为准，事件用来
// 让界面说清"发生了什么、什么时候"，断线后按 `after=next_seq` 续读不丢。
type TaskEventType string

const (
	// 任务需求与探索许可已落库
	TaskEventTypeIntentCreated TaskEventType = "intent_created"
	// 规划完成（Probe 与计划修订已生成），等待批准
	TaskEventTypePlanReady TaskEventType = "plan_ready"
	// 用户批准了这一修订与执行许可
	TaskEventTypePlanApproved TaskEventType = "plan_approved"
	// 执行已受理并排入持久队列
	TaskEventTypeExecutionStarted TaskEventType = "execution_started"
	// 重新判权后补做未完成目标（执行代次 +1）
	TaskEventTypeTaskResumed TaskEventType = "task_resumed"
	// 执行结束且至少有目标产出证据（查全与否看覆盖账本）
	TaskEventTypeTaskCompleted TaskEventType = "task_completed"
	// 执行失败（没有任何目标产出证据，或协调者被清扫）
	TaskEventTypeTaskFailed TaskEventType = "task_failed"
	// 用户显式取消；终态，迟到结果不许覆盖
	TaskEventTypeTaskCancelled TaskEventType = "task_cancelled"
	// 结果已固化为交付文档，等待下载后校验确认
	TaskEventTypeDeliveryPending TaskEventType = "delivery_pending"
	// 客户端校验摘要后确认了交付
	TaskEventTypeDeliveryConfirmed TaskEventType = "delivery_confirmed"
	// 交付在有效期内没有被确认
	TaskEventTypeDeliveryExpired TaskEventType = "delivery_expired"
)

// TaskEventTypeValues 保持 enums.yaml 里的声明顺序。
var TaskEventTypeValues = []TaskEventType{
	TaskEventTypeIntentCreated,
	TaskEventTypePlanReady,
	TaskEventTypePlanApproved,
	TaskEventTypeExecutionStarted,
	TaskEventTypeTaskResumed,
	TaskEventTypeTaskCompleted,
	TaskEventTypeTaskFailed,
	TaskEventTypeTaskCancelled,
	TaskEventTypeDeliveryPending,
	TaskEventTypeDeliveryConfirmed,
	TaskEventTypeDeliveryExpired,
}

var TaskEventTypeMeta = map[TaskEventType]EnumMeta{
	TaskEventTypeIntentCreated:     {Value: "intent_created", Label: "已创建任务", Severity: SeverityNeutral},
	TaskEventTypePlanReady:         {Value: "plan_ready", Label: "计划已生成，等待批准", Severity: SeverityNeutral},
	TaskEventTypePlanApproved:      {Value: "plan_approved", Label: "已批准计划", Severity: SeverityOk},
	TaskEventTypeExecutionStarted:  {Value: "execution_started", Label: "开始执行", Severity: SeverityProgress},
	TaskEventTypeTaskResumed:       {Value: "task_resumed", Label: "补做未完成目标", Severity: SeverityProgress},
	TaskEventTypeTaskCompleted:     {Value: "task_completed", Label: "执行结束", Severity: SeverityOk},
	TaskEventTypeTaskFailed:        {Value: "task_failed", Label: "执行失败", Severity: SeverityError},
	TaskEventTypeTaskCancelled:     {Value: "task_cancelled", Label: "已取消", Severity: SeverityWarn},
	TaskEventTypeDeliveryPending:   {Value: "delivery_pending", Label: "结果待确认", Severity: SeverityNeutral},
	TaskEventTypeDeliveryConfirmed: {Value: "delivery_confirmed", Label: "结果已确认", Severity: SeverityOk},
	TaskEventTypeDeliveryExpired:   {Value: "delivery_expired", Label: "结果交付已过期", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 task_event_type 取值。
func (s TaskEventType) Valid() bool {
	_, ok := TaskEventTypeMeta[s]
	return ok
}

// TaskPlan 的规划轴（计划 §8.1）。`invalidated` 是关键一态：
// 计划过期、输入版本变了、授权被撤销之后，**旧计划不许被执行**，
// 要重新规划并重新批准（§6.6 接单时重新检查）。
type PlanningState string

const (
	// TaskSpec 已建，还没探测
	PlanningStateDraft PlanningState = "draft"
	// 已获探索许可，正在 Probe
	PlanningStateExploring PlanningState = "exploring"
	// 计划已生成，等待用户批准外发边界
	PlanningStateReady PlanningState = "ready"
	// 计划变化超出原许可，暂停等重新批准
	PlanningStateAwaitingApproval PlanningState = "awaiting_approval"
	// 计划与外发边界都已批准，可以接单
	PlanningStateApproved PlanningState = "approved"
	// 计划过期、输入版本变更或授权撤销。**不得凭旧 Probe 放行**
	PlanningStateInvalidated PlanningState = "invalidated"
)

// PlanningStateValues 保持 enums.yaml 里的声明顺序。
var PlanningStateValues = []PlanningState{
	PlanningStateDraft,
	PlanningStateExploring,
	PlanningStateReady,
	PlanningStateAwaitingApproval,
	PlanningStateApproved,
	PlanningStateInvalidated,
}

var PlanningStateMeta = map[PlanningState]EnumMeta{
	PlanningStateDraft:            {Value: "draft", Label: "草稿", Severity: SeverityNeutral, Active: true},
	PlanningStateExploring:        {Value: "exploring", Label: "正在探测", Severity: SeverityProgress, Active: true},
	PlanningStateReady:            {Value: "ready", Label: "计划待批准", Severity: SeverityNeutral, Active: true},
	PlanningStateAwaitingApproval: {Value: "awaiting_approval", Label: "等待重新批准", Severity: SeverityWarn, Active: true},
	PlanningStateApproved:         {Value: "approved", Label: "已批准", Severity: SeverityOk},
	PlanningStateInvalidated:      {Value: "invalidated", Label: "计划已失效，需重新规划", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 planning_state 取值。
func (s PlanningState) Valid() bool {
	_, ok := PlanningStateMeta[s]
	return ok
}

// 远端执行者的受理轴（计划 §8.1 / §6.6）。
//
// **`unknown` 不等于「没执行」** —— 这是计划 T82 专门要求的区分：
// 回执丢了要先按幂等键对账，不能立刻把有副作用的步骤换个节点重做。
type AdmissionState string

const (
	// 还没提交给执行者
	AdmissionStateNotSubmitted AdmissionState = "not_submitted"
	// 受理会话已建、等输入上传完（§6.6）。**这一态不占 GPU** ——
	// 输入没齐就排队等于占着卡等上传。
	AdmissionStateWaitingInput AdmissionState = "waiting_input"
	// 服务端正在校验输入摘要与格式
	AdmissionStateChecking AdmissionState = "checking"
	// 已持久受理并返回 AdmissionReceipt（≠ 算力预留）
	AdmissionStateAccepted AdmissionState = "accepted"
	// 明确拒绝（授权、计划过期、输入不合格、配额）
	AdmissionStateRejected AdmissionState = "rejected"
	// 请求发出了但回执丢失。**必须按幂等键查询对账**，查到已有任务就用它；
	// 不得增加逻辑执行代次，也不得重复计一次成功交付。
	AdmissionStateUnknown AdmissionState = "unknown"
)

// AdmissionStateValues 保持 enums.yaml 里的声明顺序。
var AdmissionStateValues = []AdmissionState{
	AdmissionStateNotSubmitted,
	AdmissionStateWaitingInput,
	AdmissionStateChecking,
	AdmissionStateAccepted,
	AdmissionStateRejected,
	AdmissionStateUnknown,
}

var AdmissionStateMeta = map[AdmissionState]EnumMeta{
	AdmissionStateNotSubmitted: {Value: "not_submitted", Label: "未提交", Severity: SeverityNeutral},
	AdmissionStateWaitingInput: {Value: "waiting_input", Label: "等待输入上传", Severity: SeverityProgress, Active: true},
	AdmissionStateChecking:     {Value: "checking", Label: "校验输入中", Severity: SeverityProgress, Active: true},
	AdmissionStateAccepted:     {Value: "accepted", Label: "已受理", Severity: SeverityOk, Active: true},
	AdmissionStateRejected:     {Value: "rejected", Label: "被拒绝", Severity: SeverityError},
	AdmissionStateUnknown:      {Value: "unknown", Label: "受理状态未知（正在对账）", Severity: SeverityWarn, Active: true},
}

// Valid 报告 s 是不是一个已知的 admission_state 取值。
func (s AdmissionState) Valid() bool {
	_, ok := AdmissionStateMeta[s]
	return ok
}

// 输出验收轴（计划 §8.1 / §4.3 两层引用校验）。
//
// **结构校验通过 ≠ 内容正确**：`passed` 只表示引用确实存在、版本对得上、
// 定位可授权解析；"原文是否真的支持这个结论"是 `needs_review`
// 要人看的那件事（计划 §14.3 主张支持度，明确不能用引用存在率替代）。
type ValidationState string

const (
	// 还没校验
	ValidationStatePending ValidationState = "pending"
	// 结构校验通过：引用存在、版本正确、定位可解析
	ValidationStatePassed ValidationState = "passed"
	// 结构校验不通过（虚构引用 / 错版本 / 无权定位）
	ValidationStateFailed ValidationState = "failed"
	// 需要人工复核语义支持度或冲突
	ValidationStateNeedsReview ValidationState = "needs_review"
)

// ValidationStateValues 保持 enums.yaml 里的声明顺序。
var ValidationStateValues = []ValidationState{
	ValidationStatePending,
	ValidationStatePassed,
	ValidationStateFailed,
	ValidationStateNeedsReview,
}

var ValidationStateMeta = map[ValidationState]EnumMeta{
	ValidationStatePending:     {Value: "pending", Label: "待校验", Severity: SeverityNeutral, Active: true},
	ValidationStatePassed:      {Value: "passed", Label: "校验通过", Severity: SeverityOk},
	ValidationStateFailed:      {Value: "failed", Label: "校验未通过", Severity: SeverityError},
	ValidationStateNeedsReview: {Value: "needs_review", Label: "需人工复核", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 validation_state 取值。
func (s ValidationState) Valid() bool {
	_, ok := ValidationStateMeta[s]
	return ok
}

// 交付轴（计划 §8.3）。**计算成功不代表本地拿到结果。**
//
// `expired` 必须能显示出来：TTL 到期导致未领取结果失效时，界面上
// **不许**仍然显示"已保存本地"（计划 §8.3 原文要求）。
type DeliveryState string

const (
	// 不需要回传（结果留在中心）
	DeliveryStateNotRequested DeliveryState = "not_requested"
	// 结果已就绪，等待本地领取
	DeliveryStatePending DeliveryState = "pending"
	// 正在下载
	DeliveryStateTransferring DeliveryState = "transferring"
	// 本地校验 manifest 与文件后已幂等确认
	DeliveryStateConfirmed DeliveryState = "confirmed"
	// 暂存 TTL 到期，结果已失效。**不得显示成已保存本地**
	DeliveryStateExpired DeliveryState = "expired"
)

// DeliveryStateValues 保持 enums.yaml 里的声明顺序。
var DeliveryStateValues = []DeliveryState{
	DeliveryStateNotRequested,
	DeliveryStatePending,
	DeliveryStateTransferring,
	DeliveryStateConfirmed,
	DeliveryStateExpired,
}

var DeliveryStateMeta = map[DeliveryState]EnumMeta{
	DeliveryStateNotRequested: {Value: "not_requested", Label: "无需交付", Severity: SeverityNeutral},
	DeliveryStatePending:      {Value: "pending", Label: "待领取", Severity: SeverityNeutral, Active: true},
	DeliveryStateTransferring: {Value: "transferring", Label: "传输中", Severity: SeverityProgress, Active: true},
	DeliveryStateConfirmed:    {Value: "confirmed", Label: "已交付", Severity: SeverityOk},
	DeliveryStateExpired:      {Value: "expired", Label: "交付已过期（结果未领取）", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 delivery_state 取值。
func (s DeliveryState) Valid() bool {
	_, ok := DeliveryStateMeta[s]
	return ok
}

// 数据保留类别（计划 §8.1 / §8.3）。**临时处理不自动进入永久语料库** ——
// 远端算一次不等于对方获得了这份资料的长期副本。
//
// `task_pinned` 是给 GC 看的：引用仍被活跃任务或他人合法产物使用时，
// GC 不能删唯一副本（计划 §8.3 末段，项目已有的 `gc.py` 宽限期同理）。
type RetentionClass string

const (
	// 临时输入/中间产物，按 TTL 清理
	RetentionClassTemporary RetentionClass = "temporary"
	// 被活跃任务引用，GC 不得回收
	RetentionClassTaskPinned RetentionClass = "task_pinned"
	// 已按授权进入永久语料
	RetentionClassPersistent RetentionClass = "persistent"
	// 正在清理（宽限期内可能仍可见）
	RetentionClassDeleting RetentionClass = "deleting"
	// 已清理
	RetentionClassDeleted RetentionClass = "deleted"
)

// RetentionClassValues 保持 enums.yaml 里的声明顺序。
var RetentionClassValues = []RetentionClass{
	RetentionClassTemporary,
	RetentionClassTaskPinned,
	RetentionClassPersistent,
	RetentionClassDeleting,
	RetentionClassDeleted,
}

var RetentionClassMeta = map[RetentionClass]EnumMeta{
	RetentionClassTemporary:  {Value: "temporary", Label: "临时数据", Severity: SeverityNeutral},
	RetentionClassTaskPinned: {Value: "task_pinned", Label: "任务占用中", Severity: SeverityNeutral},
	RetentionClassPersistent: {Value: "persistent", Label: "永久保存", Severity: SeverityOk},
	RetentionClassDeleting:   {Value: "deleting", Label: "正在清理", Severity: SeverityProgress, Active: true},
	RetentionClassDeleted:    {Value: "deleted", Label: "已删除", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 retention_class 取值。
func (s RetentionClass) Valid() bool {
	_, ok := RetentionClassMeta[s]
	return ok
}

// 发布轴（计划 §8.1 / §4.4）。**私有来源的派生页面不能靠切 public 绕过
// 原许可** —— 发布前要检查派生内容的公开权（计划 §4.4、T06）。
type PublishingState string

const (
	// 仅所有者与获授权者可见
	PublishingStatePrivate PublishingState = "private"
	// 草稿，未发布
	PublishingStateDraft PublishingState = "draft"
	// 已按授权范围发布
	PublishingStatePublished PublishingState = "published"
	// 已撤回。**不承诺收回已下载副本**
	PublishingStateWithdrawn PublishingState = "withdrawn"
)

// PublishingStateValues 保持 enums.yaml 里的声明顺序。
var PublishingStateValues = []PublishingState{
	PublishingStatePrivate,
	PublishingStateDraft,
	PublishingStatePublished,
	PublishingStateWithdrawn,
}

var PublishingStateMeta = map[PublishingState]EnumMeta{
	PublishingStatePrivate:   {Value: "private", Label: "私有", Severity: SeverityNeutral},
	PublishingStateDraft:     {Value: "draft", Label: "草稿", Severity: SeverityNeutral},
	PublishingStatePublished: {Value: "published", Label: "已发布", Severity: SeverityOk},
	PublishingStateWithdrawn: {Value: "withdrawn", Label: "已撤回", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 publishing_state 取值。
func (s PublishingState) Valid() bool {
	_, ok := PublishingStateMeta[s]
	return ok
}

// 节点能力的就绪度（计划 §5.2）。**三件事必须分开记**：
// 静态能力配置、周期健康探测、本次任务预检。
//
// `configured` 不代表能用：计划原文举的例子是 `gpu=true` 不代表
// 所需模型已经就绪 —— 这正是本项目踩过的坑的联邦版本
// （注册表里有 OCR 专用模型，抽取平面拿它去抽值，抽不出来被记成
// `not_found`，系统能力缺失伪装成"文档里没有"，见已有的 `no_instruct`）。
type CapabilityReadiness string

const (
	// 配置里声明了这个能力，但没有健康证据。**不得当成可用**
	CapabilityReadinessConfigured CapabilityReadiness = "configured"
	// 健康探测通过且当前可接单
	CapabilityReadinessReady CapabilityReadiness = "ready"
	// 正在排空，不接新单但在跑的会做完
	CapabilityReadinessDraining CapabilityReadiness = "draining"
	// 健康探测失败
	CapabilityReadinessUnhealthy CapabilityReadiness = "unhealthy"
	// 没有有效的健康证据（从没探过 / 记录过期）。
	// **过期记录不是当前能力证明**（§5.5），要按未知处理，不许按
	// 最后一次成功当成现在可用。
	CapabilityReadinessUnknown CapabilityReadiness = "unknown"
)

// CapabilityReadinessValues 保持 enums.yaml 里的声明顺序。
var CapabilityReadinessValues = []CapabilityReadiness{
	CapabilityReadinessConfigured,
	CapabilityReadinessReady,
	CapabilityReadinessDraining,
	CapabilityReadinessUnhealthy,
	CapabilityReadinessUnknown,
}

var CapabilityReadinessMeta = map[CapabilityReadiness]EnumMeta{
	CapabilityReadinessConfigured: {Value: "configured", Label: "已配置（未验证可用）", Severity: SeverityNeutral},
	CapabilityReadinessReady:      {Value: "ready", Label: "可用", Severity: SeverityOk},
	CapabilityReadinessDraining:   {Value: "draining", Label: "正在排空", Severity: SeverityWarn},
	CapabilityReadinessUnhealthy:  {Value: "unhealthy", Label: "不可用", Severity: SeverityError},
	CapabilityReadinessUnknown:    {Value: "unknown", Label: "能力状态未知", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 capability_readiness 取值。
func (s CapabilityReadiness) Valid() bool {
	_, ok := CapabilityReadinessMeta[s]
	return ok
}

// Probe 的输入校验深度（计划 §6.4 / T78）。**这两个值的区别是钱**：
// 只看了文件描述就放进 admission，等于信任客户端声明的哈希 ——
// 本项目在直传上传那里已经踩过同一个坑（upload_status 的 `verifying`
// 不能跳过），联邦侧是同一条规则。
type InputValidation string

const (
	// 只校验了声明的格式/大小/类型，**没收到内容**。
	// 上传阶段只能是这个值，且此时不得占 GPU。
	InputValidationMetadataOnly InputValidation = "metadata_only"
	// 已收到内容并自己算过摘要校验通过。**预检仍不能排除运行时 OOM 或坏页**
	InputValidationContentVerified InputValidation = "content_verified"
)

// InputValidationValues 保持 enums.yaml 里的声明顺序。
var InputValidationValues = []InputValidation{
	InputValidationMetadataOnly,
	InputValidationContentVerified,
}

var InputValidationMeta = map[InputValidation]EnumMeta{
	InputValidationMetadataOnly:    {Value: "metadata_only", Label: "仅校验元数据", Severity: SeverityWarn},
	InputValidationContentVerified: {Value: "content_verified", Label: "已校验内容", Severity: SeverityOk},
}

// Valid 报告 s 是不是一个已知的 input_validation 取值。
func (s InputValidation) Valid() bool {
	_, ok := InputValidationMeta[s]
	return ok
}

// 检索模式（计划 §6.3 / §7）。**mode 决定怎么查，scope 决定查哪些** ——
// 两者不许互相覆盖：`fast` 不能缩小用户固定的资源范围，
// `local_first`（排序偏好）也不能偷偷变成 `local_only`（外发策略）。
type SearchMode string

const (
	// 有界选点：摘要排序 + 少量并行 Probe + 有条件扩展。
	// **结局最多是 retrieval=partial**，必须报告未检索范围。
	SearchModeFast SearchMode = "fast"
	// 按封存的 ScopeManifest 逐个目标实际探测。摘要只影响顺序、不删成员。
	// 即使已经拿到好答案也继续做完，除非用户取消（§7.3）。
	SearchModeExhaustiveScope SearchMode = "exhaustive_scope"
)

// SearchModeValues 保持 enums.yaml 里的声明顺序。
var SearchModeValues = []SearchMode{
	SearchModeFast,
	SearchModeExhaustiveScope,
}

var SearchModeMeta = map[SearchMode]EnumMeta{
	SearchModeFast:            {Value: "fast", Label: "快速检索（部分范围）", Severity: SeverityNeutral},
	SearchModeExhaustiveScope: {Value: "exhaustive_scope", Label: "范围穷查", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 search_mode 取值。
func (s SearchMode) Valid() bool {
	_, ok := SearchModeMeta[s]
	return ok
}

// `client-runtime` 的连接状态（计划 §3.4）。**与数据状态分开**
// （数据状态见 `snapshot_state`）—— 合起来的后果是
// "一个无关面板订阅失败把整个界面标成服务器断开"，计划明确禁止。
//
// 每个 `(environment_id, authenticated_profile_id)` 只有**一个**重连
// 负责人（计划 T66）；界面组件只订阅状态，不各自开重连循环。
type TransportState string

const (
	// 未连接
	TransportStateDisconnected TransportState = "disconnected"
	// 正在建立连接
	TransportStateConnecting TransportState = "connecting"
	// 连上了，正在认证
	TransportStateAuthenticating TransportState = "authenticating"
	// 可用
	TransportStateReady TransportState = "ready"
	// 有限退避等待重试
	TransportStateBackoff TransportState = "backoff"
	// 认证失效或被拒，**不再自动重试**（避免无休止刷新，T66）
	TransportStateBlocked TransportState = "blocked"
)

// TransportStateValues 保持 enums.yaml 里的声明顺序。
var TransportStateValues = []TransportState{
	TransportStateDisconnected,
	TransportStateConnecting,
	TransportStateAuthenticating,
	TransportStateReady,
	TransportStateBackoff,
	TransportStateBlocked,
}

var TransportStateMeta = map[TransportState]EnumMeta{
	TransportStateDisconnected:   {Value: "disconnected", Label: "未连接", Severity: SeverityNeutral},
	TransportStateConnecting:     {Value: "connecting", Label: "连接中", Severity: SeverityProgress, Active: true},
	TransportStateAuthenticating: {Value: "authenticating", Label: "认证中", Severity: SeverityProgress, Active: true},
	TransportStateReady:          {Value: "ready", Label: "已连接", Severity: SeverityOk},
	TransportStateBackoff:        {Value: "backoff", Label: "等待重连", Severity: SeverityWarn, Active: true},
	TransportStateBlocked:        {Value: "blocked", Label: "连接被拒绝（需重新配对）", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 transport_state 取值。
func (s TransportState) Valid() bool {
	_, ok := TransportStateMeta[s]
	return ok
}

// 客户端缓存投影的数据状态（计划 §3.4）。与 `transport_state` 分开的理由
// 在那条里。`stale` 要能显示：断网时可以看已取得的本地内容，
// 但**不能显示假在线**（计划 §3.2）。
type SnapshotState string

const (
	// 首次取快照中
	SnapshotStateLoading SnapshotState = "loading"
	// 与服务端游标一致
	SnapshotStateCurrent SnapshotState = "current"
	// 连接中断或游标落后，显示的是旧数据。**不得显示成在线最新**
	SnapshotStateStale SnapshotState = "stale"
	// 取快照失败（游标失效时应重新取快照而不是永久等）
	SnapshotStateFailed SnapshotState = "failed"
)

// SnapshotStateValues 保持 enums.yaml 里的声明顺序。
var SnapshotStateValues = []SnapshotState{
	SnapshotStateLoading,
	SnapshotStateCurrent,
	SnapshotStateStale,
	SnapshotStateFailed,
}

var SnapshotStateMeta = map[SnapshotState]EnumMeta{
	SnapshotStateLoading: {Value: "loading", Label: "加载中", Severity: SeverityProgress, Active: true},
	SnapshotStateCurrent: {Value: "current", Label: "最新", Severity: SeverityOk},
	SnapshotStateStale:   {Value: "stale", Label: "数据可能已过期", Severity: SeverityWarn},
	SnapshotStateFailed:  {Value: "failed", Label: "数据加载失败", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 snapshot_state 取值。
func (s SnapshotState) Valid() bool {
	_, ok := SnapshotStateMeta[s]
	return ok
}

// 联邦协议的机器可读错误码（计划 §9.6）。
//
// **命名对齐项目既有约定**：计划正文写的是 SCREAMING_CASE，这里统一成
// snake_case —— 项目所有错误码（`invalid_request_error`、`quota_error` …）
// 与所有枚举都是 snake_case，而生成器也只接受 snake_case。
// 同一个概念两种拼法就是漂移的开始，所以在 P0 一次定死。
//
// **对外降敏**：不得借错误码暴露私有资源是否存在（§8.4）——
// 无权主体看到的应该是"找不到"而不是"存在但你没权限"。
//
// **`unreachable` 类错误绝不能被前端翻译成「对方没有资料」**（§9.6 原文）：
// 那是把"我没查到"说成"那里没有"。
type FederationError string

const (
	// 成员枚举没能封存，覆盖承诺随之降级
	FederationErrorDiscoveryIncomplete FederationError = "discovery_incomplete"
	// ScopeManifest 过期，需重新枚举生成新 scope
	FederationErrorScopeExpired FederationError = "scope_expired"
	// 没有有效健康证据。**与 unsupported 严格分开** —— 未知要去预检，不是排除
	FederationErrorCapabilityUnknown FederationError = "capability_unknown"
	// 已核实不支持所需 operation
	FederationErrorCapabilityUnsupported FederationError = "capability_unsupported"
	// 输入摘要/格式还没校验通过就想进 admission
	FederationErrorInputNotVerified FederationError = "input_not_verified"
	// 外发许可不覆盖这次发送（接收方、内容或有效期超界）。
	// `local_only` 命中时也是这个码 —— 它高于所有自动回退（§6.2）。
	FederationErrorEgressDenied FederationError = "egress_denied"
	// 计划修订变了，原批准不再适用
	FederationErrorPlanChanged FederationError = "plan_changed"
	// Offer 有效期已过（Offer 本来就不预留算力）
	FederationErrorOfferExpired FederationError = "offer_expired"
	// 受理状态不明。**不等于未执行**，要按幂等键对账（T82）
	FederationErrorAdmissionUnknown FederationError = "admission_unknown"
	// 同一幂等键对应不同请求正文。**返回冲突，不许复用不相关结果**（T80）
	FederationErrorIdempotencyConflict FederationError = "idempotency_conflict"
	// 检索只完成了一部分，覆盖账本里有缺口
	FederationErrorPartialRetrieval FederationError = "partial_retrieval"
	// 本次范围与配置下没拿到足够证据
	FederationErrorInsufficientEvidence FederationError = "insufficient_evidence"
	// 根预算用尽（含发现与 Probe 的消耗）
	FederationErrorBudgetExhausted FederationError = "budget_exhausted"
	// 来源被撤销或转为私有，停止新授权并重判派生依赖
	FederationErrorSourceRevoked FederationError = "source_revoked"
	// 结果暂存 TTL 到期未领取
	FederationErrorDeliveryExpired FederationError = "delivery_expired"
	// 本地缺所需模型。**必须明确报出来**，不得悄悄请求远端（I03 / T18）——
	// 这正是项目已有的 `no_instruct_model` 在本地模式下的对应物。
	FederationErrorLocalModelMissing FederationError = "local_model_missing"
	// 协议版本或必需字段不兼容，明确拒绝而不是忽略后乱执行
	FederationErrorProtocolIncompatible FederationError = "protocol_incompatible"
	// 对已取消任务调用 resume。**取消是显式终态，不得被"恢复"改写回
	// running** —— 重跑必须是一条新任务（新授权、新覆盖分母），而不是
	// 拿旧计划接着跑。返回 409，任务状态原样不动。
	FederationErrorTaskCancelled FederationError = "task_cancelled"
	// 节点凭证缺失字段、不是规范序列化、base64 不严格、算法不是 Ed25519、
	// 有效期超过上限，或签名验不过（篡改）。一律 401，不区分是哪一步 ——
	// 分开报等于给伪造者一个逐步试错的口。
	FederationErrorCredentialInvalid FederationError = "credential_invalid"
	// 凭证已过期或签发时间在未来（超出时钟偏差容忍）
	FederationErrorCredentialExpired FederationError = "credential_expired"
	// 同一个 jti 第二次出现。凭证是**单次使用**的：执行者在验签通过后
	// 持久记下 jti 直到过期，并发重放由唯一约束仲裁（401）。
	FederationErrorCredentialReplayed FederationError = "credential_replayed"
	// 凭证的 audience 不是接收它的这个节点。转手给第三个节点（越权转委托）就是这个码
	FederationErrorCredentialAudienceMismatch FederationError = "credential_audience_mismatch"
	// 凭证授权的操作不是本端点的操作（403）
	FederationErrorCredentialOperationDenied FederationError = "credential_operation_denied"
	// 凭证的请求绑定（方法/路径/正文摘要）或范围约束（root_task_id / step_id /
	// scope_ref / task_spec_digest）不覆盖这次请求或它要读的那一行（403）；
	// 以别的协调者名义提交计划也是这个码。
	FederationErrorCredentialScopeDenied FederationError = "credential_scope_denied"
	// 签发节点不在本节点控制面的成员目录里，或尚未被管理员批准（401）
	FederationErrorNodeUnknown FederationError = "node_unknown"
	// 签发节点已被管理员撤销。撤销对新请求生效的延迟以公钥缓存上限为界（401）
	FederationErrorNodeRevoked FederationError = "node_revoked"
	// 语料服务配置的节点身份（BUNDLE_NODE_ID）与控制面持久密钥派生的身份不一致，
	// 或控制面报告的本节点身份变了。**Fail Closed（503）**：否则本地目标会被当成远端
	FederationErrorNodeIdentityMismatch FederationError = "node_identity_mismatch"
	// 还没从控制面取到本节点持久身份（503），联邦端点与出站一律拒绝
	FederationErrorNodeIdentityUnavailable FederationError = "node_identity_unavailable"
	// 没有可用的本节点身份配置（503）：shared 开发档位或测试跟随模式下
	// BUNDLE_NODE_ID 为空或形状不对。先配好持久身份再谈联邦。
	FederationErrorNodeIdentityUnconfigured FederationError = "node_identity_unconfigured"
	// 本节点控制面在凭证链路上不可用：出站时签不出凭证（不可达、拒签、响应形状不对），
	// 或入站时查不到签发节点的信任记录（503）。**是本节点的问题，不是对端没有资料**
	// —— 协调者记 unreachable 并保留可重试。
	FederationErrorCredentialUnavailable FederationError = "credential_unavailable"
)

// FederationErrorValues 保持 enums.yaml 里的声明顺序。
var FederationErrorValues = []FederationError{
	FederationErrorDiscoveryIncomplete,
	FederationErrorScopeExpired,
	FederationErrorCapabilityUnknown,
	FederationErrorCapabilityUnsupported,
	FederationErrorInputNotVerified,
	FederationErrorEgressDenied,
	FederationErrorPlanChanged,
	FederationErrorOfferExpired,
	FederationErrorAdmissionUnknown,
	FederationErrorIdempotencyConflict,
	FederationErrorPartialRetrieval,
	FederationErrorInsufficientEvidence,
	FederationErrorBudgetExhausted,
	FederationErrorSourceRevoked,
	FederationErrorDeliveryExpired,
	FederationErrorLocalModelMissing,
	FederationErrorProtocolIncompatible,
	FederationErrorTaskCancelled,
	FederationErrorCredentialInvalid,
	FederationErrorCredentialExpired,
	FederationErrorCredentialReplayed,
	FederationErrorCredentialAudienceMismatch,
	FederationErrorCredentialOperationDenied,
	FederationErrorCredentialScopeDenied,
	FederationErrorNodeUnknown,
	FederationErrorNodeRevoked,
	FederationErrorNodeIdentityMismatch,
	FederationErrorNodeIdentityUnavailable,
	FederationErrorNodeIdentityUnconfigured,
	FederationErrorCredentialUnavailable,
}

var FederationErrorMeta = map[FederationError]EnumMeta{
	FederationErrorDiscoveryIncomplete:        {Value: "discovery_incomplete", Label: "节点范围未能完整确定", Severity: SeverityWarn},
	FederationErrorScopeExpired:               {Value: "scope_expired", Label: "检索范围已过期", Severity: SeverityWarn},
	FederationErrorCapabilityUnknown:          {Value: "capability_unknown", Label: "对方能力未知（需预检）", Severity: SeverityWarn},
	FederationErrorCapabilityUnsupported:      {Value: "capability_unsupported", Label: "对方不支持该操作", Severity: SeverityNeutral},
	FederationErrorInputNotVerified:           {Value: "input_not_verified", Label: "输入尚未校验通过", Severity: SeverityError},
	FederationErrorEgressDenied:               {Value: "egress_denied", Label: "该数据不允许发往此接收方", Severity: SeverityError},
	FederationErrorPlanChanged:                {Value: "plan_changed", Label: "执行计划已变更，需重新批准", Severity: SeverityWarn},
	FederationErrorOfferExpired:               {Value: "offer_expired", Label: "执行意向已过期", Severity: SeverityWarn},
	FederationErrorAdmissionUnknown:           {Value: "admission_unknown", Label: "受理状态未知（正在对账）", Severity: SeverityWarn},
	FederationErrorIdempotencyConflict:        {Value: "idempotency_conflict", Label: "幂等键冲突（请求内容不一致）", Severity: SeverityError},
	FederationErrorPartialRetrieval:           {Value: "partial_retrieval", Label: "检索未覆盖全部范围", Severity: SeverityWarn},
	FederationErrorInsufficientEvidence:       {Value: "insufficient_evidence", Label: "证据不足", Severity: SeverityWarn},
	FederationErrorBudgetExhausted:            {Value: "budget_exhausted", Label: "预算已用尽", Severity: SeverityWarn},
	FederationErrorSourceRevoked:              {Value: "source_revoked", Label: "来源已撤销", Severity: SeverityWarn},
	FederationErrorDeliveryExpired:            {Value: "delivery_expired", Label: "结果已过期未领取", Severity: SeverityError},
	FederationErrorLocalModelMissing:          {Value: "local_model_missing", Label: "本地缺少所需模型", Severity: SeverityError},
	FederationErrorProtocolIncompatible:       {Value: "protocol_incompatible", Label: "协议版本不兼容", Severity: SeverityError},
	FederationErrorTaskCancelled:              {Value: "task_cancelled", Label: "任务已取消，不能恢复", Severity: SeverityError},
	FederationErrorCredentialInvalid:          {Value: "credential_invalid", Label: "节点凭证无效", Severity: SeverityError},
	FederationErrorCredentialExpired:          {Value: "credential_expired", Label: "节点凭证已过期", Severity: SeverityError},
	FederationErrorCredentialReplayed:         {Value: "credential_replayed", Label: "节点凭证被重放", Severity: SeverityError},
	FederationErrorCredentialAudienceMismatch: {Value: "credential_audience_mismatch", Label: "凭证不是发给本节点的", Severity: SeverityError},
	FederationErrorCredentialOperationDenied:  {Value: "credential_operation_denied", Label: "凭证不允许该操作", Severity: SeverityError},
	FederationErrorCredentialScopeDenied:      {Value: "credential_scope_denied", Label: "凭证范围不覆盖该请求", Severity: SeverityError},
	FederationErrorNodeUnknown:                {Value: "node_unknown", Label: "未知或未批准的节点", Severity: SeverityError},
	FederationErrorNodeRevoked:                {Value: "node_revoked", Label: "节点已被撤销", Severity: SeverityError},
	FederationErrorNodeIdentityMismatch:       {Value: "node_identity_mismatch", Label: "本节点身份不一致（联邦已停用）", Severity: SeverityError},
	FederationErrorNodeIdentityUnavailable:    {Value: "node_identity_unavailable", Label: "本节点身份尚未确定", Severity: SeverityError},
	FederationErrorNodeIdentityUnconfigured:   {Value: "node_identity_unconfigured", Label: "本节点身份未配置", Severity: SeverityError},
	FederationErrorCredentialUnavailable:      {Value: "credential_unavailable", Label: "本节点凭证服务不可用", Severity: SeverityError},
}

// Valid 报告 s 是不是一个已知的 federation_error 取值。
func (s FederationError) Valid() bool {
	_, ok := FederationErrorMeta[s]
	return ok
}

// 一张节点凭证授权的**唯一**操作（DDP-NODE-CREDENTIAL）。每个节点对节点
// 端点恰好对应一个值；凭证只签一个操作，拿读执行状态的凭证去受理任务是
// `credential_operation_denied`。
type NodeCredentialOperation string

const (
	// POST /api/v1/federation/probes
	NodeCredentialOperationProbeCreate NodeCredentialOperation = "probe_create"
	// GET /api/v1/federation/probes/{probe_id}
	NodeCredentialOperationProbeRead NodeCredentialOperation = "probe_read"
	// POST /api/v1/federation/admissions
	NodeCredentialOperationAdmissionCreate NodeCredentialOperation = "admission_create"
	// POST /api/v1/federation/admissions/lookup
	NodeCredentialOperationAdmissionLookup NodeCredentialOperation = "admission_lookup"
	// GET /api/v1/federation/tasks/{executor_task_id}
	NodeCredentialOperationExecutionRead NodeCredentialOperation = "execution_read"
	// POST /api/v1/federation/tasks/{executor_task_id}/cancel
	NodeCredentialOperationExecutionCancel NodeCredentialOperation = "execution_cancel"
	// GET /api/v1/federation/evidence-sets/{set_ref}
	NodeCredentialOperationEvidenceSetRead NodeCredentialOperation = "evidence_set_read"
	// POST /api/v1/federation/resources/locate
	NodeCredentialOperationResourceLocate NodeCredentialOperation = "resource_locate"
	// POST /api/v1/federation/results/resolve
	NodeCredentialOperationResultResolve NodeCredentialOperation = "result_resolve"
	// GET /api/v1/federation/published-collections
	NodeCredentialOperationCatalogRead NodeCredentialOperation = "catalog_read"
)

// NodeCredentialOperationValues 保持 enums.yaml 里的声明顺序。
var NodeCredentialOperationValues = []NodeCredentialOperation{
	NodeCredentialOperationProbeCreate,
	NodeCredentialOperationProbeRead,
	NodeCredentialOperationAdmissionCreate,
	NodeCredentialOperationAdmissionLookup,
	NodeCredentialOperationExecutionRead,
	NodeCredentialOperationExecutionCancel,
	NodeCredentialOperationEvidenceSetRead,
	NodeCredentialOperationResourceLocate,
	NodeCredentialOperationResultResolve,
	NodeCredentialOperationCatalogRead,
}

var NodeCredentialOperationMeta = map[NodeCredentialOperation]EnumMeta{
	NodeCredentialOperationProbeCreate:     {Value: "probe_create", Label: "发起探测", Severity: SeverityNeutral},
	NodeCredentialOperationProbeRead:       {Value: "probe_read", Label: "读取探测回执", Severity: SeverityNeutral},
	NodeCredentialOperationAdmissionCreate: {Value: "admission_create", Label: "提交接单", Severity: SeverityNeutral},
	NodeCredentialOperationAdmissionLookup: {Value: "admission_lookup", Label: "对账接单", Severity: SeverityNeutral},
	NodeCredentialOperationExecutionRead:   {Value: "execution_read", Label: "读取执行状态", Severity: SeverityNeutral},
	NodeCredentialOperationExecutionCancel: {Value: "execution_cancel", Label: "取消执行", Severity: SeverityNeutral},
	NodeCredentialOperationEvidenceSetRead: {Value: "evidence_set_read", Label: "读取证据集", Severity: SeverityNeutral},
	NodeCredentialOperationResourceLocate:  {Value: "resource_locate", Label: "定位资源版本", Severity: SeverityNeutral},
	NodeCredentialOperationResultResolve:   {Value: "result_resolve", Label: "解析证据引用", Severity: SeverityNeutral},
	NodeCredentialOperationCatalogRead:     {Value: "catalog_read", Label: "读取发布目录", Severity: SeverityNeutral},
}

// Valid 报告 s 是不是一个已知的 node_credential_operation 取值。
func (s NodeCredentialOperation) Valid() bool {
	_, ok := NodeCredentialOperationMeta[s]
	return ok
}

// 语料服务节点对节点端点的认证方式。`node_credential` 是唯一的生产形态；
// `shared_token_insecure` 只给没有控制面的开发夹具，**必须显式配置且同时
// 打开 ALLOW_INSECURE_DEFAULTS**，并在 /readyz 与能力声明里如实报成降级。
type PeerAuthMode string

const (
	// 控制面持有节点私钥，按请求签发限定 audience/actor/操作/范围/有效期的单次凭证
	PeerAuthModeNodeCredential PeerAuthMode = "node_credential"
	// 旧的共享 peer token + 服务凭据 + actor 头。所有登记同伴共用一个秘密、
	// 调用方身份未签名、同名用户会被直接合并 —— 只许开发用
	PeerAuthModeSharedTokenInsecure PeerAuthMode = "shared_token_insecure"
)

// PeerAuthModeValues 保持 enums.yaml 里的声明顺序。
var PeerAuthModeValues = []PeerAuthMode{
	PeerAuthModeNodeCredential,
	PeerAuthModeSharedTokenInsecure,
}

var PeerAuthModeMeta = map[PeerAuthMode]EnumMeta{
	PeerAuthModeNodeCredential:      {Value: "node_credential", Label: "节点签名凭证", Severity: SeverityOk},
	PeerAuthModeSharedTokenInsecure: {Value: "shared_token_insecure", Label: "共享口令（不安全，仅开发）", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 peer_auth_mode 取值。
func (s PeerAuthMode) Valid() bool {
	_, ok := PeerAuthModeMeta[s]
	return ok
}

// 控制域管理员批准的直接节点成员状态，批准不授予资源权限或证明远端持有密钥。
type NodeMembershipState string

const (
	// 已登记但管理员尚未批准
	NodeMembershipStatePending NodeMembershipState = "pending"
	// 管理员已批准配置，健康与接单另行判断
	NodeMembershipStateApproved NodeMembershipState = "approved"
	// 已撤销，保留旧快照成员位置且禁止旧修订恢复
	NodeMembershipStateRevoked NodeMembershipState = "revoked"
)

// NodeMembershipStateValues 保持 enums.yaml 里的声明顺序。
var NodeMembershipStateValues = []NodeMembershipState{
	NodeMembershipStatePending,
	NodeMembershipStateApproved,
	NodeMembershipStateRevoked,
}

var NodeMembershipStateMeta = map[NodeMembershipState]EnumMeta{
	NodeMembershipStatePending:  {Value: "pending", Label: "待批准", Severity: SeverityNeutral},
	NodeMembershipStateApproved: {Value: "approved", Label: "已批准", Severity: SeverityOk},
	NodeMembershipStateRevoked:  {Value: "revoked", Label: "已撤销", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 node_membership_state 取值。
func (s NodeMembershipState) Valid() bool {
	_, ok := NodeMembershipStateMeta[s]
	return ok
}

// 单目录快照中的下级枚举状态，不声明递归全局覆盖。
type MemberExpansionState string

const (
	// 成员支持枚举但尚未请求下级目录
	MemberExpansionStateNotRequested MemberExpansionState = "not_requested"
	// 下级不可枚举，不等于空目录
	MemberExpansionStateUnexpandedSubtree MemberExpansionState = "unexpanded_subtree"
	// 原快照成员已撤销或当前调用者不可见
	MemberExpansionStateSourceRevoked MemberExpansionState = "source_revoked"
)

// MemberExpansionStateValues 保持 enums.yaml 里的声明顺序。
var MemberExpansionStateValues = []MemberExpansionState{
	MemberExpansionStateNotRequested,
	MemberExpansionStateUnexpandedSubtree,
	MemberExpansionStateSourceRevoked,
}

var MemberExpansionStateMeta = map[MemberExpansionState]EnumMeta{
	MemberExpansionStateNotRequested:      {Value: "not_requested", Label: "尚未展开", Severity: SeverityNeutral},
	MemberExpansionStateUnexpandedSubtree: {Value: "unexpanded_subtree", Label: "下级未展开", Severity: SeverityWarn},
	MemberExpansionStateSourceRevoked:     {Value: "source_revoked", Label: "来源已撤销", Severity: SeverityWarn},
}

// Valid 报告 s 是不是一个已知的 member_expansion_state 取值。
func (s MemberExpansionState) Valid() bool {
	_, ok := MemberExpansionStateMeta[s]
	return ok
}
