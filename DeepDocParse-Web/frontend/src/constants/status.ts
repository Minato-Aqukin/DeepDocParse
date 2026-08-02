/**
 * 状态与降级的文案表 —— 全站唯一来源。
 *
 * 后端新增一种状态或降级值时**只改这个文件**：以前要在 DocumentsView 补标签、
 * 在 AskPanel 补文案、在工作台再补一次，漏一处用户就看到一个原始英文枚举。
 *
 * 这里同时是将来上 i18n 的收口：换成 t('status.parse.running') 只动本文件。
 */
import type { IndexStatus, ParseStatus } from '@/types/api'

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
  embedding_unavailable: '仅关键词检索（向量化服务不可用）',
  vision_unavailable: '未做视觉验证（视觉模型不可用）',
  crop_unsupported: '未做视觉验证（该文件不支持区域截图）',
  crop_failed: '未做视觉验证（区域截图失败）',
  client_aborted: '回答被中断',
  upstream_error: '问答服务异常',
  upstream_interrupted: '回答生成中途断流',
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
