/**
 * 版面编译的状态与降级文案 —— 同样全部来自契约。
 *
 * `compile_degraded` 与问答那条 `degraded` **是两个枚举**，因为它是列表：
 * 一次编译可以同时有好几种降级，落在 `documents.compile_degraded`（JSON 数组）上。
 * 合并成一个枚举会让"一次只报一个"这条约定失效。
 */
import {
  CODE_DETECTION_META,
  COMPILE_DEGRADED_META,
  type CodeDetection,
  type CompileStatus,
  type EnumMeta,
} from '@deepdocparse/contracts'

import { COMPILE_STATUS, type StatusMeta } from '@/constants/status'

export { COMPILE_STATUS }

export function compileStatusOf(value: string | undefined): StatusMeta {
  return COMPILE_STATUS[value as CompileStatus] ?? COMPILE_STATUS.none
}

const TAG_OF_SEVERITY = {
  neutral: 'info',
  progress: 'warning',
  ok: 'success',
  warn: 'warning',
  error: 'danger',
} as const

export const CODE_DETECTION: Record<CodeDetection, StatusMeta> = Object.fromEntries(
  Object.entries(CODE_DETECTION_META).map(([k, v]) => [
    k,
    { label: (v as EnumMeta).label, type: TAG_OF_SEVERITY[(v as EnumMeta).severity] },
  ]),
) as Record<CodeDetection, StatusMeta>

export function codeDetectionOf(value: string | undefined): StatusMeta {
  return CODE_DETECTION[value as CodeDetection] ?? CODE_DETECTION.unavailable
}

export const COMPILE_DEGRADED: Record<string, string> = Object.fromEntries(
  Object.entries(COMPILE_DEGRADED_META).map(([k, v]) => [k, (v as EnumMeta).label]),
)
