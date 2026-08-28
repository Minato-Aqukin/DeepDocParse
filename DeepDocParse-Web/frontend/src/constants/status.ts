/**
 * 状态与降级的文案表 —— 全站唯一来源。
 *
 * 后端新增一种状态或降级值时**只改这个文件**：以前要在 DocumentsView 补标签、
 * 在 AskPanel 补文案、在工作台再补一次，漏一处用户就看到一个原始英文枚举。
 *
 * 这里同时是将来上 i18n 的收口：换成 t('status.parse.running') 只动本文件。
 */
import type { FieldStatus, IndexStatus, ParseStatus, RunStatus } from '@/types/api'

export type TagType = 'success' | 'info' | 'warning' | 'danger' | 'primary'

export interface StatusMeta {
  label: string
  type: TagType
  /** 是否属于"还在动"的状态——列表与详情页据此决定要不要继续轮询 */
  active?: boolean
}

export const PARSE_STATUS: Record<ParseStatus, StatusMeta> = {
  pending: { label: '排队中', type: 'info', active: true },
  running: { label: '解析中', type: 'warning', active: true },
  archiving: { label: '归档中', type: 'warning', active: true },
  succeeded: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

export const INDEX_STATUS: Record<IndexStatus, StatusMeta> = {
  none: { label: '未索引', type: 'info' },
  pending: { label: '待索引', type: 'info', active: true },
  indexing: { label: '索引中', type: 'warning', active: true },
  ready: { label: '可问答', type: 'success' },
  failed: { label: '索引失败', type: 'danger' },
}

/**
 * 问答/检索的降级说明。
 *
 * 降级必须让用户看见——这个项目吃过静默降级的大亏（向量检索悄悄退回 BM25 无人发现），
 * 所以后端每加一个 degraded 值，这里就要有一句人话。
 */
export const DEGRADED_LABEL: Record<string, string> = {
  no_hits: '未在本文档中检索到相关内容',
  parse_mismatch: '出处存疑（图上内容与解析文本对不上）',
  embedding_unavailable: '仅关键词检索（向量化服务不可用）',
  vision_unavailable: '未做视觉验证（视觉模型不可用）',
  crop_unsupported: '未做视觉验证（该文件不支持区域截图）',
  crop_failed: '未做视觉验证（区域截图失败）',
  client_aborted: '回答被中断',
  upstream_error: '问答服务异常',
  upstream_interrupted: '回答生成中途断流',
  index_changed_during_answer: '回答生成期间索引版本已变化，出处已标为失效',
  // v1.1 新增两种
  schema_violation: '模型输出不符合 schema（已重试仍失败）',
  rerank_unavailable: '未做精排（重排序服务不可用）',
  no_instruct_model: '未抽取（后端没有可用的指令模型）',
}

/**
 * 检索可信度的文案。
 *
 * 设计取向（借自 kotaemon）：**把"我有多确信"交给用户判断，而不是替用户决定**。
 * 所以低相关时不拦着不给答案，只是把话说明白。
 *
 * 判定线在后端（`backend/app/config.py::qa_low_similarity`，那里写着实测分布），
 * 前端只负责显示 —— 校准值放两处一定会漂。
 */
export const CONFIDENCE_META: Record<string, StatusMeta & { hint: string }> = {
  high: { label: '相关度高', type: 'success', hint: '' },
  low: {
    label: '相关度偏低',
    type: 'warning',
    hint: '检索到的出处只是勉强过线，回答可能不准确 —— 请点开出处自行核对。',
  },
  unknown: {
    label: '相关度未知',
    type: 'info',
    hint: '本次只走了关键词检索（向量化服务不可用），无法判断语义相关度。',
  },
}

export function confidenceOf(level: string | undefined) {
  return level ? CONFIDENCE_META[level] : undefined
}

/**
 * 相似度"偏低"的兜底阈值。
 *
 * 校准值的唯一来源是后端 `qa_low_similarity`，接口带 `confidence.warn_below`
 * 时一律以后端为准。只有拿不到时才用这个 —— 目前 `/search` 的响应里没有这个
 * 字段（见 types/api.ts::SearchResult），所以跨文档检索页只能退到兜底值。
 * 两处各写一个 0.6 字面量迟早会漂，所以收在这里。
 */
export const DEFAULT_WARN_BELOW = 0.6

/** 相似度转成给人看的百分比。null = 没量到，不要显示成 0%（那是"完全不相关"的意思）。 */
export function similarityText(value: number | null | undefined): string | null {
  return value === null || value === undefined ? null : `${Math.round(value * 100)}%`
}

export function parseStatusOf(status: ParseStatus): StatusMeta {
  return PARSE_STATUS[status] ?? { label: status, type: 'info' }
}

export function indexStatusOf(status: IndexStatus): StatusMeta {
  return INDEX_STATUS[status] ?? { label: status, type: 'info' }
}

/** 未知的降级值也要给出可读文案，不能把枚举原样丢给用户。 */
export function degradedLabelOf(value: string | null): string | null {
  if (!value) return null
  return DEGRADED_LABEL[value] ?? `已降级（${value}）`
}

/** 筛选下拉的选项，直接由文案表派生，加状态不用改筛选器。 */
export const PARSE_STATUS_OPTIONS = (Object.keys(PARSE_STATUS) as ParseStatus[]).map((value) => ({
  value,
  label: PARSE_STATUS[value].label,
}))

export const INDEX_STATUS_OPTIONS = (Object.keys(INDEX_STATUS) as IndexStatus[]).map((value) => ({
  value,
  label: INDEX_STATUS[value].label,
}))


/* ---------- 结构化抽取 ---------- */

/**
 * 抽取任务的状态。
 *
 * `partial` 不是"有点问题"的委婉说法：它明确表示**必填字段没抽全，或个别文档失败**。
 * 与 succeeded 分开是刻意的 —— 一批 200 份文档里有 3 份失败，
 * 报成"成功"会让人直接拿去用。
 */
export const RUN_STATUS: Record<RunStatus, StatusMeta> = {
  pending: { label: '排队中', type: 'info', active: true },
  running: { label: '抽取中', type: 'warning', active: true },
  succeeded: { label: '已完成', type: 'success' },
  partial: { label: '部分完成', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
}

export function runStatusOf(status: RunStatus): StatusMeta {
  return RUN_STATUS[status] ?? { label: status, type: 'info' }
}

/**
 * 字段三态的文案。
 *
 * **"文档中未提及"绝不能写成"—"或空白**：空白让人以为是界面没渲染出来，
 * 而这里表达的是一个确定的结论（我们看过了，文档里没有）。
 * 反过来 error 也不能显示成"未提及"—— 那是把系统故障伪装成事实。
 */
export const FIELD_STATUS: Record<FieldStatus, StatusMeta> = {
  found: { label: '已抽取', type: 'success' },
  not_found: { label: '文档中未提及', type: 'info' },
  error: { label: '抽取失败', type: 'danger' },
}

export function fieldStatusOf(status: FieldStatus): StatusMeta {
  return FIELD_STATUS[status] ?? { label: status, type: 'info' }
}

/** schema 叶子类型的中文名，编辑器下拉用。 */
export const LEAF_TYPE_OPTIONS = [
  { value: 'string', label: '文本' },
  { value: 'number', label: '数字' },
  { value: 'integer', label: '整数' },
  { value: 'boolean', label: '是/否' },
] as const

export const FORMAT_OPTIONS = [
  { value: '', label: '不限' },
  { value: 'date', label: '日期 (YYYY-MM-DD)' },
  { value: 'date-time', label: '日期时间 (ISO 8601)' },
  { value: 'email', label: '邮箱' },
  { value: 'uri', label: 'URL' },
] as const
