/*
 * 由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。
 * 改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。
 */

export type Severity = 'neutral' | 'progress' | 'ok' | 'warn' | 'error'

export interface EnumMeta {
  /** 枚举值本身 */
  value: string
  /** 给用户看的中文文案 */
  label: string
  /** UI 据此选标签颜色，不要在前端另立一套 */
  severity: Severity
  /** 是否属于「还在动」的状态 —— 列表页据此决定要不要继续轮询 */
  active?: boolean
}

// 问答 / 检索 / 抽取平面的降级原因。落在 `messages.degraded`、
// DDP-Extract 的 `degraded`、以及检索响应里。
//
// **一次只报一个**（最先命中的那个）。需要同时报多个的场合请用
// `compile_degraded` 那种列表形状，不要往这里塞逗号分隔串。
export type Degraded = 'no_hits' | 'parse_mismatch' | 'resource_index_unavailable' | 'embedding_unavailable' | 'vision_unavailable' | 'crop_unsupported' | 'crop_failed' | 'client_aborted' | 'upstream_error' | 'upstream_interrupted' | 'index_changed_during_answer' | 'decision_unavailable' | 'no_evidence_in_turn' | 'inherited_evidence_incomplete' | 'gate_rejected_all' | 'citation_persist_failed' | 'verification_unavailable' | 'evidence_unavailable' | 'schema_violation' | 'rerank_unavailable' | 'no_instruct_model' | 'empty_query' | 'answer_unavailable'

export const DEGRADED_VALUES: readonly Degraded[] = [
  'no_hits',
  'parse_mismatch',
  'resource_index_unavailable',
  'embedding_unavailable',
  'vision_unavailable',
  'crop_unsupported',
  'crop_failed',
  'client_aborted',
  'upstream_error',
  'upstream_interrupted',
  'index_changed_during_answer',
  'decision_unavailable',
  'no_evidence_in_turn',
  'inherited_evidence_incomplete',
  'gate_rejected_all',
  'citation_persist_failed',
  'verification_unavailable',
  'evidence_unavailable',
  'schema_violation',
  'rerank_unavailable',
  'no_instruct_model',
  'empty_query',
  'answer_unavailable',
] as const

export const DEGRADED_META: Record<Degraded, EnumMeta> = {
  // 检索一条都没命中
  "no_hits": { value: 'no_hits', label: "未在本文档中检索到相关内容", severity: 'neutral' },
  // 裁图上的文字与解析出的块文本对不上（相似度低于
  // QA_PARSE_MISMATCH_THRESHOLD / EXTRACT_MISMATCH_THRESHOLD，实测标定 0.55）。
  // 它是**假出处**的主要探测手段，不是小问题。
  "parse_mismatch": { value: 'parse_mismatch', label: "出处存疑（图上内容与解析文本对不上）", severity: 'warn' },
  // 授权资源的固定解析版本尚无可用索引
  "resource_index_unavailable": { value: 'resource_index_unavailable', label: "该资源版本索引尚不可用，请查看解析任务", severity: 'warn' },
  // 向量化服务不可达，只走了关键词路。**这条是本项目吃过最大亏的地方**：
  // M4a 时向量检索静默退回 BM25，没人发现。必须可见。
  "embedding_unavailable": { value: 'embedding_unavailable', label: "仅关键词检索（向量化服务不可用）", severity: 'warn' },
  // 视觉模型不可用，本轮没做视觉核对
  "vision_unavailable": { value: 'vision_unavailable', label: "未做视觉验证（视觉模型不可用）", severity: 'warn' },
  // 该文件类型不支持按 bbox 裁图（例如非 PDF 原件）
  "crop_unsupported": { value: 'crop_unsupported', label: "未做视觉验证（该文件不支持区域截图）", severity: 'neutral' },
  // 裁图渲染失败。**注意**：依赖缺失不走这条，见 ddp_core/crops.py 的 _DEP_NOTE
  "crop_failed": { value: 'crop_failed', label: "未做视觉验证（区域截图失败）", severity: 'warn' },
  // 客户端在流式回答途中断开
  "client_aborted": { value: 'client_aborted', label: "回答被中断", severity: 'neutral' },
  // 上游模型服务返回错误
  "upstream_error": { value: 'upstream_error', label: "问答服务异常", severity: 'error' },
  // 上游在流式输出中途断流（拿到的是半截答案）
  "upstream_interrupted": { value: 'upstream_interrupted', label: "回答生成中途断流", severity: 'error' },
  // 回答生成期间索引 generation 变了，本轮出处已标失效
  "index_changed_during_answer": { value: 'index_changed_during_answer', label: "回答生成期间索引版本已变化，出处已标为失效", severity: 'warn' },
  // 「这轮要不要检索」的判定模型不可用，已保守地执行检索
  "decision_unavailable": { value: 'decision_unavailable', label: "是否检索判定不可用，已保守执行检索", severity: 'neutral' },
  // 本轮既没检索到证据也没有可继承证据，拒绝脱离文档作答
  "no_evidence_in_turn": { value: 'no_evidence_in_turn', label: "本轮没有可继承证据，已拒绝脱离文档作答", severity: 'warn' },
  // 上一轮的证据部分失效，不能直接沿用
  "inherited_evidence_incomplete": { value: 'inherited_evidence_incomplete', label: "上一轮证据已部分失效，需重新检索后再回答", severity: 'warn' },
  // 候选全部没通过逐篇质量门控（有候选但都不够格，与 no_hits 不同）
  "gate_rejected_all": { value: 'gate_rejected_all', label: "检索候选均未通过逐篇质量门控", severity: 'warn' },
  // 出处写库失败，相关结论已标为无证据支持
  "citation_persist_failed": { value: 'citation_persist_failed', label: "出处保存失败，相关结论已标为无证据支持", severity: 'error' },
  // 原文自动核对没得出结论
  "verification_unavailable": { value: 'verification_unavailable', label: "原文自动核对未得出结论，请人工复核", severity: 'warn' },
  // 远端计算的固定版本编译完成但没有可交付的冻结证据；交付 Bundle 只含原件与版面。
  // 与 no_hits 不同：这里不是检索没命中，而是证据本身不存在。
  "evidence_unavailable": { value: 'evidence_unavailable', label: "交付结果不含冻结证据（原件与版面仍可用）", severity: 'warn' },
  // 模型输出不符合约定结构。抽取平面：按 EXTRACT_MAX_RETRIES 重试仍失败；
  // 问答平面：回答未满足逐条证据绑定协议（非法 JSON、缺失/越界 evidence_id、
  // 截断），不重试，已校验的完整断言作为显式失败的部分回答保留。
  // **绝不能被静默当成 not_found / 文档中没有** —— 那会把系统故障伪装成"文档里没有"。
  "schema_violation": { value: 'schema_violation', label: "模型输出不符合约定格式", severity: 'error' },
  // 配了精排但上游没注册 rerank 模型，本轮没重排
  "rerank_unavailable": { value: 'rerank_unavailable', label: "未做精排（重排序服务不可用）", severity: 'neutral' },
  // 注册表里只有 OCR 专用模型（`capabilities` 含 `no_instruct`），
  // 抽值无处可调。同样绝不能伪装成 not_found。
  "no_instruct_model": { value: 'no_instruct_model', label: "未抽取（后端没有可用的指令模型）", severity: 'error' },
  // MCP `search` 收到空查询串，直接返回空结果
  "empty_query": { value: 'empty_query', label: "查询词为空", severity: 'neutral' },
  // MCP `ask` 调上游生成时非 200，本轮没有答案（证据仍然返回）
  "answer_unavailable": { value: 'answer_unavailable', label: "生成服务不可用（证据已返回，结论未生成）", severity: 'error' },
}

export function degradedLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return DEGRADED_META[value as Degraded]?.label ?? `未知取值（${value}）`
}

// 版面编译（DDP-Compile v1）的降级。与 `degraded` 分开是因为它是
// **列表**：一次编译可以同时有好几种降级，而且它落在
// `documents.compile_degraded`（JSON 数组）上。
export type CompileDegraded = 'code_detection_unavailable' | 'crop_unsupported' | 'crop_failed' | 'vision_unavailable' | 'vision_invalid_output' | 'provider_unresolved' | 'reindex_validation_required' | 'compile_failed' | 'layout_unavailable'

export const COMPILE_DEGRADED_VALUES: readonly CompileDegraded[] = [
  'code_detection_unavailable',
  'crop_unsupported',
  'crop_failed',
  'vision_unavailable',
  'vision_invalid_output',
  'provider_unresolved',
  'reindex_validation_required',
  'compile_failed',
  'layout_unavailable',
] as const

export const COMPILE_DEGRADED_META: Record<CompileDegraded, EnumMeta> = {
  // 当前版面引擎报不出代码块
  "code_detection_unavailable": { value: 'code_detection_unavailable', label: "当前版面引擎不能识别代码块", severity: 'neutral' },
  // 部分视觉原子没有可定位的裁图
  "crop_unsupported": { value: 'crop_unsupported', label: "部分视觉原子没有可定位裁图", severity: 'neutral' },
  // 部分视觉原子裁图失败
  "crop_failed": { value: 'crop_failed', label: "部分视觉原子裁图失败", severity: 'warn' },
  // 视觉理解模型不可用
  "vision_unavailable": { value: 'vision_unavailable', label: "视觉理解模型不可用", severity: 'warn' },
  // 视觉模型返回的结构不合规
  "vision_invalid_output": { value: 'vision_invalid_output', label: "视觉理解模型返回的结构不合规", severity: 'warn' },
  // 上游实际模型没解析出来，本次编译版本不可比较
  "provider_unresolved": { value: 'provider_unresolved', label: "上游实际模型未解析，当前编译版本不可比较", severity: 'warn' },
  // 存在历史出处，需先校验并人工确认后才能重建
  "reindex_validation_required": { value: 'reindex_validation_required', label: "存在历史出处，需先校验并确认后重建", severity: 'warn' },
  // 版面编译整体失败
  "compile_failed": { value: 'compile_failed', label: "版面编译失败", severity: 'error' },
  // 导入的来源没有可用版面（layout.json 缺失或无效），未编译、未建索引
  "layout_unavailable": { value: 'layout_unavailable', label: "来源版面不可用，未编译", severity: 'error' },
}

export function compileDegradedLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return COMPILE_DEGRADED_META[value as CompileDegraded]?.label ?? `未知取值（${value}）`
}

// 解析任务状态。契约（`/v1/parse/{id}`）只承诺四态；
// `archiving` 是**产品层**多出来的一态：网关已完成但归档还没落地，
// 对用户是"还在动"。
export type ParseStatus = 'pending' | 'running' | 'archiving' | 'succeeded' | 'failed'

export const PARSE_STATUS_VALUES: readonly ParseStatus[] = [
  'pending',
  'running',
  'archiving',
  'succeeded',
  'failed',
] as const

export const PARSE_STATUS_META: Record<ParseStatus, EnumMeta> = {
  // 已受理，排队中
  "pending": { value: 'pending', label: "排队中", severity: 'neutral', active: true },
  // 引擎正在解析
  "running": { value: 'running', label: "解析中", severity: 'progress', active: true },
  // 引擎已完成，产品层正在归档结果
  "archiving": { value: 'archiving', label: "归档中", severity: 'progress', active: true },
  // 解析完成且结果已可取
  "succeeded": { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 解析失败，error 里有原因
  "failed": { value: 'failed', label: "失败", severity: 'error' },
}

/** 契约 openapi_v1 只承诺这几个值 */
export const PARSE_STATUS_OPENAPI_V1: readonly ParseStatus[] = ['pending', 'running', 'succeeded', 'failed']

export function parseStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return PARSE_STATUS_META[value as ParseStatus]?.label ?? `未知取值（${value}）`
}

// 向量索引状态。索引失败必须能在 UI 上看到，不许静默。
export type IndexStatus = 'none' | 'pending' | 'indexing' | 'ready' | 'failed'

export const INDEX_STATUS_VALUES: readonly IndexStatus[] = [
  'none',
  'pending',
  'indexing',
  'ready',
  'failed',
] as const

export const INDEX_STATUS_META: Record<IndexStatus, EnumMeta> = {
  // 还没建过索引
  "none": { value: 'none', label: "未索引", severity: 'neutral' },
  // 已排队等待索引
  "pending": { value: 'pending', label: "待索引", severity: 'neutral', active: true },
  // 正在建索引
  "indexing": { value: 'indexing', label: "索引中", severity: 'progress', active: true },
  // 索引可用，可以问答
  "ready": { value: 'ready', label: "可问答", severity: 'ok' },
  // 索引失败，index_error 里有原因
  "failed": { value: 'failed', label: "索引失败", severity: 'error' },
}

export function indexStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return INDEX_STATUS_META[value as IndexStatus]?.label ?? `未知取值（${value}）`
}

// 版面编译状态。**索引 ready 不代表视觉理解完整** —— 编译状态与降级
// 必须单列并在前端展示。
export type CompileStatus = 'none' | 'pending' | 'compiling' | 'ready' | 'partial' | 'failed'

export const COMPILE_STATUS_VALUES: readonly CompileStatus[] = [
  'none',
  'pending',
  'compiling',
  'ready',
  'partial',
  'failed',
] as const

export const COMPILE_STATUS_META: Record<CompileStatus, EnumMeta> = {
  // 还没编译
  "none": { value: 'none', label: "未编译", severity: 'neutral' },
  // 已排队等待编译
  "pending": { value: 'pending', label: "待编译", severity: 'neutral', active: true },
  // 正在编译
  "compiling": { value: 'compiling', label: "编译中", severity: 'progress', active: true },
  // 编译完整、无降级
  "ready": { value: 'ready', label: "编译完整", severity: 'ok' },
  // 编译完成但有降级，见 compile_degraded
  "partial": { value: 'partial', label: "编译有降级", severity: 'warn' },
  // 编译失败
  "failed": { value: 'failed', label: "编译失败", severity: 'error' },
}

export function compileStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return COMPILE_STATUS_META[value as CompileStatus]?.label ?? `未知取值（${value}）`
}

// 抽取批次状态。`partial` **不是"有点问题"的委婉说法**：它明确表示
// 必填字段没抽全，或批次里个别文档失败。一批 200 份里有 3 份失败
// 报成"成功"，会让人直接拿去用。
export type RunStatus = 'pending' | 'running' | 'succeeded' | 'partial' | 'failed'

export const RUN_STATUS_VALUES: readonly RunStatus[] = [
  'pending',
  'running',
  'succeeded',
  'partial',
  'failed',
] as const

export const RUN_STATUS_META: Record<RunStatus, EnumMeta> = {
  // 已受理，排队中
  "pending": { value: 'pending', label: "排队中", severity: 'neutral', active: true },
  // 正在抽取
  "running": { value: 'running', label: "抽取中", severity: 'progress', active: true },
  // 全部文档全部字段都完成
  "succeeded": { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 部分文档或部分字段失败
  "partial": { value: 'partial', label: "部分完成", severity: 'warn' },
  // 整批失败
  "failed": { value: 'failed', label: "失败", severity: 'error' },
}

export function runStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return RUN_STATUS_META[value as RunStatus]?.label ?? `未知取值（${value}）`
}

// DDP-Extract 的字段三态。**必须分开对待**：`not_found` 是"我们看过了，
// 文档里确实没有"，是一种正确答案；`error` 才是系统问题。
// 界面上 not_found 绝不能显示成空白或 "—"（那让人以为是没渲染出来），
// error 也绝不能显示成"未提及"（那是把系统故障伪装成事实）。
export type FieldStatus = 'found' | 'not_found' | 'error'

export const FIELD_STATUS_VALUES: readonly FieldStatus[] = [
  'found',
  'not_found',
  'error',
] as const

export const FIELD_STATUS_META: Record<FieldStatus, EnumMeta> = {
  // 抽到了值，且有出处
  "found": { value: 'found', label: "已抽取", severity: 'ok' },
  // 文档里确实没有这个字段
  "not_found": { value: 'not_found', label: "文档中未提及", severity: 'neutral' },
  // 抽取过程本身出错
  "error": { value: 'error', label: "抽取失败", severity: 'error' },
}

export function fieldStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return FIELD_STATUS_META[value as FieldStatus]?.label ?? `未知取值（${value}）`
}

// 代码块识别的来源。启发式与原生要分开，因为它决定了代码检索的可信度。
export type CodeDetection = 'native' | 'heuristic' | 'unavailable'

export const CODE_DETECTION_VALUES: readonly CodeDetection[] = [
  'native',
  'heuristic',
  'unavailable',
] as const

export const CODE_DETECTION_META: Record<CodeDetection, EnumMeta> = {
  // 版面引擎直接报出了 code 块
  "native": { value: 'native', label: "代码识别：原生", severity: 'ok' },
  // 靠启发式规则判出来的
  "heuristic": { value: 'heuristic', label: "代码识别：启发式", severity: 'neutral' },
  // 当前引擎识别不了代码块
  "unavailable": { value: 'unavailable', label: "代码识别：不可用", severity: 'warn' },
}

export function codeDetectionLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return CODE_DETECTION_META[value as CodeDetection]?.label ?? `未知取值（${value}）`
}

// 证据是原文还是生成物。**第三条不变式**：生成物与原文必须可区分，
// 且生成物的引用最终仍要指回原始原子 bbox（`derived_from`）。
// 判据是 `evidence.derived_from` 是否为空 —— 不要在别处另立标志位。
export type SourceType = 'source' | 'generated'

export const SOURCE_TYPE_VALUES: readonly SourceType[] = [
  'source',
  'generated',
] as const

export const SOURCE_TYPE_META: Record<SourceType, EnumMeta> = {
  // 直接来自版面的原子（derived_from 为空）
  "source": { value: 'source', label: "原文", severity: 'neutral' },
  // 模型生成的理解（derived_from 指向原子）
  "generated": { value: 'generated', label: "生成理解", severity: 'warn' },
}

export function sourceTypeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return SOURCE_TYPE_META[value as SourceType]?.label ?? `未知取值（${value}）`
}

// DDP-Layout v1.1 的块类型词汇表 —— **契约的一部分**。
// 每个引擎的 normalizer 都必须产出这八个值之一；认不出来的归 `other`
// （不是丢弃 —— 丢弃会让新引擎的块凭空消失）。
// 规范实现在 `ddp_core.blocks.normalize_type`，守卫在
// `scripts/check_blocktype_parity.py`。
export type BlockType = 'text' | 'title' | 'code' | 'table' | 'figure' | 'equation' | 'list' | 'other'

export const BLOCK_TYPE_VALUES: readonly BlockType[] = [
  'text',
  'title',
  'code',
  'table',
  'figure',
  'equation',
  'list',
  'other',
] as const

export const BLOCK_TYPE_META: Record<BlockType, EnumMeta> = {
  // 正文段落。也是"压根没有 type"时的默认
  "text": { value: 'text', label: "正文", severity: 'neutral' },
  // 各级标题
  "title": { value: 'title', label: "标题", severity: 'neutral' },
  // 代码块
  "code": { value: 'code', label: "代码", severity: 'neutral' },
  // 表格（table_html 可能有值）
  "table": { value: 'table', label: "表格", severity: 'neutral' },
  // 图。**无 caption 也要产出原子**，否则视觉链路没输入
  "figure": { value: 'figure', label: "图", severity: 'neutral' },
  // 行间公式
  "equation": { value: 'equation', label: "公式", severity: 'neutral' },
  // 列表
  "list": { value: 'list', label: "列表", severity: 'neutral' },
  // 有 type 但不在映射表里 —— 与「压根没有 type」要分开，后者归 text
  "other": { value: 'other', label: "其它", severity: 'neutral' },
}

export function blockTypeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return BLOCK_TYPE_META[value as BlockType]?.label ?? `未知取值（${value}）`
}

// 计量流水的种类。`extract` 按**字段数**计 requests：一次抽取 = N 次检索
// + N 次模型调用，按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜。
export type UsageKind = 'parse' | 'chat' | 'embeddings' | 'mcp' | 'qa' | 'embed' | 'compile_vision' | 'extract' | 'knowledge' | 'federated_execution' | 'federated_delivery'

export const USAGE_KIND_VALUES: readonly UsageKind[] = [
  'parse',
  'chat',
  'embeddings',
  'mcp',
  'qa',
  'embed',
  'compile_vision',
  'extract',
  'knowledge',
  'federated_execution',
  'federated_delivery',
] as const

export const USAGE_KIND_META: Record<UsageKind, EnumMeta> = {
  // 文档解析，按页计
  "parse": { value: 'parse', label: "解析", severity: 'neutral' },
  // 对外 chat 代理，按次计
  "chat": { value: 'chat', label: "对话", severity: 'neutral' },
  // 对外向量化代理
  "embeddings": { value: 'embeddings', label: "向量化", severity: 'neutral' },
  // MCP 工具调用
  "mcp": { value: 'mcp', label: "MCP 调用", severity: 'neutral' },
  // 站内问答
  "qa": { value: 'qa', label: "问答", severity: 'neutral' },
  // 索引时的向量化
  "embed": { value: 'embed', label: "索引向量化", severity: 'neutral' },
  // 编译期的视觉理解调用
  "compile_vision": { value: 'compile_vision', label: "视觉理解", severity: 'neutral' },
  // 结构化抽取，按字段数计
  "extract": { value: 'extract', label: "结构化抽取", severity: 'neutral' },
  // 图谱 / wiki 生成，按次计
  "knowledge": { value: 'knowledge', label: "知识生成", severity: 'neutral' },
  // 本节点作为联邦执行者完成一次受理的执行（按执行任务号恰好一次）
  "federated_execution": { value: 'federated_execution', label: "联邦执行", severity: 'neutral' },
  // 本节点协调的联邦任务产出一份可交付结果（按根任务恰好一次，补做不重复）
  "federated_delivery": { value: 'federated_delivery', label: "联邦交付", severity: 'neutral' },
}

export function usageKindLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return USAGE_KIND_META[value as UsageKind]?.label ?? `未知取值（${value}）`
}

// 调用者身份类型。corpus-api **不自己验用户凭据**，它只信任 control-api
// 在内部调用里下发的 `X-DDP-Actor-Kind` + `X-DDP-Actor`。
export type ActorKind = 'user' | 'api_key' | 'service'

export const ACTOR_KIND_VALUES: readonly ActorKind[] = [
  'user',
  'api_key',
  'service',
] as const

export const ACTOR_KIND_META: Record<ActorKind, EnumMeta> = {
  // 浏览器会话（JWT / OIDC）
  "user": { value: 'user', label: "用户", severity: 'neutral' },
  // sk- 开头的对外 key
  "api_key": { value: 'api_key', label: "API Key", severity: 'neutral' },
  // 服务间调用（服务凭据）
  "service": { value: 'service', label: "服务", severity: 'neutral' },
}

export function actorKindLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return ACTOR_KIND_META[value as ActorKind]?.label ?? `未知取值（${value}）`
}

// 组织内角色（RBAC）。**首发是单组织独占部署**，一次部署 = 一份语料，
// 组织内成员共享语料；角色控制的是"能做什么"，不是"能看见什么"。
export type Role = 'viewer' | 'contributor' | 'reviewer' | 'admin'

export const ROLE_VALUES: readonly Role[] = [
  'viewer',
  'contributor',
  'reviewer',
  'admin',
] as const

export const ROLE_META: Record<Role, EnumMeta> = {
  // 只读：检索、问答、看证据
  "viewer": { value: 'viewer', label: "只读成员", severity: 'neutral' },
  // viewer + 上传、重解析、发起抽取
  "contributor": { value: 'contributor', label: "贡献者", severity: 'neutral' },
  // contributor + 复核队列、确认/驳回知识条目
  "reviewer": { value: 'reviewer', label: "复核员", severity: 'neutral' },
  // 全部 + 成员管理、API key、配额、删除
  "admin": { value: 'admin', label: "管理员", severity: 'neutral' },
}

export function roleLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return ROLE_META[value as Role]?.label ?? `未知取值（${value}）`
}

// 持久任务的状态机（§10）。**领取必须带 generation fencing**：
// lease 只解决"谁可以接管"，最终写入还要比 generation —— 否则被判死的
// 旧 worker 迟到写入会覆盖新结果。
export type TaskStatus = 'queued' | 'claimed' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export const TASK_STATUS_VALUES: readonly TaskStatus[] = [
  'queued',
  'claimed',
  'running',
  'succeeded',
  'failed',
  'cancelled',
] as const

export const TASK_STATUS_META: Record<TaskStatus, EnumMeta> = {
  // 已落库等待领取
  "queued": { value: 'queued', label: "排队中", severity: 'neutral', active: true },
  // 已被某个 worker 领取（带 lease_until）
  "claimed": { value: 'claimed', label: "已领取", severity: 'progress', active: true },
  // 正在执行，靠 heartbeat 续租
  "running": { value: 'running', label: "执行中", severity: 'progress', active: true },
  // 完成
  "succeeded": { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 失败，失败原因必须持久化并在 UI 可见
  "failed": { value: 'failed', label: "失败", severity: 'error' },
  // 被显式取消。**终态，迟到的成功/失败写入一律被 generation + 状态守卫拒绝**。
  // 与 failed 分开是因为"用户不想要了"和"系统做砸了"对用户是两件事：
  // 前者不该进失败告警，后者必须留失败原因。
  "cancelled": { value: 'cancelled', label: "已取消", severity: 'warn' },
}

export function taskStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return TASK_STATUS_META[value as TaskStatus]?.label ?? `未知取值（${value}）`
}

// 持久任务的种类。每种**分别设并发与队列**，不共用一个无量纲总并发。
export type TaskKind = 'parse_poll' | 'compile' | 'index' | 'extract' | 'knowledge' | 'gc' | 'federation_execute' | 'federation_plan'

export const TASK_KIND_VALUES: readonly TaskKind[] = [
  'parse_poll',
  'compile',
  'index',
  'extract',
  'knowledge',
  'gc',
  'federation_execute',
  'federation_plan',
] as const

export const TASK_KIND_META: Record<TaskKind, EnumMeta> = {
  // 轮询解析引擎并归档结果
  "parse_poll": { value: 'parse_poll', label: "解析归档", severity: 'neutral' },
  // 版面编译（含视觉理解）
  "compile": { value: 'compile', label: "版面编译", severity: 'neutral' },
  // 分块 + 向量化 + 写索引
  "index": { value: 'index', label: "建立索引", severity: 'neutral' },
  // 结构化抽取批次
  "extract": { value: 'extract', label: "结构化抽取", severity: 'neutral' },
  // 图谱 / wiki 生成
  "knowledge": { value: 'knowledge', label: "知识生成", severity: 'neutral' },
  // 对象回收（带宽限期）
  "gc": { value: 'gc', label: "对象回收", severity: 'neutral' },
  // 联邦节点侧的单步执行（`federation.execute`）。受理与执行行先提交、
  // 再排这个任务 —— 进程重启后由别的 worker 按租约接管，已受理的执行
  // 不会永远停在 queued/running（不变式 7）。
  "federation_execute": { value: 'federation_execute', label: "联邦执行", severity: 'neutral' },
  // 联邦协调者推进一个已批准计划（`federation_tasks._execute_plan`）。
  // 与节点侧分开成两种任务，协调者等待本地执行时不会占满执行池
  // （否则单池会被"等子任务的父任务"堵死）。
  "federation_plan": { value: 'federation_plan', label: "联邦计划执行", severity: 'neutral' },
}

export function taskKindLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return TASK_KIND_META[value as TaskKind]?.label ?? `未知取值（${value}）`
}

// 直传上传会话的状态（§9.1）。**`verifying` 不能跳过**：服务端没校验完
// 对象大小与摘要之前，文档不得进入解析 —— 否则等于信任客户端声明的哈希。
export type UploadStatus = 'created' | 'uploading' | 'verifying' | 'ready' | 'failed' | 'expired'

export const UPLOAD_STATUS_VALUES: readonly UploadStatus[] = [
  'created',
  'uploading',
  'verifying',
  'ready',
  'failed',
  'expired',
] as const

export const UPLOAD_STATUS_META: Record<UploadStatus, EnumMeta> = {
  // 会话已创建，预签名已下发
  "created": { value: 'created', label: "待上传", severity: 'neutral', active: true },
  // 客户端正在分片上传
  "uploading": { value: 'uploading', label: "上传中", severity: 'progress', active: true },
  // 已 finalize，服务端正在校验摘要
  "verifying": { value: 'verifying', label: "校验中", severity: 'progress', active: true },
  // 校验通过，已发出 DocumentSubmitted
  "ready": { value: 'ready', label: "已就绪", severity: 'ok' },
  // 校验失败或客户端放弃
  "failed": { value: 'failed', label: "失败", severity: 'error' },
  // 预签名过期未完成
  "expired": { value: 'expired', label: "已过期", severity: 'warn' },
}

export function uploadStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return UPLOAD_STATUS_META[value as UploadStatus]?.label ?? `未知取值（${value}）`
}

// 永久上传字节就绪（`upload_status=ready`）之后的**语料登记确认**：control 把
// `DocumentSubmitted` 投递给语料域，只有 2xx 或 `409 duplicate_event` 算确认。
// 与 `upload_status` 是两段：`ready` 只表示已登记，**不是解析/索引完成**。
// 字节未就绪、或临时计算上传时，该字段为 null。
export type IngestStatus = 'pending' | 'retrying' | 'ready' | 'rejected'

export const INGEST_STATUS_VALUES: readonly IngestStatus[] = [
  'pending',
  'retrying',
  'ready',
  'rejected',
] as const

export const INGEST_STATUS_META: Record<IngestStatus, EnumMeta> = {
  // 登记事件尚未得到确认（含事件尚未落库）
  "pending": { value: 'pending', label: "登记中", severity: 'progress', active: true },
  // 投递遇到暂时故障，按退避重试同一事件
  "retrying": { value: 'retrying', label: "登记重试中", severity: 'warn', active: true },
  // 语料域已确认登记；解析/索引另行展示
  "ready": { value: 'ready', label: "已登记", severity: 'ok' },
  // 语料域确定性拒绝，终态，不再重投
  "rejected": { value: 'rejected', label: "登记被拒绝", severity: 'error' },
}

export function ingestStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return INGEST_STATUS_META[value as IngestStatus]?.label ?? `未知取值（${value}）`
}

// `ingest_status=rejected` 时 `ingest_error` 的取值：语料域对 `DocumentSubmitted`
// 的**确定性**拒绝码。投递器只把这一组当终态；其余非 2xx（含 5xx、
// `document_state_changed`、校验错误）一律按暂时故障重试 —— 把可恢复的失败
// 判成终态会让一份已校验的上传永远进不了语料库。
export type IngestRejection = 'resource_not_found' | 'resource_version_exists' | 'invalid_upload_target' | 'idempotency_conflict' | 'source_missing'

export const INGEST_REJECTION_VALUES: readonly IngestRejection[] = [
  'resource_not_found',
  'resource_version_exists',
  'invalid_upload_target',
  'idempotency_conflict',
  'source_missing',
] as const

export const INGEST_REJECTION_META: Record<IngestRejection, EnumMeta> = {
  // 目标资源已删除、已撤回或不属于上传者
  "resource_not_found": { value: 'resource_not_found', label: "目标资源不存在、已撤回或无权追加", severity: 'error' },
  // 相同字节已是目标资源的一个固定版本
  "resource_version_exists": { value: 'resource_version_exists', label: "该内容已是目标资源的一个版本", severity: 'error' },
  // 目标字段非法，或临时计算上传带了目标
  "invalid_upload_target": { value: 'invalid_upload_target', label: "目标资源无效", severity: 'error' },
  // 同一登记键绑定了不同的输入
  "idempotency_conflict": { value: 'idempotency_conflict', label: "登记幂等键冲突", severity: 'error' },
  // 内容对应的原件已不可用
  "source_missing": { value: 'source_missing', label: "原件已不可用", severity: 'error' },
}

export function ingestRejectionLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return INGEST_REJECTION_META[value as IngestRejection]?.label ?? `未知取值（${value}）`
}

// 版本化 Wiki 修订 `stale_reasons` 里按页给出的原因（读时依据来源的当前状态现算）。
// 页面只在它对某资源的依赖**没有一条**落在最新版本时才因新版本而过期（wiki-format）。
export type WikiStaleReason = 'source_version_changed' | 'parse_revision_changed' | 'source_digest_changed' | 'permission_unresolved' | 'source_withdrawn' | 'source_unavailable'

export const WIKI_STALE_REASON_VALUES: readonly WikiStaleReason[] = [
  'source_version_changed',
  'parse_revision_changed',
  'source_digest_changed',
  'permission_unresolved',
  'source_withdrawn',
  'source_unavailable',
] as const

export const WIKI_STALE_REASON_META: Record<WikiStaleReason, EnumMeta> = {
  // 该页依赖的资源有了更新的固定版本
  "source_version_changed": { value: 'source_version_changed', label: "来源有了新版本", severity: 'warn' },
  // 固定版本绑定的解析修订与依赖记录不一致
  "parse_revision_changed": { value: 'parse_revision_changed', label: "来源的解析修订已变化", severity: 'warn' },
  // 版本或原始证据的摘要与依赖记录不一致
  "source_digest_changed": { value: 'source_digest_changed', label: "来源原文摘要已变化", severity: 'warn' },
  // 跨节点来源的授权无法确认
  "permission_unresolved": { value: 'permission_unresolved', label: "来源授权无法确认", severity: 'warn' },
  // 依赖的固定版本已被撤回（本机工作区可撤回单个版本）
  "source_withdrawn": { value: 'source_withdrawn', label: "来源版本已撤回", severity: 'warn' },
  // 依赖的固定版本或其原始证据已读不到（删除、未就绪）
  "source_unavailable": { value: 'source_unavailable', label: "来源版本不可用", severity: 'warn' },
}

export function wikiStaleReasonLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return WIKI_STALE_REASON_META[value as WikiStaleReason]?.label ?? `未知取值（${value}）`
}

// `ScopeManifest` 的成员枚举状态（计划 §5.4）。**这是「查了哪里」这句话
// 的分母**：分母没封上就没有百分比可言。
//
// `partial` 与 `expired` 必须与 `sealed` 严格分开：把无法展开的子域
// 当成空目录，等于用"那里没有资料"冒充"我没能去看"。
export type EnumerationState = 'building' | 'sealed' | 'partial' | 'expired'

export const ENUMERATION_STATE_VALUES: readonly EnumerationState[] = [
  'building',
  'sealed',
  'partial',
  'expired',
] as const

export const ENUMERATION_STATE_META: Record<EnumerationState, EnumMeta> = {
  // 正在逐个目录取分页快照，还没封存
  "building": { value: 'building', label: "正在确定检索范围", severity: 'progress', active: true },
  // 全部获准目录都取到稳定快照且已去重封存，可重放。
  // **只有这个值允许后续声明 retrieval=complete。**
  "sealed": { value: 'sealed', label: "检索范围已确定", severity: 'ok' },
  // 有子目录超时、拒绝或不支持枚举。未展开子域记在
  // `unexpanded_subtrees[]`，**不得当成空集**，也不得给出真实总数。
  "partial": { value: 'partial', label: "检索范围不完整（部分下级目录无法展开）", severity: 'warn' },
  // 快照有效期已过或枚举游标失效。不能把不同分页时代的列表拼成
  // "完整快照"（§5.5）—— 要重新枚举生成新 scope。
  "expired": { value: 'expired', label: "检索范围已过期，需重新确定", severity: 'warn' },
}

export function enumerationStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return ENUMERATION_STATE_META[value as EnumerationState]?.label ?? `未知取值（${value}）`
}

// 覆盖账本里**单个目标**的状态（计划 §7.4）。目标键是
// `(origin_node_id, collection_id, operation)` —— 一台服务器有多个集合时，
// 探测了其中一个**不能**把整台标成完成（计划 T85）。
//
// `unsupported` 可以结束对该目标的发现处理，但**它不表示在那里完成了
// 全文检索**；报告时它进"排除数"，不进"成功检索数"。
export type CoverageTargetState = 'planned' | 'in_flight' | 'succeeded' | 'partial' | 'denied' | 'failed' | 'unsupported' | 'unreachable' | 'not_attempted' | 'revoked'

export const COVERAGE_TARGET_STATE_VALUES: readonly CoverageTargetState[] = [
  'planned',
  'in_flight',
  'succeeded',
  'partial',
  'denied',
  'failed',
  'unsupported',
  'unreachable',
  'not_attempted',
  'revoked',
] as const

export const COVERAGE_TARGET_STATE_META: Record<CoverageTargetState, EnumMeta> = {
  // 已进入本次范围，尚未发出请求
  "planned": { value: 'planned', label: "待检索", severity: 'neutral', active: true },
  // 请求已发出，还没有回执
  "in_flight": { value: 'in_flight', label: "检索中", severity: 'progress', active: true },
  // 拿到有效且完成的检索回执
  "succeeded": { value: 'succeeded', label: "已检索", severity: 'ok' },
  // 目标自己报了内部限制（分片失败、索引落后、只查了子集）。
  // **算缺口，不算完成** —— 节点外层写 completed 而内部有 partial
  // 是计划 §6.4 明确禁止的。
  "partial": { value: 'partial', label: "部分检索（对方报告内部不完整）", severity: 'warn' },
  // 鉴权通过但该目标拒绝本次操作
  "denied": { value: 'denied', label: "对方拒绝", severity: 'warn' },
  // 请求出错（非超时）
  "failed": { value: 'failed', label: "检索失败", severity: 'error' },
  // 已核实该目标不支持所需 operation。**只有可核验依据才能记这个值** ——
  // 能力元数据过期或缺失一律算 unknown/未完成，不得直接排除（§7.3）。
  "unsupported": { value: 'unsupported', label: "对方不支持该操作", severity: 'neutral' },
  // 超时或连不上
  "unreachable": { value: 'unreachable', label: "无法连接", severity: 'error' },
  // 预算耗尽 / 任务取消 / 范围过期导致压根没发出。**不是"没有资料"**
  "not_attempted": { value: 'not_attempted', label: "未检索（预算或取消）", severity: 'warn' },
  // 成员在范围封存后被撤销。**留在分母里**（§5.5）——
  // 从分母删掉来把完成率做漂亮是明确禁止的。
  "revoked": { value: 'revoked', label: "成员已撤销（保留在范围内）", severity: 'warn' },
}

export function coverageTargetStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return COVERAGE_TARGET_STATE_META[value as CoverageTargetState]?.label ?? `未知取值（${value}）`
}

// 整个任务的检索完成度（计划 §7.4）。
//
// `complete` 的判据是**合取**，缺一条都不许写：
//   ① `enumeration_state == sealed` 且无未展开子域；
//   ② 所有适用且已授权的目标都返回有效、完成的回执；
//   ③ 没有 in_flight / not_attempted / unreachable / denied / revoked，
//      也没有任何目标自报 partial；
//   ④ 每个被排除的目标都有可核验依据。
//
// 快速模式**永远不允许**写 complete（§7.2）：它只完成了自己选中的候选，
// 所以它报的是 `partial` 加上"未检索范围"。
export type RetrievalCompleteness = 'not_started' | 'partial' | 'complete'

export const RETRIEVAL_COMPLETENESS_VALUES: readonly RetrievalCompleteness[] = [
  'not_started',
  'partial',
  'complete',
] as const

export const RETRIEVAL_COMPLETENESS_META: Record<RetrievalCompleteness, EnumMeta> = {
  // 范围还没封存或还没开始检索
  "not_started": { value: 'not_started', label: "尚未检索", severity: 'neutral', active: true },
  // 有目标未完成，或本轮是 fast 模式。**fast 模式的成功结局也是这个值**
  // —— 它必须同时给出未检索范围，不能因为选中的候选全成功就报完成。
  "partial": { value: 'partial', label: "部分范围已检索", severity: 'warn' },
  // 上述四条合取全部成立。**这仍然不代表证据充分或结论正确。**
  "complete": { value: 'complete', label: "声明范围内已全部检索", severity: 'ok' },
}

export function retrievalCompletenessLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return RETRIEVAL_COMPLETENESS_META[value as RetrievalCompleteness]?.label ?? `未知取值（${value}）`
}

// 证据充分性（计划 §7.4 第三轴）。与检索完成度**严格分开**：
// 「该查的都查了」和「查到的够回答」是两件事，而
// 「够回答」和「答对了」又是第三件事（那一件靠人工评审，不进这个枚举）。
//
// `conflicting` 不是 `insufficient` 的变体：矛盾证据意味着拿到了实质内容
// 但来源互相打架，界面上要让用户看见冲突，而不是折叠成"资料不足"。
export type EvidenceSufficiency = 'sufficient_by_policy' | 'insufficient' | 'conflicting' | 'unknown'

export const EVIDENCE_SUFFICIENCY_VALUES: readonly EvidenceSufficiency[] = [
  'sufficient_by_policy',
  'insufficient',
  'conflicting',
  'unknown',
] as const

export const EVIDENCE_SUFFICIENCY_META: Record<EvidenceSufficiency, EnumMeta> = {
  // 按当次策略判定证据足够。名字里的 `by_policy` 是刻意的 ——
  // 它是**按规则判的**，不是"客观上充分"，更不是 LLM 自报信心
  // （§7.2 明确禁止把自报信心当唯一早停条件）。
  "sufficient_by_policy": { value: 'sufficient_by_policy', label: "证据满足本次策略要求", severity: 'ok' },
  // 没有足够证据支撑结论，必须如实说不足
  "insufficient": { value: 'insufficient', label: "证据不足", severity: 'warn' },
  // 多来源证据互相矛盾（含同一资料的不同版本）。要展示冲突，不要挑一个。优先级低于 insufficient / unknown：证据本身不足时报不足，矛盾记录照样保留
  "conflicting": { value: 'conflicting', label: "证据存在矛盾", severity: 'warn' },
  // 还没评估（检索未完成 / 评估器不可用）
  "unknown": { value: 'unknown', label: "证据充分性未知", severity: 'neutral' },
}

export function evidenceSufficiencyLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return EVIDENCE_SUFFICIENCY_META[value as EvidenceSufficiency]?.label ?? `未知取值（${value}）`
}

// 一条证据矛盾记录**凭什么**成立（计划 §7.6：同时处理矛盾证据，不挑一个）。
// 两种依据都只能把 `sufficient_by_policy` 压成 `conflicting`，**不能**抬高它，
// 也**不能**把 `insufficient` / `unknown` 改写成 `conflicting`（那会藏掉"证据不足"
// 并放行不该发生的生成）—— 这两种情况下矛盾记录照样保留、照样可见。
// 模型说"有矛盾"最多让界面多一个警告，而模型说"没矛盾"不改变任何东西。
// 每条记录都要人看（`semantic_review=needs_review`），这里记的是"值得复核的
// 矛盾"，不是裁决。
export type EvidenceConflictBasis = 'version_divergence' | 'generation_reported'

export const EVIDENCE_CONFLICT_BASIS_VALUES: readonly EvidenceConflictBasis[] = [
  'version_divergence',
  'generation_reported',
] as const

export const EVIDENCE_CONFLICT_BASIS_META: Record<EvidenceConflictBasis, EnumMeta> = {
  // 规则判定：同一来源（同节点、同资源）的不同固定版本在**同一定位**
  // （物理页 + 块序）上取回了不同正文。只看结构，不读语义。
  "version_divergence": { value: 'version_divergence', label: "同一资料的版本不一致", severity: 'warn' },
  // 带出处生成时模型标出的矛盾引用对。只在引用全部落在本次证据编号域、
  // 且至少指向两条不同证据时才采信；引用不成立则整份答案作废。
  "generation_reported": { value: 'generation_reported', label: "生成时标出的矛盾", severity: 'warn' },
}

export function evidenceConflictBasisLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return EVIDENCE_CONFLICT_BASIS_META[value as EvidenceConflictBasis]?.label ?? `未知取值（${value}）`
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
export type FederatedAnswerReason = 'insufficient_evidence' | 'local_model_missing' | 'evidence_excerpt_unavailable' | 'excerpt_over_contract_bound' | 'upstream_error' | 'no_model_output' | 'budget_exceeded' | 'root_budget_exhausted' | 'unsupported_generation' | 'delegated_answer_missing' | 'delegated_answer_rejected' | 'delegated_bindings_missing' | 'delegated_binding_out_of_scope' | 'delegated_conflict_out_of_scope' | 'evidence_delegation_over_limit' | 'invalid_admission_receipt' | 'receipt_binding_mismatch' | 'delegated_admission_not_accepted' | 'delegated_execution_failed' | 'peer_unavailable'

export const FEDERATED_ANSWER_REASON_VALUES: readonly FederatedAnswerReason[] = [
  'insufficient_evidence',
  'local_model_missing',
  'evidence_excerpt_unavailable',
  'excerpt_over_contract_bound',
  'upstream_error',
  'no_model_output',
  'budget_exceeded',
  'root_budget_exhausted',
  'unsupported_generation',
  'delegated_answer_missing',
  'delegated_answer_rejected',
  'delegated_bindings_missing',
  'delegated_binding_out_of_scope',
  'delegated_conflict_out_of_scope',
  'evidence_delegation_over_limit',
  'invalid_admission_receipt',
  'receipt_binding_mismatch',
  'delegated_admission_not_accepted',
  'delegated_execution_failed',
  'peer_unavailable',
] as const

export const FEDERATED_ANSWER_REASON_META: Record<FederatedAnswerReason, EnumMeta> = {
  // 证据不足（或没有可引用证据），不给模型凭常识补答的机会
  "insufficient_evidence": { value: 'insufficient_evidence', label: "证据不足，未生成答案", severity: 'warn' },
  // 本节点与计划内远端都没有可用的生成能力（或生成预算为 0）
  "local_model_missing": { value: 'local_model_missing', label: "没有可用的生成模型，只返回证据", severity: 'warn' },
  // 某条证据取不到正文（空白或缺失），不能拿无根片段生成
  "evidence_excerpt_unavailable": { value: 'evidence_excerpt_unavailable', label: "有证据取不到原文片段，未生成答案", severity: 'warn' },
  // 证据正文超过契约上限（2000 字符），显式拒绝而不是静默截断
  "excerpt_over_contract_bound": { value: 'excerpt_over_contract_bound', label: "证据片段超出长度上限，未生成答案", severity: 'warn' },
  // 调生成模型的请求失败或返回非 200
  "upstream_error": { value: 'upstream_error', label: "生成服务出错，只返回证据", severity: 'error' },
  // 模型没有返回可用文本
  "no_model_output": { value: 'no_model_output', label: "模型没有输出，只返回证据", severity: 'error' },
  // 生成结果超出计划的生成 token 预算
  "budget_exceeded": { value: 'budget_exceeded', label: "超出生成预算，答案作废", severity: 'error' },
  // 本任务的根预算（请求、字节、跳数、生成 token 或截止时间）在生成这一步用完，没有拿到生成结果；不是模型输出超出 token 预算
  "root_budget_exhausted": { value: 'root_budget_exhausted', label: "任务预算已用完，未生成答案", severity: 'warn' },
  // 生成文本的引用结构不成立（无引用、越界引用、矛盾标注不成立）
  "unsupported_generation": { value: 'unsupported_generation', label: "生成的答案引用不成立，已作废", severity: 'error' },
  // 远端执行完成但没有返回答案文档
  "delegated_answer_missing": { value: 'delegated_answer_missing', label: "远端没有返回答案", severity: 'error' },
  // 远端答案校验未通过；远端自报的原因认不出来时放进细节
  "delegated_answer_rejected": { value: 'delegated_answer_rejected', label: "远端答案未通过校验", severity: 'error' },
  // 远端答案没有任何主张绑定
  "delegated_bindings_missing": { value: 'delegated_bindings_missing', label: "远端答案没有引用，已作废", severity: 'error' },
  // 远端答案的引用不在本次发送的证据里
  "delegated_binding_out_of_scope": { value: 'delegated_binding_out_of_scope', label: "远端答案引用了未发送的证据，已作废", severity: 'error' },
  // 远端标出的矛盾引用不在本次发送的证据里
  "delegated_conflict_out_of_scope": { value: 'delegated_conflict_out_of_scope', label: "远端标注的矛盾引用不成立，答案已作废", severity: 'error' },
  // 要委托的证据条数超过受理上限，不截断证据去凑数
  "evidence_delegation_over_limit": { value: 'evidence_delegation_over_limit', label: "证据条数超过委托上限，未生成答案", severity: 'warn' },
  // 远端受理回执缺执行任务号
  "invalid_admission_receipt": { value: 'invalid_admission_receipt', label: "远端受理回执无效", severity: 'error' },
  // 远端回执与本次 root/step/幂等键/计划修订/执行者对不上；细节是出错字段（回执不是对象时为 schema）
  "receipt_binding_mismatch": { value: 'receipt_binding_mismatch', label: "远端回执与本次任务对不上，未采用", severity: 'error' },
  // 远端没有受理答案步骤；细节是回执状态（如 waiting_input / rejected）
  "delegated_admission_not_accepted": { value: 'delegated_admission_not_accepted', label: "远端未受理生成请求", severity: 'error' },
  // 远端答案执行没有成功；细节是对端错误码或状态（含本节点轮询超时 peer_execution_timeout）
  "delegated_execution_failed": { value: 'delegated_execution_failed', label: "远端生成步骤未完成", severity: 'error' },
  // 远端生成节点未登记、连不上、回 HTTP 错误或返回非法响应；细节是对端错误码、http_状态或 transport
  "peer_unavailable": { value: 'peer_unavailable', label: "远端生成节点不可用", severity: 'error' },
}

export function federatedAnswerReasonLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return FEDERATED_ANSWER_REASON_META[value as FederatedAnswerReason]?.label ?? `未知取值（${value}）`
}

// 协调者入口（`POST /api/v1/task-intents`）**受理哪些 TaskSpec.operation**。
// 这是一个闭集：认不出来的 operation 当场拒绝，不许落库。
//
// 为什么必须闭集：规划只按 operation 决定要不要加生成步骤。以前不看 operation，
// 本地模型就绪时**任何** operation 都会被追加一个 `answer` 步 —— 提交
// `corpus.retrieve`（只取证据）会白跑一次生成；未登记的操作必须明确拒绝，
// 不得用 RAG 答案冒充其他业务产物。
//
// 本地运行时的 TaskSpec 还有别的 operation（本机自己的计划许可），不受这里约束。
export type FederationTaskOperation = 'corpus.retrieve' | 'rag.answer.cited' | 'wiki.pages'

export const FEDERATION_TASK_OPERATION_VALUES: readonly FederationTaskOperation[] = [
  'corpus.retrieve',
  'rag.answer.cited',
  'wiki.pages',
] as const

export const FEDERATION_TASK_OPERATION_META: Record<FederationTaskOperation, EnumMeta> = {
  // 只按范围取证据，不生成结论
  "corpus.retrieve": { value: 'corpus.retrieve', label: "只取证据", severity: 'neutral' },
  // 取证据并生成带出处的回答
  "rag.answer.cited": { value: 'rag.answer.cited', label: "带出处的回答", severity: 'neutral' },
  // 按固定原始证据生成并验证版本化 Wiki 草稿
  "wiki.pages": { value: 'wiki.pages', label: "构建 Wiki 草稿", severity: 'neutral' },
}

export function federationTaskOperationLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return FEDERATION_TASK_OPERATION_META[value as FederationTaskOperation]?.label ?? `未知取值（${value}）`
}

// 联邦任务事件流（`GET /api/v1/tasks/{root_task_id}/events`）里 `Event.type` 的取值。
// 事件是**可恢复的进度记录**，不是状态真相：状态以 `TaskStatus` 各轴为准，事件用来
// 让界面说清"发生了什么、什么时候"，断线后按 `after=next_seq` 续读不丢。
export type TaskEventType = 'intent_created' | 'plan_ready' | 'plan_approved' | 'execution_started' | 'task_resumed' | 'task_completed' | 'task_failed' | 'task_cancelled' | 'delivery_pending' | 'delivery_confirmed' | 'delivery_expired'

export const TASK_EVENT_TYPE_VALUES: readonly TaskEventType[] = [
  'intent_created',
  'plan_ready',
  'plan_approved',
  'execution_started',
  'task_resumed',
  'task_completed',
  'task_failed',
  'task_cancelled',
  'delivery_pending',
  'delivery_confirmed',
  'delivery_expired',
] as const

export const TASK_EVENT_TYPE_META: Record<TaskEventType, EnumMeta> = {
  // 任务需求与探索许可已落库
  "intent_created": { value: 'intent_created', label: "已创建任务", severity: 'neutral' },
  // 规划完成（Probe 与计划修订已生成），等待批准
  "plan_ready": { value: 'plan_ready', label: "计划已生成，等待批准", severity: 'neutral' },
  // 用户批准了这一修订与执行许可
  "plan_approved": { value: 'plan_approved', label: "已批准计划", severity: 'ok' },
  // 执行已受理并排入持久队列
  "execution_started": { value: 'execution_started', label: "开始执行", severity: 'progress' },
  // 重新判权后补做未完成目标（执行代次 +1）
  "task_resumed": { value: 'task_resumed', label: "补做未完成目标", severity: 'progress' },
  // 执行结束且至少有目标产出证据（查全与否看覆盖账本）
  "task_completed": { value: 'task_completed', label: "执行结束", severity: 'ok' },
  // 执行失败（没有任何目标产出证据，或协调者被清扫）
  "task_failed": { value: 'task_failed', label: "执行失败", severity: 'error' },
  // 用户显式取消；终态，迟到结果不许覆盖
  "task_cancelled": { value: 'task_cancelled', label: "已取消", severity: 'warn' },
  // 结果已固化为交付文档，等待下载后校验确认
  "delivery_pending": { value: 'delivery_pending', label: "结果待确认", severity: 'neutral' },
  // 客户端校验摘要后确认了交付
  "delivery_confirmed": { value: 'delivery_confirmed', label: "结果已确认", severity: 'ok' },
  // 交付在有效期内没有被确认
  "delivery_expired": { value: 'delivery_expired', label: "结果交付已过期", severity: 'warn' },
}

export function taskEventTypeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return TASK_EVENT_TYPE_META[value as TaskEventType]?.label ?? `未知取值（${value}）`
}

// TaskPlan 的规划轴（计划 §8.1）。`invalidated` 是关键一态：
// 计划过期、输入版本变了、授权被撤销之后，**旧计划不许被执行**，
// 要重新规划并重新批准（§6.6 接单时重新检查）。
export type PlanningState = 'draft' | 'exploring' | 'ready' | 'awaiting_approval' | 'approved' | 'invalidated'

export const PLANNING_STATE_VALUES: readonly PlanningState[] = [
  'draft',
  'exploring',
  'ready',
  'awaiting_approval',
  'approved',
  'invalidated',
] as const

export const PLANNING_STATE_META: Record<PlanningState, EnumMeta> = {
  // TaskSpec 已建，还没探测
  "draft": { value: 'draft', label: "草稿", severity: 'neutral', active: true },
  // 已获探索许可，正在 Probe
  "exploring": { value: 'exploring', label: "正在探测", severity: 'progress', active: true },
  // 计划已生成，等待用户批准外发边界
  "ready": { value: 'ready', label: "计划待批准", severity: 'neutral', active: true },
  // 计划变化超出原许可，暂停等重新批准
  "awaiting_approval": { value: 'awaiting_approval', label: "等待重新批准", severity: 'warn', active: true },
  // 计划与外发边界都已批准，可以接单
  "approved": { value: 'approved', label: "已批准", severity: 'ok' },
  // 计划过期、输入版本变更或授权撤销。**不得凭旧 Probe 放行**
  "invalidated": { value: 'invalidated', label: "计划已失效，需重新规划", severity: 'warn' },
}

export function planningStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return PLANNING_STATE_META[value as PlanningState]?.label ?? `未知取值（${value}）`
}

// 远端执行者的受理轴（计划 §8.1 / §6.6）。
//
// **`unknown` 不等于「没执行」** —— 这是计划 T82 专门要求的区分：
// 回执丢了要先按幂等键对账，不能立刻把有副作用的步骤换个节点重做。
export type AdmissionState = 'not_submitted' | 'waiting_input' | 'checking' | 'accepted' | 'rejected' | 'unknown'

export const ADMISSION_STATE_VALUES: readonly AdmissionState[] = [
  'not_submitted',
  'waiting_input',
  'checking',
  'accepted',
  'rejected',
  'unknown',
] as const

export const ADMISSION_STATE_META: Record<AdmissionState, EnumMeta> = {
  // 还没提交给执行者
  "not_submitted": { value: 'not_submitted', label: "未提交", severity: 'neutral' },
  // 受理会话已建、等输入上传完（§6.6）。**这一态不占 GPU** ——
  // 输入没齐就排队等于占着卡等上传。
  "waiting_input": { value: 'waiting_input', label: "等待输入上传", severity: 'progress', active: true },
  // 服务端正在校验输入摘要与格式
  "checking": { value: 'checking', label: "校验输入中", severity: 'progress', active: true },
  // 已持久受理并返回 AdmissionReceipt（≠ 算力预留）
  "accepted": { value: 'accepted', label: "已受理", severity: 'ok', active: true },
  // 明确拒绝（授权、计划过期、输入不合格、配额）
  "rejected": { value: 'rejected', label: "被拒绝", severity: 'error' },
  // 请求发出了但回执丢失。**必须按幂等键查询对账**，查到已有任务就用它；
  // 不得增加逻辑执行代次，也不得重复计一次成功交付。
  "unknown": { value: 'unknown', label: "受理状态未知（正在对账）", severity: 'warn', active: true },
}

export function admissionStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return ADMISSION_STATE_META[value as AdmissionState]?.label ?? `未知取值（${value}）`
}

// 输出验收轴（计划 §8.1 / §4.3 两层引用校验）。
//
// **结构校验通过 ≠ 内容正确**：`passed` 只表示引用确实存在、版本对得上、
// 定位可授权解析；"原文是否真的支持这个结论"是 `needs_review`
// 要人看的那件事（计划 §14.3 主张支持度，明确不能用引用存在率替代）。
export type ValidationState = 'pending' | 'passed' | 'failed' | 'needs_review'

export const VALIDATION_STATE_VALUES: readonly ValidationState[] = [
  'pending',
  'passed',
  'failed',
  'needs_review',
] as const

export const VALIDATION_STATE_META: Record<ValidationState, EnumMeta> = {
  // 还没校验
  "pending": { value: 'pending', label: "待校验", severity: 'neutral', active: true },
  // 结构校验通过：引用存在、版本正确、定位可解析
  "passed": { value: 'passed', label: "校验通过", severity: 'ok' },
  // 结构校验不通过（虚构引用 / 错版本 / 无权定位）
  "failed": { value: 'failed', label: "校验未通过", severity: 'error' },
  // 需要人工复核语义支持度或冲突
  "needs_review": { value: 'needs_review', label: "需人工复核", severity: 'warn' },
}

export function validationStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return VALIDATION_STATE_META[value as ValidationState]?.label ?? `未知取值（${value}）`
}

// 交付轴（计划 §8.3）。**计算成功不代表本地拿到结果。**
//
// `expired` 必须能显示出来：TTL 到期导致未领取结果失效时，界面上
// **不许**仍然显示"已保存本地"（计划 §8.3 原文要求）。
export type DeliveryState = 'not_requested' | 'pending' | 'transferring' | 'confirmed' | 'expired'

export const DELIVERY_STATE_VALUES: readonly DeliveryState[] = [
  'not_requested',
  'pending',
  'transferring',
  'confirmed',
  'expired',
] as const

export const DELIVERY_STATE_META: Record<DeliveryState, EnumMeta> = {
  // 不需要回传（结果留在中心）
  "not_requested": { value: 'not_requested', label: "无需交付", severity: 'neutral' },
  // 结果已就绪，等待本地领取
  "pending": { value: 'pending', label: "待领取", severity: 'neutral', active: true },
  // 正在下载
  "transferring": { value: 'transferring', label: "传输中", severity: 'progress', active: true },
  // 本地校验 manifest 与文件后已幂等确认
  "confirmed": { value: 'confirmed', label: "已交付", severity: 'ok' },
  // 暂存 TTL 到期，结果已失效。**不得显示成已保存本地**
  "expired": { value: 'expired', label: "交付已过期（结果未领取）", severity: 'error' },
}

export function deliveryStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return DELIVERY_STATE_META[value as DeliveryState]?.label ?? `未知取值（${value}）`
}

// 数据保留类别（计划 §8.1 / §8.3）。**临时处理不自动进入永久语料库** ——
// 远端算一次不等于对方获得了这份资料的长期副本。
//
// `task_pinned` 是给 GC 看的：引用仍被活跃任务或他人合法产物使用时，
// GC 不能删唯一副本（计划 §8.3 末段，项目已有的 `gc.py` 宽限期同理）。
export type RetentionClass = 'temporary' | 'task_pinned' | 'persistent' | 'deleting' | 'deleted'

export const RETENTION_CLASS_VALUES: readonly RetentionClass[] = [
  'temporary',
  'task_pinned',
  'persistent',
  'deleting',
  'deleted',
] as const

export const RETENTION_CLASS_META: Record<RetentionClass, EnumMeta> = {
  // 临时输入/中间产物，按 TTL 清理
  "temporary": { value: 'temporary', label: "临时数据", severity: 'neutral' },
  // 被活跃任务引用，GC 不得回收
  "task_pinned": { value: 'task_pinned', label: "任务占用中", severity: 'neutral' },
  // 已按授权进入永久语料
  "persistent": { value: 'persistent', label: "永久保存", severity: 'ok' },
  // 正在清理（宽限期内可能仍可见）
  "deleting": { value: 'deleting', label: "正在清理", severity: 'progress', active: true },
  // 已清理
  "deleted": { value: 'deleted', label: "已删除", severity: 'neutral' },
}

export function retentionClassLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return RETENTION_CLASS_META[value as RetentionClass]?.label ?? `未知取值（${value}）`
}

// 发布轴（计划 §8.1 / §4.4）。**私有来源的派生页面不能靠切 public 绕过
// 原许可** —— 发布前要检查派生内容的公开权（计划 §4.4、T06）。
export type PublishingState = 'private' | 'draft' | 'published' | 'withdrawn'

export const PUBLISHING_STATE_VALUES: readonly PublishingState[] = [
  'private',
  'draft',
  'published',
  'withdrawn',
] as const

export const PUBLISHING_STATE_META: Record<PublishingState, EnumMeta> = {
  // 仅所有者与获授权者可见
  "private": { value: 'private', label: "私有", severity: 'neutral' },
  // 草稿，未发布
  "draft": { value: 'draft', label: "草稿", severity: 'neutral' },
  // 已按授权范围发布
  "published": { value: 'published', label: "已发布", severity: 'ok' },
  // 已撤回。**不承诺收回已下载副本**
  "withdrawn": { value: 'withdrawn', label: "已撤回", severity: 'warn' },
}

export function publishingStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return PUBLISHING_STATE_META[value as PublishingState]?.label ?? `未知取值（${value}）`
}

// 节点能力的就绪度（计划 §5.2）。**三件事必须分开记**：
// 静态能力配置、周期健康探测、本次任务预检。
//
// `configured` 不代表能用：计划原文举的例子是 `gpu=true` 不代表
// 所需模型已经就绪 —— 这正是本项目踩过的坑的联邦版本
// （注册表里有 OCR 专用模型，抽取平面拿它去抽值，抽不出来被记成
// `not_found`，系统能力缺失伪装成"文档里没有"，见已有的 `no_instruct`）。
export type CapabilityReadiness = 'configured' | 'ready' | 'draining' | 'unhealthy' | 'unknown'

export const CAPABILITY_READINESS_VALUES: readonly CapabilityReadiness[] = [
  'configured',
  'ready',
  'draining',
  'unhealthy',
  'unknown',
] as const

export const CAPABILITY_READINESS_META: Record<CapabilityReadiness, EnumMeta> = {
  // 配置里声明了这个能力，但没有健康证据。**不得当成可用**
  "configured": { value: 'configured', label: "已配置（未验证可用）", severity: 'neutral' },
  // 健康探测通过且当前可接单
  "ready": { value: 'ready', label: "可用", severity: 'ok' },
  // 正在排空，不接新单但在跑的会做完
  "draining": { value: 'draining', label: "正在排空", severity: 'warn' },
  // 健康探测失败
  "unhealthy": { value: 'unhealthy', label: "不可用", severity: 'error' },
  // 没有有效的健康证据（从没探过 / 记录过期）。
  // **过期记录不是当前能力证明**（§5.5），要按未知处理，不许按
  // 最后一次成功当成现在可用。
  "unknown": { value: 'unknown', label: "能力状态未知", severity: 'warn' },
}

export function capabilityReadinessLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return CAPABILITY_READINESS_META[value as CapabilityReadiness]?.label ?? `未知取值（${value}）`
}

// Probe 的输入校验深度（计划 §6.4 / T78）。**这两个值的区别是钱**：
// 只看了文件描述就放进 admission，等于信任客户端声明的哈希 ——
// 本项目在直传上传那里已经踩过同一个坑（upload_status 的 `verifying`
// 不能跳过），联邦侧是同一条规则。
export type InputValidation = 'metadata_only' | 'content_verified'

export const INPUT_VALIDATION_VALUES: readonly InputValidation[] = [
  'metadata_only',
  'content_verified',
] as const

export const INPUT_VALIDATION_META: Record<InputValidation, EnumMeta> = {
  // 只校验了声明的格式/大小/类型，**没收到内容**。
  // 上传阶段只能是这个值，且此时不得占 GPU。
  "metadata_only": { value: 'metadata_only', label: "仅校验元数据", severity: 'warn' },
  // 已收到内容并自己算过摘要校验通过。**预检仍不能排除运行时 OOM 或坏页**
  "content_verified": { value: 'content_verified', label: "已校验内容", severity: 'ok' },
}

export function inputValidationLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return INPUT_VALIDATION_META[value as InputValidation]?.label ?? `未知取值（${value}）`
}

// 检索模式（计划 §6.3 / §7）。**mode 决定怎么查，scope 决定查哪些** ——
// 两者不许互相覆盖：`fast` 不能缩小用户固定的资源范围，
// `local_first`（排序偏好）也不能偷偷变成 `local_only`（外发策略）。
export type SearchMode = 'fast' | 'exhaustive_scope'

export const SEARCH_MODE_VALUES: readonly SearchMode[] = [
  'fast',
  'exhaustive_scope',
] as const

export const SEARCH_MODE_META: Record<SearchMode, EnumMeta> = {
  // 有界选点：摘要排序 + 少量并行 Probe + 有条件扩展。
  // **结局最多是 retrieval=partial**，必须报告未检索范围。
  "fast": { value: 'fast', label: "快速检索（部分范围）", severity: 'neutral' },
  // 按封存的 ScopeManifest 逐个目标实际探测。摘要只影响顺序、不删成员。
  // 即使已经拿到好答案也继续做完，除非用户取消（§7.3）。
  "exhaustive_scope": { value: 'exhaustive_scope', label: "范围穷查", severity: 'neutral' },
}

export function searchModeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return SEARCH_MODE_META[value as SearchMode]?.label ?? `未知取值（${value}）`
}

// `client-runtime` 的连接状态（计划 §3.4）。**与数据状态分开**
// （数据状态见 `snapshot_state`）—— 合起来的后果是
// "一个无关面板订阅失败把整个界面标成服务器断开"，计划明确禁止。
//
// 每个 `(environment_id, authenticated_profile_id)` 只有**一个**重连
// 负责人（计划 T66）；界面组件只订阅状态，不各自开重连循环。
export type TransportState = 'disconnected' | 'connecting' | 'authenticating' | 'ready' | 'backoff' | 'blocked'

export const TRANSPORT_STATE_VALUES: readonly TransportState[] = [
  'disconnected',
  'connecting',
  'authenticating',
  'ready',
  'backoff',
  'blocked',
] as const

export const TRANSPORT_STATE_META: Record<TransportState, EnumMeta> = {
  // 未连接
  "disconnected": { value: 'disconnected', label: "未连接", severity: 'neutral' },
  // 正在建立连接
  "connecting": { value: 'connecting', label: "连接中", severity: 'progress', active: true },
  // 连上了，正在认证
  "authenticating": { value: 'authenticating', label: "认证中", severity: 'progress', active: true },
  // 可用
  "ready": { value: 'ready', label: "已连接", severity: 'ok' },
  // 有限退避等待重试
  "backoff": { value: 'backoff', label: "等待重连", severity: 'warn', active: true },
  // 认证失效或被拒，**不再自动重试**（避免无休止刷新，T66）
  "blocked": { value: 'blocked', label: "连接被拒绝（需重新配对）", severity: 'error' },
}

export function transportStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return TRANSPORT_STATE_META[value as TransportState]?.label ?? `未知取值（${value}）`
}

// 客户端缓存投影的数据状态（计划 §3.4）。与 `transport_state` 分开的理由
// 在那条里。`stale` 要能显示：断网时可以看已取得的本地内容，
// 但**不能显示假在线**（计划 §3.2）。
export type SnapshotState = 'loading' | 'current' | 'stale' | 'failed'

export const SNAPSHOT_STATE_VALUES: readonly SnapshotState[] = [
  'loading',
  'current',
  'stale',
  'failed',
] as const

export const SNAPSHOT_STATE_META: Record<SnapshotState, EnumMeta> = {
  // 首次取快照中
  "loading": { value: 'loading', label: "加载中", severity: 'progress', active: true },
  // 与服务端游标一致
  "current": { value: 'current', label: "最新", severity: 'ok' },
  // 连接中断或游标落后，显示的是旧数据。**不得显示成在线最新**
  "stale": { value: 'stale', label: "数据可能已过期", severity: 'warn' },
  // 取快照失败（游标失效时应重新取快照而不是永久等）
  "failed": { value: 'failed', label: "数据加载失败", severity: 'error' },
}

export function snapshotStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return SNAPSHOT_STATE_META[value as SnapshotState]?.label ?? `未知取值（${value}）`
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
export type FederationError = 'discovery_incomplete' | 'scope_expired' | 'capability_unknown' | 'capability_unsupported' | 'input_not_verified' | 'egress_denied' | 'plan_changed' | 'offer_expired' | 'admission_unknown' | 'idempotency_conflict' | 'partial_retrieval' | 'insufficient_evidence' | 'budget_exhausted' | 'source_revoked' | 'delivery_expired' | 'local_model_missing' | 'protocol_incompatible' | 'task_cancelled' | 'credential_invalid' | 'credential_expired' | 'credential_replayed' | 'credential_audience_mismatch' | 'credential_operation_denied' | 'credential_scope_denied' | 'node_unknown' | 'node_revoked' | 'node_identity_mismatch' | 'node_identity_unavailable' | 'node_identity_unconfigured' | 'credential_unavailable'

export const FEDERATION_ERROR_VALUES: readonly FederationError[] = [
  'discovery_incomplete',
  'scope_expired',
  'capability_unknown',
  'capability_unsupported',
  'input_not_verified',
  'egress_denied',
  'plan_changed',
  'offer_expired',
  'admission_unknown',
  'idempotency_conflict',
  'partial_retrieval',
  'insufficient_evidence',
  'budget_exhausted',
  'source_revoked',
  'delivery_expired',
  'local_model_missing',
  'protocol_incompatible',
  'task_cancelled',
  'credential_invalid',
  'credential_expired',
  'credential_replayed',
  'credential_audience_mismatch',
  'credential_operation_denied',
  'credential_scope_denied',
  'node_unknown',
  'node_revoked',
  'node_identity_mismatch',
  'node_identity_unavailable',
  'node_identity_unconfigured',
  'credential_unavailable',
] as const

export const FEDERATION_ERROR_META: Record<FederationError, EnumMeta> = {
  // 成员枚举没能封存，覆盖承诺随之降级
  "discovery_incomplete": { value: 'discovery_incomplete', label: "节点范围未能完整确定", severity: 'warn' },
  // ScopeManifest 过期，需重新枚举生成新 scope
  "scope_expired": { value: 'scope_expired', label: "检索范围已过期", severity: 'warn' },
  // 没有有效健康证据。**与 unsupported 严格分开** —— 未知要去预检，不是排除
  "capability_unknown": { value: 'capability_unknown', label: "对方能力未知（需预检）", severity: 'warn' },
  // 已核实不支持所需 operation
  "capability_unsupported": { value: 'capability_unsupported', label: "对方不支持该操作", severity: 'neutral' },
  // 输入摘要/格式还没校验通过就想进 admission
  "input_not_verified": { value: 'input_not_verified', label: "输入尚未校验通过", severity: 'error' },
  // 外发许可不覆盖这次发送（接收方、内容或有效期超界）。
  // `local_only` 命中时也是这个码 —— 它高于所有自动回退（§6.2）。
  "egress_denied": { value: 'egress_denied', label: "该数据不允许发往此接收方", severity: 'error' },
  // 计划修订变了，原批准不再适用
  "plan_changed": { value: 'plan_changed', label: "执行计划已变更，需重新批准", severity: 'warn' },
  // Offer 有效期已过（Offer 本来就不预留算力）
  "offer_expired": { value: 'offer_expired', label: "执行意向已过期", severity: 'warn' },
  // 受理状态不明。**不等于未执行**，要按幂等键对账（T82）
  "admission_unknown": { value: 'admission_unknown', label: "受理状态未知（正在对账）", severity: 'warn' },
  // 同一幂等键对应不同请求正文。**返回冲突，不许复用不相关结果**（T80）
  "idempotency_conflict": { value: 'idempotency_conflict', label: "幂等键冲突（请求内容不一致）", severity: 'error' },
  // 检索只完成了一部分，覆盖账本里有缺口
  "partial_retrieval": { value: 'partial_retrieval', label: "检索未覆盖全部范围", severity: 'warn' },
  // 本次范围与配置下没拿到足够证据
  "insufficient_evidence": { value: 'insufficient_evidence', label: "证据不足", severity: 'warn' },
  // 根预算用尽（含发现与 Probe 的消耗）
  "budget_exhausted": { value: 'budget_exhausted', label: "预算已用尽", severity: 'warn' },
  // 来源被撤销或转为私有，停止新授权并重判派生依赖
  "source_revoked": { value: 'source_revoked', label: "来源已撤销", severity: 'warn' },
  // 结果暂存 TTL 到期未领取
  "delivery_expired": { value: 'delivery_expired', label: "结果已过期未领取", severity: 'error' },
  // 本地缺所需模型。**必须明确报出来**，不得悄悄请求远端（I03 / T18）——
  // 这正是项目已有的 `no_instruct_model` 在本地模式下的对应物。
  "local_model_missing": { value: 'local_model_missing', label: "本地缺少所需模型", severity: 'error' },
  // 协议版本或必需字段不兼容，明确拒绝而不是忽略后乱执行
  "protocol_incompatible": { value: 'protocol_incompatible', label: "协议版本不兼容", severity: 'error' },
  // 对已取消任务调用 resume。**取消是显式终态，不得被"恢复"改写回
  // running** —— 重跑必须是一条新任务（新授权、新覆盖分母），而不是
  // 拿旧计划接着跑。返回 409，任务状态原样不动。
  "task_cancelled": { value: 'task_cancelled', label: "任务已取消，不能恢复", severity: 'error' },
  // 节点凭证缺失字段、不是规范序列化、base64 不严格、算法不是 Ed25519、
  // 有效期超过上限，或签名验不过（篡改）。一律 401，不区分是哪一步 ——
  // 分开报等于给伪造者一个逐步试错的口。
  "credential_invalid": { value: 'credential_invalid', label: "节点凭证无效", severity: 'error' },
  // 凭证已过期或签发时间在未来（超出时钟偏差容忍）
  "credential_expired": { value: 'credential_expired', label: "节点凭证已过期", severity: 'error' },
  // 同一个 jti 第二次出现。凭证是**单次使用**的：执行者在验签通过后
  // 持久记下 jti 直到过期，并发重放由唯一约束仲裁（401）。
  "credential_replayed": { value: 'credential_replayed', label: "节点凭证被重放", severity: 'error' },
  // 凭证的 audience 不是接收它的这个节点。转手给第三个节点（越权转委托）就是这个码
  "credential_audience_mismatch": { value: 'credential_audience_mismatch', label: "凭证不是发给本节点的", severity: 'error' },
  // 凭证授权的操作不是本端点的操作（403）
  "credential_operation_denied": { value: 'credential_operation_denied', label: "凭证不允许该操作", severity: 'error' },
  // 凭证的请求绑定（方法/路径/正文摘要）或范围约束（root_task_id / step_id /
  // scope_ref / task_spec_digest）不覆盖这次请求或它要读的那一行（403）；
  // 以别的协调者名义提交计划也是这个码。
  "credential_scope_denied": { value: 'credential_scope_denied', label: "凭证范围不覆盖该请求", severity: 'error' },
  // 签发节点不在本节点控制面的成员目录里，或尚未被管理员批准（401）
  "node_unknown": { value: 'node_unknown', label: "未知或未批准的节点", severity: 'error' },
  // 签发节点已被管理员撤销。撤销对新请求生效的延迟以公钥缓存上限为界（401）
  "node_revoked": { value: 'node_revoked', label: "节点已被撤销", severity: 'error' },
  // 语料服务配置的节点身份（BUNDLE_NODE_ID）与控制面持久密钥派生的身份不一致，
  // 或控制面报告的本节点身份变了。**Fail Closed（503）**：否则本地目标会被当成远端
  "node_identity_mismatch": { value: 'node_identity_mismatch', label: "本节点身份不一致（联邦已停用）", severity: 'error' },
  // 还没从控制面取到本节点持久身份（503），联邦端点与出站一律拒绝
  "node_identity_unavailable": { value: 'node_identity_unavailable', label: "本节点身份尚未确定", severity: 'error' },
  // 没有可用的本节点身份配置（503）：shared 开发档位或测试跟随模式下
  // BUNDLE_NODE_ID 为空或形状不对。先配好持久身份再谈联邦。
  "node_identity_unconfigured": { value: 'node_identity_unconfigured', label: "本节点身份未配置", severity: 'error' },
  // 本节点控制面在凭证链路上不可用：出站时签不出凭证（不可达、拒签、响应形状不对），
  // 或入站时查不到签发节点的信任记录（503）。**是本节点的问题，不是对端没有资料**
  // —— 协调者记 unreachable 并保留可重试。
  "credential_unavailable": { value: 'credential_unavailable', label: "本节点凭证服务不可用", severity: 'error' },
}

export function federationErrorLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return FEDERATION_ERROR_META[value as FederationError]?.label ?? `未知取值（${value}）`
}

// 一张节点凭证授权的**唯一**操作（DDP-NODE-CREDENTIAL）。每个节点对节点
// 端点恰好对应一个值；凭证只签一个操作，拿读执行状态的凭证去受理任务是
// `credential_operation_denied`。
export type NodeCredentialOperation = 'probe_create' | 'probe_read' | 'admission_create' | 'admission_lookup' | 'execution_read' | 'execution_cancel' | 'evidence_set_read' | 'resource_locate' | 'result_resolve' | 'catalog_read' | 'directory_members_read' | 'directory_collections_read' | 'directory_capabilities_read'

export const NODE_CREDENTIAL_OPERATION_VALUES: readonly NodeCredentialOperation[] = [
  'probe_create',
  'probe_read',
  'admission_create',
  'admission_lookup',
  'execution_read',
  'execution_cancel',
  'evidence_set_read',
  'resource_locate',
  'result_resolve',
  'catalog_read',
  'directory_members_read',
  'directory_collections_read',
  'directory_capabilities_read',
] as const

export const NODE_CREDENTIAL_OPERATION_META: Record<NodeCredentialOperation, EnumMeta> = {
  // POST /api/v1/federation/probes
  "probe_create": { value: 'probe_create', label: "发起探测", severity: 'neutral' },
  // GET /api/v1/federation/probes/{probe_id}
  "probe_read": { value: 'probe_read', label: "读取探测回执", severity: 'neutral' },
  // POST /api/v1/federation/admissions
  "admission_create": { value: 'admission_create', label: "提交接单", severity: 'neutral' },
  // POST /api/v1/federation/admissions/lookup
  "admission_lookup": { value: 'admission_lookup', label: "对账接单", severity: 'neutral' },
  // GET /api/v1/federation/tasks/{executor_task_id}
  "execution_read": { value: 'execution_read', label: "读取执行状态", severity: 'neutral' },
  // POST /api/v1/federation/tasks/{executor_task_id}/cancel
  "execution_cancel": { value: 'execution_cancel', label: "取消执行", severity: 'neutral' },
  // GET /api/v1/federation/evidence-sets/{set_ref}
  "evidence_set_read": { value: 'evidence_set_read', label: "读取证据集", severity: 'neutral' },
  // POST /api/v1/federation/resources/locate
  "resource_locate": { value: 'resource_locate', label: "定位资源版本", severity: 'neutral' },
  // POST /api/v1/federation/results/resolve
  "result_resolve": { value: 'result_resolve', label: "解析证据引用", severity: 'neutral' },
  // GET /api/v1/federation/published-collections
  "catalog_read": { value: 'catalog_read', label: "读取发布目录", severity: 'neutral' },
  // GET /api/v1/federation/members
  "directory_members_read": { value: 'directory_members_read', label: "读取目录成员", severity: 'neutral' },
  // GET /api/v1/federation/collections
  "directory_collections_read": { value: 'directory_collections_read', label: "读取目录集合", severity: 'neutral' },
  // GET /api/v1/federation/generation-descriptor
  "directory_capabilities_read": { value: 'directory_capabilities_read', label: "读取生成能力", severity: 'neutral' },
}

export function nodeCredentialOperationLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return NODE_CREDENTIAL_OPERATION_META[value as NodeCredentialOperation]?.label ?? `未知取值（${value}）`
}

// 语料服务节点对节点端点的认证方式。唯一取值 `node_credential`：控制面持有
// 节点私钥，按请求签发限定 audience/actor/操作/范围/有效期的单次凭证。
// 旧的共享口令形态已删除，不再有兼容取值。
export type PeerAuthMode = 'node_credential'

export const PEER_AUTH_MODE_VALUES: readonly PeerAuthMode[] = [
  'node_credential',
] as const

export const PEER_AUTH_MODE_META: Record<PeerAuthMode, EnumMeta> = {
  // 控制面持有节点私钥，按请求签发限定 audience/actor/操作/范围/有效期的单次凭证
  "node_credential": { value: 'node_credential', label: "节点签名凭证", severity: 'ok' },
}

export function peerAuthModeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return PEER_AUTH_MODE_META[value as PeerAuthMode]?.label ?? `未知取值（${value}）`
}

// 控制域管理员批准的直接节点成员状态，批准不授予资源权限或证明远端持有密钥。
export type NodeMembershipState = 'pending' | 'approved' | 'revoked'

export const NODE_MEMBERSHIP_STATE_VALUES: readonly NodeMembershipState[] = [
  'pending',
  'approved',
  'revoked',
] as const

export const NODE_MEMBERSHIP_STATE_META: Record<NodeMembershipState, EnumMeta> = {
  // 已登记但管理员尚未批准
  "pending": { value: 'pending', label: "待批准", severity: 'neutral' },
  // 管理员已批准配置，健康与接单另行判断
  "approved": { value: 'approved', label: "已批准", severity: 'ok' },
  // 已撤销，保留旧快照成员位置且禁止旧修订恢复
  "revoked": { value: 'revoked', label: "已撤销", severity: 'warn' },
}

export function nodeMembershipStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return NODE_MEMBERSHIP_STATE_META[value as NodeMembershipState]?.label ?? `未知取值（${value}）`
}

// 单目录快照中的下级枚举状态，不声明递归全局覆盖。
export type MemberExpansionState = 'not_requested' | 'unexpanded_subtree' | 'source_revoked'

export const MEMBER_EXPANSION_STATE_VALUES: readonly MemberExpansionState[] = [
  'not_requested',
  'unexpanded_subtree',
  'source_revoked',
] as const

export const MEMBER_EXPANSION_STATE_META: Record<MemberExpansionState, EnumMeta> = {
  // 成员支持枚举但尚未请求下级目录
  "not_requested": { value: 'not_requested', label: "尚未展开", severity: 'neutral' },
  // 下级不可枚举，不等于空目录
  "unexpanded_subtree": { value: 'unexpanded_subtree', label: "下级未展开", severity: 'warn' },
  // 原快照成员已撤销或当前调用者不可见
  "source_revoked": { value: 'source_revoked', label: "来源已撤销", severity: 'warn' },
}

export function memberExpansionStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return MEMBER_EXPANSION_STATE_META[value as MemberExpansionState]?.label ?? `未知取值（${value}）`
}

// 桌面端数据源的状态（DESKTOP-APPSHELL-PLAN §1.6）。
// 与 `transport_state` 分开：那条是 client-runtime 的连接状态，
// 这里是"当前数据源能不能按其能力读写" —— 本机工作区与已连接中心各自一态。
export type SourceState = 'ready' | 'connecting' | 'signed_out' | 'unavailable'

export const SOURCE_STATE_VALUES: readonly SourceState[] = [
  'ready',
  'connecting',
  'signed_out',
  'unavailable',
] as const

export const SOURCE_STATE_META: Record<SourceState, EnumMeta> = {
  // 数据源可用，可按其能力读写
  "ready": { value: 'ready', label: "可用", severity: 'ok' },
  // 正在连接（本机运行时启动中或中心登录握手中）
  "connecting": { value: 'connecting', label: "连接中", severity: 'progress', active: true },
  // 中心登录已过期，需重新连接该中心
  "signed_out": { value: 'signed_out', label: "需要重新登录", severity: 'warn' },
  // 数据源不可用（本机运行时起不来或中心不可达）
  "unavailable": { value: 'unavailable', label: "不可用", severity: 'error' },
}

export function sourceStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return SOURCE_STATE_META[value as SourceState]?.label ?? `未知取值（${value}）`
}

// 桌面端数据源错误的机器可读码（宿主 `/api` 代理与数据源具名方法返回）。
// 命名对齐项目既有约定（snake_case）。`approved_plan_required` 是中心只读的
// 写拒绝：不是权限不足，而是写必须走联邦任务、经原生对话框批准后派发。
export type SourceError = 'no_active_source' | 'approved_plan_required' | 'source_signed_out' | 'source_changed' | 'not_supported_locally'

export const SOURCE_ERROR_VALUES: readonly SourceError[] = [
  'no_active_source',
  'approved_plan_required',
  'source_signed_out',
  'source_changed',
  'not_supported_locally',
] as const

export const SOURCE_ERROR_META: Record<SourceError, EnumMeta> = {
  // 还没有任何当前数据源（首运或全部移除后），/api 直接 503
  "no_active_source": { value: 'no_active_source', label: "还没有选择数据源", severity: 'error' },
  // 中心源只放行 GET/HEAD；写操作须作为联邦任务发起并经批准派发
  "approved_plan_required": { value: 'approved_plan_required', label: "中心在桌面里只读；写操作请作为联邦任务发起并批准", severity: 'error' },
  // 中心 JWT 过期（上游 401），该源变为"需要重新登录"
  "source_signed_out": { value: 'source_signed_out', label: "登录已过期，请重新连接该中心", severity: 'error' },
  // 在途请求返回时当前源已切换，页面丢弃该响应
  "source_changed": { value: 'source_changed', label: "数据源已切换，此结果已丢弃", severity: 'error' },
  // 本机工作区没实现这项中心接口（发布、重解析等）
  "not_supported_locally": { value: 'not_supported_locally', label: "本机工作区不支持这项功能", severity: 'error' },
}

export function sourceErrorLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return SOURCE_ERROR_META[value as SourceError]?.label ?? `未知取值（${value}）`
}

// 桌面端渲染进程的本机错误码（旧工作台 `platform/desktop.ts` 的 `reasons` 表搬入）。
// 命名对齐项目既有约定（snake_case）。这些码来自宿主具名方法与本地账本，
// 不是中心契约：含义只在"本机工作区 + 已配对中心"这套桌面链路下成立。
// 宿主 HostError、文件安全检查 code 选项与 fail 调用的用户文案由架构守卫检查，
// 包含直接字面量与条件分支返回码；含 smoke 的码、development_* 与
// invalid_development_url 仅用于内部诊断，不提供用户标签。
export type DesktopError = 'unsafe_runtime_directory' | 'unsafe_credential_directory' | 'unsafe_client_directory' | 'client_configuration_invalid' | 'connection_limit' | 'credential_required' | 'credential_unavailable' | 'export_target_changed' | 'file_changed' | 'file_operation_failed' | 'file_too_large' | 'host_closing' | 'invalid_bundle' | 'invalid_credential' | 'invalid_endpoint' | 'invalid_identity' | 'invalid_pdf' | 'invalid_runtime_session' | 'invalid_workspace' | 'invalid_wsl_distro' | 'runtime_exited' | 'runtime_incompatible' | 'runtime_start_failed' | 'runtime_stopped' | 'runtime_stopping' | 'runtime_unavailable' | 'subscription_limit' | 'unknown_connection' | 'unknown_operation' | 'unknown_runtime_backend' | 'unknown_workspace' | 'unsafe_export_target' | 'unsafe_runtime_token' | 'untrusted_sender' | 'workspace_alias_conflict' | 'workspace_changed' | 'workspace_unavailable' | 'wsl_backend_unavailable' | 'wsl_missing' | 'wsl_pid_record_failed' | 'wsl_runtime_abi_mismatch' | 'wsl_runtime_archive_mismatch' | 'wsl_runtime_manifest_invalid' | 'wsl_runtime_not_installed' | 'wsl_runtime_provision_failed' | 'wsl_unavailable' | 'wsl1_unsupported' | 'wsl_distro_not_found' | 'local_runtime_unavailable' | 'connection_failed' | 'authentication_required' | 'identity_mismatch' | 'profile_mismatch' | 'protocol_incompatible' | 'cache_failure' | 'model_unavailable' | 'unsupported_operation' | 'approved_plan_required' | 'outcome_unknown' | 'receipt_required' | 'disposed' | 'draft_conflict' | 'revision_conflict' | 'input_too_large' | 'not_found' | 'source_unavailable' | 'source_digest_mismatch' | 'wiki_response_too_large' | 'wiki_source_unavailable' | 'wiki_generation_invalid' | 'unsupported_generation' | 'wiki_relation_unsupported' | 'out_of_memory' | 'cursor_expired' | 'approval_cancelled' | 'plan_changed' | 'approval_unavailable' | 'consent_required' | 'consent_revoked' | 'consent_expired' | 'budget_exceeded' | 'policy_denied' | 'input_changed' | 'local_only' | 'center_not_paired' | 'center_not_current' | 'center_identity_changed' | 'center_binding_required' | 'center_unavailable' | 'delivery_unverified' | 'dispatch_already_reserved' | 'connection_not_current' | 'unreachable' | 'transport_error' | 'delivery_expired' | 'delivery_not_found' | 'delivery_id_missing' | 'result_manifest_mismatch' | 'result_unavailable' | 'ack_not_confirmed' | 'transfer_unknown' | 'resume_unknown' | 'transfer_in_progress' | 'upload_expired' | 'upload_failed' | 'upload_incomplete' | 'storage_origin_not_approved' | 'delivery_too_large' | 'gpu_device_unsupported' | 'gpu_offload_unverified' | 'model_backend_incompatible' | 'model_process_busy' | 'egress_denied' | 'invalid_response' | 'invalid_arguments' | 'host_operation_failed'

export const DESKTOP_ERROR_VALUES: readonly DesktopError[] = [
  'unsafe_runtime_directory',
  'unsafe_credential_directory',
  'unsafe_client_directory',
  'client_configuration_invalid',
  'connection_limit',
  'credential_required',
  'credential_unavailable',
  'export_target_changed',
  'file_changed',
  'file_operation_failed',
  'file_too_large',
  'host_closing',
  'invalid_bundle',
  'invalid_credential',
  'invalid_endpoint',
  'invalid_identity',
  'invalid_pdf',
  'invalid_runtime_session',
  'invalid_workspace',
  'invalid_wsl_distro',
  'runtime_exited',
  'runtime_incompatible',
  'runtime_start_failed',
  'runtime_stopped',
  'runtime_stopping',
  'runtime_unavailable',
  'subscription_limit',
  'unknown_connection',
  'unknown_operation',
  'unknown_runtime_backend',
  'unknown_workspace',
  'unsafe_export_target',
  'unsafe_runtime_token',
  'untrusted_sender',
  'workspace_alias_conflict',
  'workspace_changed',
  'workspace_unavailable',
  'wsl_backend_unavailable',
  'wsl_missing',
  'wsl_pid_record_failed',
  'wsl_runtime_abi_mismatch',
  'wsl_runtime_archive_mismatch',
  'wsl_runtime_manifest_invalid',
  'wsl_runtime_not_installed',
  'wsl_runtime_provision_failed',
  'wsl_unavailable',
  'wsl1_unsupported',
  'wsl_distro_not_found',
  'local_runtime_unavailable',
  'connection_failed',
  'authentication_required',
  'identity_mismatch',
  'profile_mismatch',
  'protocol_incompatible',
  'cache_failure',
  'model_unavailable',
  'unsupported_operation',
  'approved_plan_required',
  'outcome_unknown',
  'receipt_required',
  'disposed',
  'draft_conflict',
  'revision_conflict',
  'input_too_large',
  'not_found',
  'source_unavailable',
  'source_digest_mismatch',
  'wiki_response_too_large',
  'wiki_source_unavailable',
  'wiki_generation_invalid',
  'unsupported_generation',
  'wiki_relation_unsupported',
  'out_of_memory',
  'cursor_expired',
  'approval_cancelled',
  'plan_changed',
  'approval_unavailable',
  'consent_required',
  'consent_revoked',
  'consent_expired',
  'budget_exceeded',
  'policy_denied',
  'input_changed',
  'local_only',
  'center_not_paired',
  'center_not_current',
  'center_identity_changed',
  'center_binding_required',
  'center_unavailable',
  'delivery_unverified',
  'dispatch_already_reserved',
  'connection_not_current',
  'unreachable',
  'transport_error',
  'delivery_expired',
  'delivery_not_found',
  'delivery_id_missing',
  'result_manifest_mismatch',
  'result_unavailable',
  'ack_not_confirmed',
  'transfer_unknown',
  'resume_unknown',
  'transfer_in_progress',
  'upload_expired',
  'upload_failed',
  'upload_incomplete',
  'storage_origin_not_approved',
  'delivery_too_large',
  'gpu_device_unsupported',
  'gpu_offload_unverified',
  'model_backend_incompatible',
  'model_process_busy',
  'egress_denied',
  'invalid_response',
  'invalid_arguments',
  'host_operation_failed',
] as const

export const DESKTOP_ERROR_META: Record<DesktopError, EnumMeta> = {
  // 本机运行时私有目录不是普通目录、是符号链接或 POSIX 权限与所有者不安全
  "unsafe_runtime_directory": { value: 'unsafe_runtime_directory', label: "本机运行时目录未通过安全检查，无法启动，请联系维护者检查目录类型、权限和所有者。", severity: 'error' },
  // 凭证私有目录不是普通目录、是符号链接或 POSIX 权限与所有者不安全
  "unsafe_credential_directory": { value: 'unsafe_credential_directory', label: "凭证目录未通过安全检查，无法保存或读取凭证，请联系维护者检查目录类型、权限和所有者。", severity: 'error' },
  // 客户端私有目录不是普通目录、是符号链接或 POSIX 权限与所有者不安全
  "unsafe_client_directory": { value: 'unsafe_client_directory', label: "桌面客户端目录未通过安全检查，无法启动客户端，请联系维护者检查目录类型、权限和所有者。", severity: 'error' },
  // 客户端连接配置文件不安全、无法读取或内容无效，宿主初始化客户端层失败
  "client_configuration_invalid": { value: 'client_configuration_invalid', label: "桌面客户端配置无法读取或不安全，无法启动客户端，请联系维护者检查配置文件。", severity: 'error' },
  // 注册新连接时已保存的连接数达到 64 个上限
  "connection_limit": { value: 'connection_limit', label: "已保存的数据源连接达到上限，请移除不再使用的数据源后再连接。", severity: 'warn' },
  // 宿主执行需要凭证的操作时未找到当前环境与身份的凭证
  "credential_required": { value: 'credential_required', label: "当前身份没有可用凭证，无法执行此操作，请重新认证。", severity: 'warn' },
  // 凭证读取、安全检查或解密失败，或操作期间凭证会话已失效
  "credential_unavailable": { value: 'credential_unavailable', label: "当前凭证无法安全读取或会话已失效，请重新认证后再操作。", severity: 'error' },
  // 导出写入前目标文件的类型、设备、inode、大小或修改时间与选择时不一致
  "export_target_changed": { value: 'export_target_changed', label: "导出目标文件已变化，已阻止覆盖，请重新选择保存位置。", severity: 'warn' },
  // 导入读取未完成或读取前后文件大小、修改时间、状态变更时间不一致
  "file_changed": { value: 'file_changed', label: "导入文件在读取期间发生变化，请停止修改文件后重新选择导入。", severity: 'warn' },
  // 文件请求返回未单独分类的失败状态或没有响应体
  "file_operation_failed": { value: 'file_operation_failed', label: "文件请求失败，无法取得有效响应，请检查数据源状态后再操作。", severity: 'error' },
  // 文件响应超过 32 MiB，或导入对象不是非空普通文件或超过 32 MiB
  "file_too_large": { value: 'file_too_large', label: "文件不符合读取限制，请选择非空普通文件并确保大小不超过 32 MiB。", severity: 'error' },
  // 宿主正在关闭或已关闭，不再受理连接、工作区或运行时操作
  "host_closing": { value: 'host_closing', label: "桌面宿主正在关闭，无法受理此操作，请重新打开应用后再操作。", severity: 'neutral' },
  // Bundle 类型、大小或验证结果无效，或运行时无法完成 Bundle 验证
  "invalid_bundle": { value: 'invalid_bundle', label: "Bundle 格式不符合要求或验证未完成，请检查文件及本机运行时状态。", severity: 'error' },
  // 设置凭证时密钥为空、过长、含控制字符或持久化参数不是布尔值
  "invalid_credential": { value: 'invalid_credential', label: "中心返回的登录凭证无法由桌面端保存，请联系中心管理员检查凭证格式。", severity: 'error' },
  // 中心地址或存储源地址无法解析，含禁止字段或不满足 HTTPS 与源地址格式要求
  "invalid_endpoint": { value: 'invalid_endpoint', label: "中心或存储地址不符合安全要求，请使用 HTTPS 并移除嵌入凭证、查询参数和片段，且对象存储源地址只能包含协议、主机及可选端口，不能包含路径。", severity: 'error' },
  // 凭证操作的环境或身份标识符格式无效
  "invalid_identity": { value: 'invalid_identity', label: "环境或身份标识不符合要求，无法处理凭证，请重新连接数据源后再操作。", severity: 'error' },
  // 导入或读取的原件缺少 PDF 文件头，或响应类型不是 application/pdf
  "invalid_pdf": { value: 'invalid_pdf', label: "文件或原件响应不是有效的 PDF，无法继续处理，请检查所选文件或数据源原件。", severity: 'error' },
  // WSL 运行时启动时未提供非空宿主会话目录
  "invalid_runtime_session": { value: 'invalid_runtime_session', label: "本机运行时缺少有效会话目录，无法启动，请联系维护者检查宿主配置。", severity: 'error' },
  // 工作区标识、原生目录类型或 WSL 工作区路径不符合宿主校验要求
  "invalid_workspace": { value: 'invalid_workspace', label: "工作区标识或目录不符合要求，无法打开，请重新选择有效的工作区。", severity: 'error' },
  // 选择的 WSL 分发名称不符合安全字符格式
  "invalid_wsl_distro": { value: 'invalid_wsl_distro', label: "WSL 分发名称不符合要求，无法使用本机工作区，请检查分发配置并重新启动 DeepDocParse。", severity: 'error' },
  // 已拥有的运行时子进程意外退出，或 WSL 子进程在就绪前关闭
  "runtime_exited": { value: 'runtime_exited', label: "本机运行时意外退出，请检查运行环境后重新打开工作区。", severity: 'error' },
  // 本机运行时握手响应不是有效 JSON、协议版本不匹配或缺少环境与工作区身份
  "runtime_incompatible": { value: 'runtime_incompatible', label: "本机运行时握手不符合桌面协议，无法建立连接，请检查运行时与应用版本。", severity: 'error' },
  // 本机运行时未在期限内就绪、进程启动失败或启动期间发生未分类错误
  "runtime_start_failed": { value: 'runtime_start_failed', label: "本机运行时启动失败，请检查运行环境后重新打开工作区。", severity: 'error' },
  // 本机运行时启动在会话准备或等待就绪期间被停止请求取消
  "runtime_stopped": { value: 'runtime_stopped', label: "本机运行时启动已被停止，如需继续使用请重新打开工作区。", severity: 'neutral' },
  // 运行时尚处于停止中，拒绝新的启动请求
  "runtime_stopping": { value: 'runtime_stopping', label: "本机运行时仍在停止，暂时无法启动，请等待停止完成后再打开工作区。", severity: 'warn' },
  // 本机运行时连接未就绪，或握手超时、返回非成功状态或响应过大
  "runtime_unavailable": { value: 'runtime_unavailable', label: "本机运行时连接尚不可用，请检查运行时状态后重新打开工作区。", severity: 'error' },
  // 新建连接状态订阅时已达到 128 个订阅上限
  "subscription_limit": { value: 'subscription_limit', label: "连接状态订阅达到上限，无法添加订阅，请重新打开应用后再操作。", severity: 'warn' },
  // 宿主连接表中不存在请求指定的连接
  "unknown_connection": { value: 'unknown_connection', label: "指定的数据源连接已不存在，请重新选择或连接数据源。", severity: 'error' },
  // 旧宿主桥或数据源桥收到未定义的方法名
  "unknown_operation": { value: 'unknown_operation', label: "桌面宿主不识别此操作，请检查应用与宿主版本是否一致。", severity: 'error' },
  // 运行时后端类型既不是 native 也不是 wsl
  "unknown_runtime_backend": { value: 'unknown_runtime_backend', label: "本机运行时后端配置无法识别，请联系维护者检查宿主配置。", severity: 'error' },
  // 宿主工作区注册表中不存在请求指定的工作区
  "unknown_workspace": { value: 'unknown_workspace', label: "指定的工作区已不存在，请重新选择工作区。", severity: 'error' },
  // 选择的既有导出目标不是普通文件或是符号链接
  "unsafe_export_target": { value: 'unsafe_export_target', label: "导出目标不是安全的普通文件，已阻止写入，请重新选择保存位置。", severity: 'error' },
  // 运行时连接文件的类型、大小、权限、所有者或 PID、地址、令牌内容不安全
  "unsafe_runtime_token": { value: 'unsafe_runtime_token', label: "本机运行时连接信息未通过安全检查，已拒绝连接，请联系维护者检查运行时文件和权限。", severity: 'error' },
  // IPC 请求的发送窗口、主框架或页面地址不是受信任的桌面界面
  "untrusted_sender": { value: 'untrusted_sender', label: "请求并非来自受信任的桌面界面，已拒绝操作，请从正式桌面窗口操作。", severity: 'error' },
  // 同一环境与身份连接已绑定不同目录或不同工作区后端类型
  "workspace_alias_conflict": { value: 'workspace_alias_conflict', label: "此身份已绑定另一工作区目录或后端，无法重复绑定，请核对并选择原工作区。", severity: 'error' },
  // 原生工作区目录类型或设备、inode、规范路径身份与选择时不一致
  "workspace_changed": { value: 'workspace_changed', label: "工作区目录已变化，已拒绝继续使用，请重新选择工作区并核对内容。", severity: 'error' },
  // 本机数据源没有有效工作区句柄，或宿主不能显示工作区选择入口
  "workspace_unavailable": { value: 'workspace_unavailable', label: "本机工作区当前不可用，请重新选择工作区或检查桌面宿主状态。", severity: 'error' },
  // WSL 后端模块加载、构建、配置校验或初始化失败
  "wsl_backend_unavailable": { value: 'wsl_backend_unavailable', label: "WSL 运行时后端无法初始化，请联系维护者检查应用安装及后端配置。", severity: 'error' },
  // WSL 检测或 Bundle 验证时无法启动 wsl.exe
  "wsl_missing": { value: 'wsl_missing', label: "未找到可用的 WSL，无法使用本机工作区，请安装并启用 WSL 2 后重新启动 DeepDocParse。", severity: 'error' },
  // WSL 运行时就绪后无法把 Linux PID 写入宿主会话文件
  "wsl_pid_record_failed": { value: 'wsl_pid_record_failed', label: "WSL 运行时进程记录保存失败，无法完成启动，请检查宿主存储空间和写入权限。", severity: 'error' },
  // WSL 解压后的 Python 运行失败或版本、缓存标签、SOABI、机器架构与清单不一致
  "wsl_runtime_abi_mismatch": { value: 'wsl_runtime_abi_mismatch', label: "WSL 运行时无法执行或与安装清单不兼容，请联系维护者检查运行时安装包。", severity: 'error' },
  // WSL 运行时归档不可读或大小、SHA-256 与安装清单不一致
  "wsl_runtime_archive_mismatch": { value: 'wsl_runtime_archive_mismatch', label: "WSL 运行时安装包未通过完整性检查，请联系维护者检查安装包。", severity: 'error' },
  // WSL 运行时清单无法读取、不是 JSON 或不满足版本、归档、Python ABI 格式要求
  "wsl_runtime_manifest_invalid": { value: 'wsl_runtime_manifest_invalid', label: "WSL 运行时安装清单无效或无法读取，请联系维护者检查应用安装。", severity: 'error' },
  // WSL Bundle 验证时运行时目录缺少 runtime-files.py
  "wsl_runtime_not_installed": { value: 'wsl_runtime_not_installed', label: "WSL 运行时尚未完整安装，无法验证 Bundle，请先打开本机工作区完成运行时准备。", severity: 'error' },
  // WSL 运行时归档解压部署或安装标记写入失败或超时
  "wsl_runtime_provision_failed": { value: 'wsl_runtime_provision_failed', label: "WSL 运行时部署失败，请检查 WSL 的存储空间和写入权限后重新打开工作区。", severity: 'error' },
  // WSL 分发检测失败，或宿主无法读取遗留会话目录与 PID 记录进行清理
  "wsl_unavailable": { value: 'wsl_unavailable', label: "WSL 检测或遗留进程清理不可用，请检查 WSL 状态后重新启动 DeepDocParse。", severity: 'error' },
  // 检测到选择的分发运行在 WSL 1，而不是受支持的 WSL 2
  "wsl1_unsupported": { value: 'wsl1_unsupported', label: "当前分发使用 WSL 1，无法运行本机工作区，请将分发转换为 WSL 2 后重新启动 DeepDocParse。", severity: 'error' },
  // WSL 分发列表中不存在配置指定的分发
  "wsl_distro_not_found": { value: 'wsl_distro_not_found', label: "未找到配置的 WSL 分发，请安装该分发或修正分发配置后重新启动 DeepDocParse。", severity: 'error' },
  // 原生运行时后端初始化出现未分类错误，宿主保留错误并拒绝本机连接
  "local_runtime_unavailable": { value: 'local_runtime_unavailable', label: "本机运行时后端无法初始化，请联系维护者检查应用安装和运行环境。", severity: 'error' },
  // 本机连接暂不可用，写操作未发出，草稿已保留
  "connection_failed": { value: 'connection_failed', label: "连接暂不可用，已保留草稿。", severity: 'error' },
  // 当前身份需要重新认证后才能操作
  "authentication_required": { value: 'authentication_required', label: "此身份需要重新认证。", severity: 'error' },
  // 环境身份与已配对记录不一致，拒绝操作
  "identity_mismatch": { value: 'identity_mismatch', label: "环境身份与已配对记录不一致。", severity: 'error' },
  // 登录身份与已配对记录不一致，拒绝操作
  "profile_mismatch": { value: 'profile_mismatch', label: "登录身份与已配对记录不一致。", severity: 'error' },
  // 当前环境没有提供所需的工作台协议
  "protocol_incompatible": { value: 'protocol_incompatible', label: "此环境未提供所需的工作台协议。", severity: 'error' },
  // 本地缓存/草稿写不进去（先持久再出网不断言失败）
  "cache_failure": { value: 'cache_failure', label: "本地缓存无法写入，请检查可用空间。", severity: 'error' },
  // 生成模型尚不可用，检索与原文不受影响
  "model_unavailable": { value: 'model_unavailable', label: "生成模型尚不可用，可以继续检索和查看原文。", severity: 'warn' },
  // 当前环境不支持这项操作，未发出请求
  "unsupported_operation": { value: 'unsupported_operation', label: "此环境暂不支持这项操作。", severity: 'error' },
  // 中心源只读，写操作须作为联邦任务发起并经批准派发
  "approved_plan_required": { value: 'approved_plan_required', label: "此操作需要先确认远端执行与外发许可。", severity: 'error' },
  // 请求发出但回执丢失，须按幂等键查询回执后再处理
  "outcome_unknown": { value: 'outcome_unknown', label: "提交结果未确认，请查询回执后再处理。", severity: 'warn' },
  // 有未对账的操作键，先查回执再做新的写操作
  "receipt_required": { value: 'receipt_required', label: "请查询已保存操作的回执。", severity: 'warn' },
  // 连接已切换或释放，旧连接上的操作不再受理
  "disposed": { value: 'disposed', label: "连接已切换，请在当前工作区重新操作。", severity: 'error' },
  // 草稿被另一窗口先写，CAS 修订对不上
  "draft_conflict": { value: 'draft_conflict', label: "草稿已被另一窗口更新，请重新打开后合并。", severity: 'warn' },
  // 草稿被另一窗口先写，CAS 修订对不上
  "revision_conflict": { value: 'revision_conflict', label: "草稿已被另一窗口更新，请重新打开后合并。", severity: 'warn' },
  // 文件超过当前操作的大小限制，未发送
  "input_too_large": { value: 'input_too_large', label: "文件超过当前操作的大小限制。", severity: 'error' },
  // 资料不存在或当前身份无权访问
  "not_found": { value: 'not_found', label: "该资料不存在或当前身份无权访问。", severity: 'error' },
  // 来源的访问许可已撤销或过期，不能继续读取快照
  "source_unavailable": { value: 'source_unavailable', label: "此来源的访问许可已撤销或过期，不能继续读取快照。", severity: 'error' },
  // 收到的原文与固定版本摘要不一致，已阻止显示
  "source_digest_mismatch": { value: 'source_digest_mismatch', label: "收到的原文与固定版本摘要不一致，已阻止显示。", severity: 'error' },
  // Wiki 修订超过当前读取大小限制
  "wiki_response_too_large": { value: 'wiki_response_too_large', label: "Wiki 修订超过当前读取大小限制。", severity: 'error' },
  // Wiki 的固定来源已不可用，需重新选择来源
  "wiki_source_unavailable": { value: 'wiki_source_unavailable', label: "Wiki 的固定来源已经不可用，请重新选择来源。", severity: 'error' },
  // 模型输出未通过 Wiki 格式或引用检查，本次没有发布修订
  "wiki_generation_invalid": { value: 'wiki_generation_invalid', label: "模型输出未通过 Wiki 格式或引用检查，此次没有发布修订。", severity: 'error' },
  // 生成内容缺少有效的原始出处，本次没有发布
  "unsupported_generation": { value: 'unsupported_generation', label: "生成内容缺少有效的原始出处，此次没有发布。", severity: 'error' },
  // 模型选择了不存在或缺少原文支撑的关系，本次没有发布修订
  "wiki_relation_unsupported": { value: 'wiki_relation_unsupported', label: "模型选择了不存在或缺少原文支撑的关系，此次没有发布 Wiki 修订。请缩小主题或调整模型后重试。", severity: 'error' },
  // 本机内存不足，模型已停止，可查看任务后重启
  "out_of_memory": { value: 'out_of_memory', label: "本机内存不足，模型已经停止；可以查看任务后重新启动。", severity: 'error' },
  // 目录已更新或快照已失效，需重新读取首页
  "cursor_expired": { value: 'cursor_expired', label: "目录已更新或快照已失效，请重新读取首页。", severity: 'warn' },
  // 用户在系统确认框里取消了批准，没有授予任何外发许可
  "approval_cancelled": { value: 'approval_cancelled', label: "已取消批准，没有授予任何外发许可。", severity: 'neutral' },
  // 计划修订变了，原批准不再适用
  "plan_changed": { value: 'plan_changed', label: "执行计划已变更，需重新批准。", severity: 'warn' },
  // 当前宿主无法显示系统确认框，不能批准外发
  "approval_unavailable": { value: 'approval_unavailable', label: "当前宿主无法显示系统确认框，不能批准外发。", severity: 'error' },
  // 该阶段尚未批准，未发送任何内容
  "consent_required": { value: 'consent_required', label: "该阶段尚未批准，未发送任何内容。", severity: 'warn' },
  // 批准已撤销，需准备并批准新计划
  "consent_revoked": { value: 'consent_revoked', label: "批准已撤销；需要准备并批准新计划。", severity: 'warn' },
  // 计划或批准已过期，需准备新计划
  "consent_expired": { value: 'consent_expired', label: "计划或批准已过期；需要准备新计划。", severity: 'warn' },
  // 超出已批准的请求或外发字节预算，未发送
  "budget_exceeded": { value: 'budget_exceeded', label: "超出已批准的请求或外发字节预算，未发送。", severity: 'warn' },
  // 接收方、地址或数据边超出已批准范围，未发送
  "policy_denied": { value: 'policy_denied', label: "接收方、地址或数据边超出已批准范围，未发送。", severity: 'error' },
  // 本地输入与锁定摘要不一致，未发送
  "input_changed": { value: 'input_changed', label: "本地输入与锁定摘要不一致，未发送。", severity: 'error' },
  // 工作区处于仅本地模式，禁止外发
  "local_only": { value: 'local_only', label: "工作区处于仅本地模式，禁止外发。", severity: 'error' },
  // 计划中的中心尚未配对
  "center_not_paired": { value: 'center_not_paired', label: "尚未配对计划中的中心。", severity: 'error' },
  // 中心连接未就绪，需重连并核对节点身份
  "center_not_current": { value: 'center_not_current', label: "中心连接未就绪；请在“数据源”页重新连接中心并核对节点身份后再操作。", severity: 'error' },
  // 中心地址或身份与已审阅计划不一致，已拒绝发送
  "center_identity_changed": { value: 'center_identity_changed', label: "中心地址或身份与已审阅计划不一致，已拒绝发送。", severity: 'error' },
  // 计划没有唯一的已审阅接收方，不能派发
  "center_binding_required": { value: 'center_binding_required', label: "计划没有唯一的已审阅接收方，不能派发。", severity: 'error' },
  // 当前连接无法取得中心凭证
  "center_unavailable": { value: 'center_unavailable', label: "当前连接无法取得中心凭证。", severity: 'error' },
  // 交付结果没有通过本地摘要重算，不能确认
  "delivery_unverified": { value: 'delivery_unverified', label: "交付结果没有通过本地摘要重算，不能确认。", severity: 'error' },
  // 这次发送已占用预算，先对账再重试
  "dispatch_already_reserved": { value: 'dispatch_already_reserved', label: "这次发送已经占用预算，请先对账再重试。", severity: 'warn' },
  // 本机工作区连接未就绪，草稿已保留
  "connection_not_current": { value: 'connection_not_current', label: "本机工作区连接未就绪，已保留草稿。", severity: 'warn' },
  // 中心暂时无法连接，未确认任何结果
  "unreachable": { value: 'unreachable', label: "中心暂时无法连接，未确认任何结果。", severity: 'error' },
  // 读取中心时连接中断（只读请求，写请求丢响应另记为结果未知），没有确认任何结果
  "transport_error": { value: 'transport_error', label: "读取中心时连接中断，没有确认任何结果；可以再次读取，不会重复任何写入。", severity: 'warn' },
  // 交付已过期，结果没有保存到本机
  "delivery_expired": { value: 'delivery_expired', label: "交付已过期，结果没有保存到本机。", severity: 'error' },
  // 中心暂时没有这份交付，可稍后再取
  "delivery_not_found": { value: 'delivery_not_found', label: "中心暂时没有这份交付，可以稍后再取。", severity: 'warn' },
  // 中心尚未给出交付编号
  "delivery_id_missing": { value: 'delivery_id_missing', label: "中心尚未给出交付编号。", severity: 'warn' },
  // 取回的结果与中心声明的摘要不一致，没有保存
  "result_manifest_mismatch": { value: 'result_manifest_mismatch', label: "取回的结果与中心声明的摘要不一致，没有保存。", severity: 'error' },
  // 中心没有返回可校验的结果
  "result_unavailable": { value: 'result_unavailable', label: "中心没有返回可校验的结果。", severity: 'error' },
  // 中心没有确认这次交付，可以再次确认
  "ack_not_confirmed": { value: 'ack_not_confirmed', label: "中心没有确认这次交付，可以再次确认。", severity: 'warn' },
  // 传输结果未确认，保留原创建编号，先查回执再对账，不自动重传
  "transfer_unknown": { value: 'transfer_unknown', label: "传输结果未确认。保留原创建编号，先查询回执，再显式对账或继续缺片；不会自动重传。", severity: 'warn' },
  // 继续请求的结果未确认，查回执后再处理，不自动批准或派发
  "resume_unknown": { value: 'resume_unknown', label: "继续请求的结果未确认，请查询回执后再处理；不会自动批准或派发。", severity: 'warn' },
  // 此计划已有主机传输在执行，可查看进度或停止传输
  "transfer_in_progress": { value: 'transfer_in_progress', label: "此计划已有主机传输在执行，可查看进度或停止传输。", severity: 'neutral' },
  // 临时上传已过期，需重新准备并批准计划
  "upload_expired": { value: 'upload_expired', label: "临时上传已过期，需要重新准备并批准计划。", severity: 'warn' },
  // 服务端拒绝了这份输入，未提交解析任务
  "upload_failed": { value: 'upload_failed', label: "服务端拒绝了这份输入，未提交解析任务。", severity: 'error' },
  // 中心返回的分片清单不完整，没有继续发送
  "upload_incomplete": { value: 'upload_incomplete', label: "中心返回的分片清单不完整，没有继续发送。", severity: 'error' },
  // 对象存储地址不在已审阅的传输范围内，原件未发送
  "storage_origin_not_approved": { value: 'storage_origin_not_approved', label: "对象存储地址不在已审阅的传输范围内，原件未发送。", severity: 'error' },
  // 交付超过本机 64 MiB 校验上限，尚未确认或清理
  "delivery_too_large": { value: 'delivery_too_large', label: "交付超过本机 64 MiB 校验上限，尚未确认或清理。", severity: 'error' },
  // 所选 Vulkan 设备是软件渲染器，未启动，也未自动退回 CPU
  "gpu_device_unsupported": { value: 'gpu_device_unsupported', label: "所选 Vulkan 设备是软件渲染器，未启动，也未自动退回 CPU。", severity: 'error' },
  // 没有观测到物理 GPU 上的模型层卸载，已停止该进程
  "gpu_offload_unverified": { value: 'gpu_offload_unverified', label: "没有观测到物理 GPU 上的模型层卸载，已停止该进程。需要 CPU 时请明确选择 CPU 运行包。", severity: 'error' },
  // 所选模型与运行包不兼容
  "model_backend_incompatible": { value: 'model_backend_incompatible', label: "所选模型与运行包不兼容。", severity: 'error' },
  // 受管模型正在运行，先停止再切换模型或后端
  "model_process_busy": { value: 'model_process_busy', label: "请先停止当前受管模型，再切换模型或后端。", severity: 'warn' },
  // 中心拒绝了这次外发许可
  "egress_denied": { value: 'egress_denied', label: "中心拒绝了这次外发许可。", severity: 'error' },
  // 中心返回的内容无法识别
  "invalid_response": { value: 'invalid_response', label: "中心返回的内容无法识别。", severity: 'error' },
  // 请求参数不合法，宿主拒绝执行
  "invalid_arguments": { value: 'invalid_arguments', label: "请求参数不正确，宿主拒绝执行。", severity: 'error' },
  // 宿主操作失败且结果未知，须按幂等键对账
  "host_operation_failed": { value: 'host_operation_failed', label: "宿主操作结果未知，请查询回执后再处理。", severity: 'warn' },
}

export function desktopErrorLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return DESKTOP_ERROR_META[value as DesktopError]?.label ?? `未知取值（${value}）`
}

// 本机运行时联邦派发状态机（`ddp_local/federation_dispatch.py` 的 `state`）。
// 与契约轴分开：它是"本机镜像看到的进展"，中心的权威状态以对账结果为准。
// 展示时按契约轴摆（规划/受理/交付），明细收进详情 —— 不许压成一个绿色完成。
export type LocalDispatchState = 'prepared' | 'exploring' | 'planned' | 'explore_unknown' | 'submitted' | 'submit_unknown' | 'approved' | 'succeeded' | 'failed' | 'cancelled' | 'delivered' | 'waiting_input' | 'uploading' | 'content_verifying' | 'content_verified' | 'running' | 'expired' | 'acked' | 'cancel_unknown' | 'resume_unknown'

export const LOCAL_DISPATCH_STATE_VALUES: readonly LocalDispatchState[] = [
  'prepared',
  'exploring',
  'planned',
  'explore_unknown',
  'submitted',
  'submit_unknown',
  'approved',
  'succeeded',
  'failed',
  'cancelled',
  'delivered',
  'waiting_input',
  'uploading',
  'content_verifying',
  'content_verified',
  'running',
  'expired',
  'acked',
  'cancel_unknown',
  'resume_unknown',
] as const

export const LOCAL_DISPATCH_STATE_META: Record<LocalDispatchState, EnumMeta> = {
  // 计划已建，还没派发
  "prepared": { value: 'prepared', label: "尚未派发", severity: 'neutral' },
  // 已派发探索，中心规划中
  "exploring": { value: 'exploring', label: "中心规划中", severity: 'progress', active: true },
  // 中心计划已就绪，待审阅批准
  "planned": { value: 'planned', label: "中心计划已就绪", severity: 'progress', active: true },
  // 探索发出但回执丢失，需对账
  "explore_unknown": { value: 'explore_unknown', label: "探索结果未知 · 需对账", severity: 'warn', active: true },
  // 已提交中心执行
  "submitted": { value: 'submitted', label: "中心执行中", severity: 'progress', active: true },
  // 提交发出但回执丢失，需对账
  "submit_unknown": { value: 'submit_unknown', label: "提交结果未知 · 需对账", severity: 'warn', active: true },
  // 中心已批准（中心侧状态）
  "approved": { value: 'approved', label: "中心已批准", severity: 'progress', active: true },
  // 中心已完成
  "succeeded": { value: 'succeeded', label: "中心已完成", severity: 'ok' },
  // 失败，失败原因持久化在镜像里
  "failed": { value: 'failed', label: "失败", severity: 'error' },
  // 已取消（显式终态）
  "cancelled": { value: 'cancelled', label: "已取消", severity: 'neutral' },
  // 已交付并经本地确认
  "delivered": { value: 'delivered', label: "已交付", severity: 'ok' },
  // 文件计划：等原始输入上传完，不占算力
  "waiting_input": { value: 'waiting_input', label: "等待原始输入", severity: 'progress', active: true },
  // 文件计划：原始输入上传中
  "uploading": { value: 'uploading', label: "上传中", severity: 'progress', active: true },
  // 文件计划：服务端全量校验中
  "content_verifying": { value: 'content_verifying', label: "完整摘要校验中", severity: 'progress', active: true },
  // 文件计划：输入已校验，待执行
  "content_verified": { value: 'content_verified', label: "输入已校验 · 等待执行", severity: 'progress', active: true },
  // 中心计算执行中
  "running": { value: 'running', label: "中心计算中", severity: 'progress', active: true },
  // 临时产物已过期
  "expired": { value: 'expired', label: "临时产物已过期", severity: 'warn' },
  // 本机已确认交付
  "acked": { value: 'acked', label: "本机已确认交付", severity: 'ok' },
  // 取消发出但回执丢失，需对账
  "cancel_unknown": { value: 'cancel_unknown', label: "取消结果未知 · 需对账", severity: 'warn' },
  // 继续请求发出但回执丢失，需对账
  "resume_unknown": { value: 'resume_unknown', label: "继续请求结果未知 · 需对账", severity: 'warn', active: true },
}

export function localDispatchStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return LOCAL_DISPATCH_STATE_META[value as LocalDispatchState]?.label ?? `未知取值（${value}）`
}

// 宿主对取回的交付字节重算摘要的结果（`bridge.d.ts` 的 `PlanDetail.verification`）。
// 与契约 `validation_state` 分开：那条是中心对输出引用的结构校验，
// 这里是本机对交付字节的摘要重算 —— 只有 `passed` 才允许确认交付。
export type LocalVerifyState = 'passed' | 'failed' | 'unavailable'

export const LOCAL_VERIFY_STATE_VALUES: readonly LocalVerifyState[] = [
  'passed',
  'failed',
  'unavailable',
] as const

export const LOCAL_VERIFY_STATE_META: Record<LocalVerifyState, EnumMeta> = {
  // 本地重算摘要与中心声明一致，可以确认
  "passed": { value: 'passed', label: "本地重算摘要一致", severity: 'ok' },
  // 本地重算摘要与中心声明不一致，禁止确认
  "failed": { value: 'failed', label: "本地重算摘要不一致", severity: 'error' },
  // 尚无取回的交付可校验
  "unavailable": { value: 'unavailable', label: "尚无可校验的本地结果", severity: 'neutral' },
}

export function localVerifyStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return LOCAL_VERIFY_STATE_META[value as LocalVerifyState]?.label ?? `未知取值（${value}）`
}

// 文件计划的对象存储传输相位（`bridge.d.ts` 的 `PlanDetail.transfer`）。
// 上传走的是宿主独占的传输循环，账本只记录相位与字节 ——
// 上传完成不等于解析完成，界面不许把两者混成一个"成功"。
export type LocalTransferState = 'prepared' | 'creating' | 'uploading' | 'verifying' | 'verified' | 'unknown'

export const LOCAL_TRANSFER_STATE_VALUES: readonly LocalTransferState[] = [
  'prepared',
  'creating',
  'uploading',
  'verifying',
  'verified',
  'unknown',
] as const

export const LOCAL_TRANSFER_STATE_META: Record<LocalTransferState, EnumMeta> = {
  // 传输尚未开始
  "prepared": { value: 'prepared', label: "待发送", severity: 'neutral' },
  // 正在准备临时上传
  "creating": { value: 'creating', label: "准备临时上传", severity: 'progress', active: true },
  // 正在上传缺片
  "uploading": { value: 'uploading', label: "正在上传缺片", severity: 'progress', active: true },
  // 已上传，等待确认服务端全量校验结果
  "verifying": { value: 'verifying', label: "已上传，等待确认服务端全量校验结果", severity: 'progress', active: true },
  // 输入已通过服务端校验
  "verified": { value: 'verified', label: "输入已通过服务端校验", severity: 'ok' },
  // 上次传输中断，需先对账再继续
  "unknown": { value: 'unknown', label: "上次传输中断，需对账", severity: 'warn' },
}

export function localTransferStateLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return LOCAL_TRANSFER_STATE_META[value as LocalTransferState]?.label ?? `未知取值（${value}）`
}

// 固定版本授权副本的可读状态，不代表来源节点在线。
export type BundleReplicaAvailability = 'licensed_copy' | 'unavailable'

export const BUNDLE_REPLICA_AVAILABILITY_VALUES: readonly BundleReplicaAvailability[] = [
  'licensed_copy',
  'unavailable',
] as const

export const BUNDLE_REPLICA_AVAILABILITY_META: Record<BundleReplicaAvailability, EnumMeta> = {
  // 未撤销且未过期的许可离线快照
  "licensed_copy": { value: 'licensed_copy', label: "离线快照（不是源节点在线，也不是重新授权）", severity: 'warn' },
  // 授权副本已撤销或过期，禁止读取原件
  "unavailable": { value: 'unavailable', label: "来源已撤销或过期（410），停止新授权，不显示在线。", severity: 'error' },
}

export function bundleReplicaAvailabilityLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return BUNDLE_REPLICA_AVAILABILITY_META[value as BundleReplicaAvailability]?.label ?? `未知取值（${value}）`
}

export type { GenerationOperation, GenerationCandidate, GenerationCandidates } from './generation-candidates'
