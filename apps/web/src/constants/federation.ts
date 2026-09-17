/**
 * 联邦任务的文案与状态映射 —— **取值与文案全部来自契约生成物**，这里只做 UI 映射
 * （与 `constants/status.ts` 同一套：语义色 → tag type 只在那边写一次）。
 *
 * 不在这里手写任何枚举取值的中文：后端每加一个状态/原因/事件，契约生成物里就有
 * 文案；界面遇到契约里没有的值显示原始代码，而不是空白（不变式 2）。
 */
import {
  COVERAGE_TARGET_STATE_META,
  DELIVERY_STATE_META,
  ENUMERATION_STATE_META,
  EVIDENCE_CONFLICT_BASIS_META,
  EVIDENCE_SUFFICIENCY_META,
  FEDERATED_ANSWER_REASON_META,
  PLANNING_STATE_META,
  RETENTION_CLASS_META,
  RETRIEVAL_COMPLETENESS_META,
  SEARCH_MODE_META,
  SOURCE_TYPE_META,
  TASK_EVENT_TYPE_META,
  TASK_STATUS_META,
  VALIDATION_STATE_META,
  type EnumMeta,
  type FederatedAnswerReason,
} from '@deepdocparse/contracts'

import { splitReason } from '@/federation/task-model'

import { mapMeta, toStatusMeta, type StatusMeta } from './status'

export const TASK_STATUS = mapMeta(TASK_STATUS_META)
export const PLANNING_STATE = mapMeta(PLANNING_STATE_META)
export const RETRIEVAL_COMPLETENESS = mapMeta(RETRIEVAL_COMPLETENESS_META)
export const EVIDENCE_SUFFICIENCY = mapMeta(EVIDENCE_SUFFICIENCY_META)
export const DELIVERY_STATE = mapMeta(DELIVERY_STATE_META)
export const COVERAGE_TARGET_STATE = mapMeta(COVERAGE_TARGET_STATE_META)
export const ENUMERATION_STATE = mapMeta(ENUMERATION_STATE_META)
export const VALIDATION_STATE = mapMeta(VALIDATION_STATE_META)
export const TASK_EVENT_TYPE = mapMeta(TASK_EVENT_TYPE_META)

/** 查一张由契约生成的状态表；契约里没有的值给出原始代码，不给空白。 */
export function metaOf(table: Record<string, StatusMeta>, value: string | null | undefined): StatusMeta {
  if (!value) return { label: '—', type: 'info' }
  return table[value] ?? { label: `未知取值（${value}）`, type: 'info' }
}

function labelOf(table: Record<string, EnumMeta>, value: string | null | undefined): string {
  if (!value) return '—'
  return table[value]?.label ?? `未知取值（${value}）`
}

export const searchModeLabel = (value: string | null | undefined) => labelOf(SEARCH_MODE_META, value)
export const conflictBasisLabel = (value: string | null | undefined) => labelOf(EVIDENCE_CONFLICT_BASIS_META, value)
export const retentionLabel = (value: string | null | undefined) => labelOf(RETENTION_CLASS_META, value)
export const sourceTypeLabel = (value: string | null | undefined) => labelOf(SOURCE_TYPE_META, value)

/**
 * 答案原因：代码查契约文案，冒号后面的细节（对端状态码、出错字段）原样附在括号里。
 * 生成的 `federatedAnswerReasonLabelOf` 不去后缀，所以不能直接用它（契约描述写明了）。
 */
export function answerReason(reason: string | null | undefined): StatusMeta | null {
  if (!reason) return null
  const { code, detail } = splitReason(reason)
  const meta = FEDERATED_ANSWER_REASON_META[code as FederatedAnswerReason]
  if (!meta) return { label: `未知原因（${reason}）`, type: 'danger' }
  const base = toStatusMeta(meta)
  return detail ? { ...base, label: `${base.label}（${detail}）` } : base
}

/** 范围类型不是生成枚举（TaskSpec.resource_scope.kind 在 schema 里内联），文案只用于展示。 */
export const SCOPE_KIND_LABEL: Record<string, string> = {
  local_only: '仅本机',
  site_public: '本站公开',
  federation_public: '联邦公开范围',
  fixed_resources: '指定资源',
}

/**
 * 外发许可里可勾选的载荷类别（`ddp-task-probe/v1.json` 的 `allowed_payload`）。
 *
 * 这张表不是 `enums.yaml` 的生成物 —— 取值内联在 JSON schema 里，所以前端只能抄一份。
 * **抄的东西会漂**：schema 加一类外发而这里没加，用户就授不了权（那类目标被记成 denied，
 * 看起来像"对方拒绝"）；这里多一个 schema 没有的，请求直接 403。
 * `__tests__/federation.spec.ts` 对着 schema 文件逐字比对钉死这件事。
 */
export const PROBE_PAYLOAD_LABEL: Record<string, string> = {
  query_text: '问题原文',
  subquery_text: '拆解后的子问题',
  entity_names: '实体名',
  resource_names: '资源名',
  collection_filters: '集合筛选条件',
  evidence_excerpts: '证据摘录',
  source_files: '原文件',
}

/** 计划数据边上实际外发的内容（`ddp-plan-admission/v1.json` 的 `payload_kind`）。 */
export const PAYLOAD_KIND_LABEL: Record<string, string> = {
  query_text: '问题原文',
  evidence_excerpts: '证据摘录',
  source_files: '原文件',
  parsed_layout: '版面解析结果',
  embeddings: '向量',
  answer_text: '答案文本',
  wiki_draft: 'Wiki 草稿',
}

export const probePayloadLabel = (value: string) => PROBE_PAYLOAD_LABEL[value] ?? value
export const payloadKindLabel = (value: string) => PAYLOAD_KIND_LABEL[value] ?? value
