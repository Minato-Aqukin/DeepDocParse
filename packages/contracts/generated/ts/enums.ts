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
export type Degraded = 'no_hits' | 'parse_mismatch' | 'embedding_unavailable' | 'vision_unavailable' | 'crop_unsupported' | 'crop_failed' | 'client_aborted' | 'upstream_error' | 'upstream_interrupted' | 'index_changed_during_answer' | 'decision_unavailable' | 'no_evidence_in_turn' | 'inherited_evidence_incomplete' | 'gate_rejected_all' | 'citation_persist_failed' | 'verification_unavailable' | 'schema_violation' | 'rerank_unavailable' | 'no_instruct_model' | 'empty_query' | 'answer_unavailable'

export const DEGRADED_VALUES: readonly Degraded[] = [
  'no_hits',
  'parse_mismatch',
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
  'schema_violation',
  'rerank_unavailable',
  'no_instruct_model',
  'empty_query',
  'answer_unavailable',
] as const

export const DEGRADED_META: Record<Degraded, EnumMeta> = {
  // 检索一条都没命中
  no_hits: { value: 'no_hits', label: "未在本文档中检索到相关内容", severity: 'neutral' },
  // 裁图上的文字与解析出的块文本对不上（相似度低于
  // QA_PARSE_MISMATCH_THRESHOLD / EXTRACT_MISMATCH_THRESHOLD，实测标定 0.55）。
  // 它是**假出处**的主要探测手段，不是小问题。
  parse_mismatch: { value: 'parse_mismatch', label: "出处存疑（图上内容与解析文本对不上）", severity: 'warn' },
  // 向量化服务不可达，只走了关键词路。**这条是本项目吃过最大亏的地方**：
  // M4a 时向量检索静默退回 BM25，没人发现。必须可见。
  embedding_unavailable: { value: 'embedding_unavailable', label: "仅关键词检索（向量化服务不可用）", severity: 'warn' },
  // 视觉模型不可用，本轮没做视觉核对
  vision_unavailable: { value: 'vision_unavailable', label: "未做视觉验证（视觉模型不可用）", severity: 'warn' },
  // 该文件类型不支持按 bbox 裁图（例如非 PDF 原件）
  crop_unsupported: { value: 'crop_unsupported', label: "未做视觉验证（该文件不支持区域截图）", severity: 'neutral' },
  // 裁图渲染失败。**注意**：依赖缺失不走这条，见 ddp_core/crops.py 的 _DEP_NOTE
  crop_failed: { value: 'crop_failed', label: "未做视觉验证（区域截图失败）", severity: 'warn' },
  // 客户端在流式回答途中断开
  client_aborted: { value: 'client_aborted', label: "回答被中断", severity: 'neutral' },
  // 上游模型服务返回错误
  upstream_error: { value: 'upstream_error', label: "问答服务异常", severity: 'error' },
  // 上游在流式输出中途断流（拿到的是半截答案）
  upstream_interrupted: { value: 'upstream_interrupted', label: "回答生成中途断流", severity: 'error' },
  // 回答生成期间索引 generation 变了，本轮出处已标失效
  index_changed_during_answer: { value: 'index_changed_during_answer', label: "回答生成期间索引版本已变化，出处已标为失效", severity: 'warn' },
  // 「这轮要不要检索」的判定模型不可用，已保守地执行检索
  decision_unavailable: { value: 'decision_unavailable', label: "是否检索判定不可用，已保守执行检索", severity: 'neutral' },
  // 本轮既没检索到证据也没有可继承证据，拒绝脱离文档作答
  no_evidence_in_turn: { value: 'no_evidence_in_turn', label: "本轮没有可继承证据，已拒绝脱离文档作答", severity: 'warn' },
  // 上一轮的证据部分失效，不能直接沿用
  inherited_evidence_incomplete: { value: 'inherited_evidence_incomplete', label: "上一轮证据已部分失效，需重新检索后再回答", severity: 'warn' },
  // 候选全部没通过逐篇质量门控（有候选但都不够格，与 no_hits 不同）
  gate_rejected_all: { value: 'gate_rejected_all', label: "检索候选均未通过逐篇质量门控", severity: 'warn' },
  // 出处写库失败，相关结论已标为无证据支持
  citation_persist_failed: { value: 'citation_persist_failed', label: "出处保存失败，相关结论已标为无证据支持", severity: 'error' },
  // 原文自动核对没得出结论
  verification_unavailable: { value: 'verification_unavailable', label: "原文自动核对未得出结论，请人工复核", severity: 'warn' },
  // 模型输出反复不合 schema（已按 EXTRACT_MAX_RETRIES 重试仍失败）。
  // **绝不能被静默当成 not_found** —— 那会把系统故障伪装成"文档里没有"。
  schema_violation: { value: 'schema_violation', label: "模型输出不符合 schema（已重试仍失败）", severity: 'error' },
  // 配了精排但上游没注册 rerank 模型，本轮没重排
  rerank_unavailable: { value: 'rerank_unavailable', label: "未做精排（重排序服务不可用）", severity: 'neutral' },
  // 注册表里只有 OCR 专用模型（`capabilities` 含 `no_instruct`），
  // 抽值无处可调。同样绝不能伪装成 not_found。
  no_instruct_model: { value: 'no_instruct_model', label: "未抽取（后端没有可用的指令模型）", severity: 'error' },
  // MCP `search` 收到空查询串，直接返回空结果
  empty_query: { value: 'empty_query', label: "查询词为空", severity: 'neutral' },
  // MCP `ask` 调上游生成时非 200，本轮没有答案（证据仍然返回）
  answer_unavailable: { value: 'answer_unavailable', label: "生成服务不可用（证据已返回，结论未生成）", severity: 'error' },
}

export function degradedLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return DEGRADED_META[value as Degraded]?.label ?? `未知取值（${value}）`
}

// 版面编译（DDP-Compile v1）的降级。与 `degraded` 分开是因为它是
// **列表**：一次编译可以同时有好几种降级，而且它落在
// `documents.compile_degraded`（JSON 数组）上。
export type CompileDegraded = 'code_detection_unavailable' | 'crop_unsupported' | 'crop_failed' | 'vision_unavailable' | 'vision_invalid_output' | 'provider_unresolved' | 'reindex_validation_required' | 'compile_failed'

export const COMPILE_DEGRADED_VALUES: readonly CompileDegraded[] = [
  'code_detection_unavailable',
  'crop_unsupported',
  'crop_failed',
  'vision_unavailable',
  'vision_invalid_output',
  'provider_unresolved',
  'reindex_validation_required',
  'compile_failed',
] as const

export const COMPILE_DEGRADED_META: Record<CompileDegraded, EnumMeta> = {
  // 当前版面引擎报不出代码块
  code_detection_unavailable: { value: 'code_detection_unavailable', label: "当前版面引擎不能识别代码块", severity: 'neutral' },
  // 部分视觉原子没有可定位的裁图
  crop_unsupported: { value: 'crop_unsupported', label: "部分视觉原子没有可定位裁图", severity: 'neutral' },
  // 部分视觉原子裁图失败
  crop_failed: { value: 'crop_failed', label: "部分视觉原子裁图失败", severity: 'warn' },
  // 视觉理解模型不可用
  vision_unavailable: { value: 'vision_unavailable', label: "视觉理解模型不可用", severity: 'warn' },
  // 视觉模型返回的结构不合规
  vision_invalid_output: { value: 'vision_invalid_output', label: "视觉理解模型返回的结构不合规", severity: 'warn' },
  // 上游实际模型没解析出来，本次编译版本不可比较
  provider_unresolved: { value: 'provider_unresolved', label: "上游实际模型未解析，当前编译版本不可比较", severity: 'warn' },
  // 存在历史出处，需先校验并人工确认后才能重建
  reindex_validation_required: { value: 'reindex_validation_required', label: "存在历史出处，需先校验并确认后重建", severity: 'warn' },
  // 版面编译整体失败
  compile_failed: { value: 'compile_failed', label: "版面编译失败", severity: 'error' },
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
  pending: { value: 'pending', label: "排队中", severity: 'neutral', active: true },
  // 引擎正在解析
  running: { value: 'running', label: "解析中", severity: 'progress', active: true },
  // 引擎已完成，产品层正在归档结果
  archiving: { value: 'archiving', label: "归档中", severity: 'progress', active: true },
  // 解析完成且结果已可取
  succeeded: { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 解析失败，error 里有原因
  failed: { value: 'failed', label: "失败", severity: 'error' },
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
  none: { value: 'none', label: "未索引", severity: 'neutral' },
  // 已排队等待索引
  pending: { value: 'pending', label: "待索引", severity: 'neutral', active: true },
  // 正在建索引
  indexing: { value: 'indexing', label: "索引中", severity: 'progress', active: true },
  // 索引可用，可以问答
  ready: { value: 'ready', label: "可问答", severity: 'ok' },
  // 索引失败，index_error 里有原因
  failed: { value: 'failed', label: "索引失败", severity: 'error' },
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
  none: { value: 'none', label: "未编译", severity: 'neutral' },
  // 已排队等待编译
  pending: { value: 'pending', label: "待编译", severity: 'neutral', active: true },
  // 正在编译
  compiling: { value: 'compiling', label: "编译中", severity: 'progress', active: true },
  // 编译完整、无降级
  ready: { value: 'ready', label: "编译完整", severity: 'ok' },
  // 编译完成但有降级，见 compile_degraded
  partial: { value: 'partial', label: "编译有降级", severity: 'warn' },
  // 编译失败
  failed: { value: 'failed', label: "编译失败", severity: 'error' },
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
  pending: { value: 'pending', label: "排队中", severity: 'neutral', active: true },
  // 正在抽取
  running: { value: 'running', label: "抽取中", severity: 'progress', active: true },
  // 全部文档全部字段都完成
  succeeded: { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 部分文档或部分字段失败
  partial: { value: 'partial', label: "部分完成", severity: 'warn' },
  // 整批失败
  failed: { value: 'failed', label: "失败", severity: 'error' },
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
  found: { value: 'found', label: "已抽取", severity: 'ok' },
  // 文档里确实没有这个字段
  not_found: { value: 'not_found', label: "文档中未提及", severity: 'neutral' },
  // 抽取过程本身出错
  error: { value: 'error', label: "抽取失败", severity: 'error' },
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
  native: { value: 'native', label: "代码识别：原生", severity: 'ok' },
  // 靠启发式规则判出来的
  heuristic: { value: 'heuristic', label: "代码识别：启发式", severity: 'neutral' },
  // 当前引擎识别不了代码块
  unavailable: { value: 'unavailable', label: "代码识别：不可用", severity: 'warn' },
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
  source: { value: 'source', label: "原文", severity: 'neutral' },
  // 模型生成的理解（derived_from 指向原子）
  generated: { value: 'generated', label: "生成理解", severity: 'warn' },
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
  text: { value: 'text', label: "正文", severity: 'neutral' },
  // 各级标题
  title: { value: 'title', label: "标题", severity: 'neutral' },
  // 代码块
  code: { value: 'code', label: "代码", severity: 'neutral' },
  // 表格（table_html 可能有值）
  table: { value: 'table', label: "表格", severity: 'neutral' },
  // 图。**无 caption 也要产出原子**，否则视觉链路没输入
  figure: { value: 'figure', label: "图", severity: 'neutral' },
  // 行间公式
  equation: { value: 'equation', label: "公式", severity: 'neutral' },
  // 列表
  list: { value: 'list', label: "列表", severity: 'neutral' },
  // 有 type 但不在映射表里 —— 与「压根没有 type」要分开，后者归 text
  other: { value: 'other', label: "其它", severity: 'neutral' },
}

export function blockTypeLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return BLOCK_TYPE_META[value as BlockType]?.label ?? `未知取值（${value}）`
}

// 计量流水的种类。`extract` 按**字段数**计 requests：一次抽取 = N 次检索
// + N 次模型调用，按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜。
export type UsageKind = 'parse' | 'chat' | 'embeddings' | 'mcp' | 'qa' | 'embed' | 'compile_vision' | 'extract'

export const USAGE_KIND_VALUES: readonly UsageKind[] = [
  'parse',
  'chat',
  'embeddings',
  'mcp',
  'qa',
  'embed',
  'compile_vision',
  'extract',
] as const

export const USAGE_KIND_META: Record<UsageKind, EnumMeta> = {
  // 文档解析，按页计
  parse: { value: 'parse', label: "解析", severity: 'neutral' },
  // 对外 chat 代理，按次计
  chat: { value: 'chat', label: "对话", severity: 'neutral' },
  // 对外向量化代理
  embeddings: { value: 'embeddings', label: "向量化", severity: 'neutral' },
  // MCP 工具调用
  mcp: { value: 'mcp', label: "MCP 调用", severity: 'neutral' },
  // 站内问答
  qa: { value: 'qa', label: "问答", severity: 'neutral' },
  // 索引时的向量化
  embed: { value: 'embed', label: "索引向量化", severity: 'neutral' },
  // 编译期的视觉理解调用
  compile_vision: { value: 'compile_vision', label: "视觉理解", severity: 'neutral' },
  // 结构化抽取，按字段数计
  extract: { value: 'extract', label: "结构化抽取", severity: 'neutral' },
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
  user: { value: 'user', label: "用户", severity: 'neutral' },
  // sk- 开头的对外 key
  api_key: { value: 'api_key', label: "API Key", severity: 'neutral' },
  // 服务间调用（服务凭据）
  service: { value: 'service', label: "服务", severity: 'neutral' },
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
  viewer: { value: 'viewer', label: "只读成员", severity: 'neutral' },
  // viewer + 上传、重解析、发起抽取
  contributor: { value: 'contributor', label: "贡献者", severity: 'neutral' },
  // contributor + 复核队列、确认/驳回知识条目
  reviewer: { value: 'reviewer', label: "复核员", severity: 'neutral' },
  // 全部 + 成员管理、API key、配额、删除
  admin: { value: 'admin', label: "管理员", severity: 'neutral' },
}

export function roleLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return ROLE_META[value as Role]?.label ?? `未知取值（${value}）`
}

// 持久任务的状态机（§10）。**领取必须带 generation fencing**：
// lease 只解决"谁可以接管"，最终写入还要比 generation —— 否则被判死的
// 旧 worker 迟到写入会覆盖新结果。
export type TaskStatus = 'queued' | 'claimed' | 'running' | 'succeeded' | 'failed'

export const TASK_STATUS_VALUES: readonly TaskStatus[] = [
  'queued',
  'claimed',
  'running',
  'succeeded',
  'failed',
] as const

export const TASK_STATUS_META: Record<TaskStatus, EnumMeta> = {
  // 已落库等待领取
  queued: { value: 'queued', label: "排队中", severity: 'neutral', active: true },
  // 已被某个 worker 领取（带 lease_until）
  claimed: { value: 'claimed', label: "已领取", severity: 'progress', active: true },
  // 正在执行，靠 heartbeat 续租
  running: { value: 'running', label: "执行中", severity: 'progress', active: true },
  // 完成
  succeeded: { value: 'succeeded', label: "已完成", severity: 'ok' },
  // 失败，失败原因必须持久化并在 UI 可见
  failed: { value: 'failed', label: "失败", severity: 'error' },
}

export function taskStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return TASK_STATUS_META[value as TaskStatus]?.label ?? `未知取值（${value}）`
}

// 持久任务的种类。每种**分别设并发与队列**，不共用一个无量纲总并发。
export type TaskKind = 'parse_poll' | 'compile' | 'index' | 'extract' | 'knowledge' | 'gc'

export const TASK_KIND_VALUES: readonly TaskKind[] = [
  'parse_poll',
  'compile',
  'index',
  'extract',
  'knowledge',
  'gc',
] as const

export const TASK_KIND_META: Record<TaskKind, EnumMeta> = {
  // 轮询解析引擎并归档结果
  parse_poll: { value: 'parse_poll', label: "解析归档", severity: 'neutral' },
  // 版面编译（含视觉理解）
  compile: { value: 'compile', label: "版面编译", severity: 'neutral' },
  // 分块 + 向量化 + 写索引
  index: { value: 'index', label: "建立索引", severity: 'neutral' },
  // 结构化抽取批次
  extract: { value: 'extract', label: "结构化抽取", severity: 'neutral' },
  // 图谱 / wiki 生成
  knowledge: { value: 'knowledge', label: "知识生成", severity: 'neutral' },
  // 对象回收（带宽限期）
  gc: { value: 'gc', label: "对象回收", severity: 'neutral' },
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
  created: { value: 'created', label: "待上传", severity: 'neutral', active: true },
  // 客户端正在分片上传
  uploading: { value: 'uploading', label: "上传中", severity: 'progress', active: true },
  // 已 finalize，服务端正在校验摘要
  verifying: { value: 'verifying', label: "校验中", severity: 'progress', active: true },
  // 校验通过，已发出 DocumentSubmitted
  ready: { value: 'ready', label: "已就绪", severity: 'ok' },
  // 校验失败或客户端放弃
  failed: { value: 'failed', label: "失败", severity: 'error' },
  // 预签名过期未完成
  expired: { value: 'expired', label: "已过期", severity: 'warn' },
}

export function uploadStatusLabelOf(value: string | null | undefined): string | null {
  if (!value) return null
  return UPLOAD_STATUS_META[value as UploadStatus]?.label ?? `未知取值（${value}）`
}
