/**
 * 状态与降级的文案表 —— **全部由契约生成，这里只做 UI 映射。**
 *
 * 合仓前这个文件是手写的：后端每加一种降级，就要有人记得回来补一句中文，
 * 漏一处用户就看到一个原始英文枚举 —— 或者更糟，那条降级在 UI 上等于不存在，
 * 而"降级必须可见"是第二条不变式。
 *
 * 现在取值与文案都住在 `packages/contracts/enums.yaml`，
 * Go / TypeScript / Python 三侧同源生成。本文件剩下的职责只有两件：
 *   1. 把语义色 `severity` 映射成 Element Plus 的 tag type（换 UI 框架只改这一处）
 *   2. 提供筛选下拉之类的派生数据
 *
 * 将来上 i18n 的收口也在这里：`label` 换成 `t('status.parse.running')` 只动本文件。
 */
import {
  COMPILE_STATUS_META,
  DEGRADED_META,
  FIELD_STATUS_META,
  INDEX_STATUS_META,
  PARSE_STATUS_META,
  RUN_STATUS_META,
  type CompileStatus,
  type Degraded,
  type EnumMeta,
  type FieldStatus,
  type IndexStatus,
  type ParseStatus,
  type RunStatus,
  type Severity,
  degradedLabelOf as contractDegradedLabelOf,
} from '@deepdocparse/contracts'

export type TagType = 'success' | 'info' | 'warning' | 'danger' | 'primary'

export interface StatusMeta {
  label: string
  type: TagType
  /** 是否属于"还在动"的状态——列表与详情页据此决定要不要继续轮询 */
  active?: boolean
}

/**
 * 语义色 -> Element Plus 的 tag type。**全站唯一一处**。
 *
 * 契约里存的是语义（`neutral` / `progress` / `ok` / `warn` / `error`），
 * 不是颜色名 —— 否则换 UI 框架就得回去改契约，而契约是三种语言共用的。
 */
const TAG_OF_SEVERITY: Record<Severity, TagType> = {
  neutral: 'info',
  progress: 'warning',
  ok: 'success',
  warn: 'warning',
  error: 'danger',
}

function toStatusMeta(meta: EnumMeta): StatusMeta {
  return { label: meta.label, type: TAG_OF_SEVERITY[meta.severity], active: meta.active }
}

function mapMeta<K extends string>(source: Record<K, EnumMeta>): Record<K, StatusMeta> {
  return Object.fromEntries(
    Object.entries(source).map(([k, v]) => [k, toStatusMeta(v as EnumMeta)]),
  ) as Record<K, StatusMeta>
}

export const PARSE_STATUS = mapMeta<ParseStatus>(PARSE_STATUS_META)
export const INDEX_STATUS = mapMeta<IndexStatus>(INDEX_STATUS_META)
export const RUN_STATUS = mapMeta<RunStatus>(RUN_STATUS_META)
export const FIELD_STATUS = mapMeta<FieldStatus>(FIELD_STATUS_META)
export const COMPILE_STATUS = mapMeta<CompileStatus>(COMPILE_STATUS_META)

/** 问答/检索的降级说明。取值与文案都来自契约，这里不许再手写一份。 */
export const DEGRADED_LABEL: Record<string, string> = Object.fromEntries(
  Object.entries(DEGRADED_META).map(([k, v]) => [k, (v as EnumMeta).label]),
)

/** 降级的语义色 —— AskPanel 用它决定提示条是灰是黄还是红。 */
export const DEGRADED_TAG: Record<string, TagType> = Object.fromEntries(
  Object.entries(DEGRADED_META).map(([k, v]) => [k, TAG_OF_SEVERITY[(v as EnumMeta).severity]]),
)

/**
 * 检索可信度的文案。
 *
 * 设计取向（借自 kotaemon）：**把"我有多确信"交给用户判断，而不是替用户决定**。
 * 所以低相关时不拦着不给答案，只是把话说明白。
 *
 * 判定线在后端（`ddp_corpus/config.py::qa_low_similarity`，那里写着实测分布），
 * 前端只负责显示 —— 校准值放两处一定会漂。
 *
 * 这一组**不在契约里**：它不是后端返回的枚举，而是前端按 `similarity` 与
 * `warn_below` 现算出来的三档展示状态。放进契约反而会让人以为后端会返回它。
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
  return contractDegradedLabelOf(value as Degraded | null)
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

export function runStatusOf(status: RunStatus): StatusMeta {
  return RUN_STATUS[status] ?? { label: status, type: 'info' }
}

export function fieldStatusOf(status: FieldStatus): StatusMeta {
  return FIELD_STATUS[status] ?? { label: status, type: 'info' }
}

/**
 * schema 叶子类型的中文名，编辑器下拉用。
 *
 * **不在契约里**：这是 JSON Schema 自己的类型词汇，不是 DeepDocParse 定义的枚举。
 * 受限子集的边界见 `packages/contracts/ddp/extract-format.md`。
 */
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
