import type { CodeDetection, CompileStatus } from '@/types/api'
import type { StatusMeta } from '@/constants/status'

export const COMPILE_STATUS: Record<CompileStatus, StatusMeta> = {
  none: { label: '未编译', type: 'info', active: false },
  pending: { label: '待编译', type: 'info', active: true },
  compiling: { label: '编译中', type: 'warning', active: true },
  ready: { label: '编译完整', type: 'success', active: false },
  partial: { label: '编译有降级', type: 'warning', active: false },
  failed: { label: '编译失败', type: 'danger', active: false },
}

export function compileStatusOf(value: string | undefined): StatusMeta {
  return COMPILE_STATUS[value as CompileStatus] ?? COMPILE_STATUS.none
}

export const CODE_DETECTION: Record<CodeDetection, StatusMeta> = {
  native: { label: '代码识别：原生', type: 'success' },
  heuristic: { label: '代码识别：启发式', type: 'info' },
  unavailable: { label: '代码识别：不可用', type: 'warning' },
}

export function codeDetectionOf(value: string | undefined): StatusMeta {
  return CODE_DETECTION[value as CodeDetection] ?? CODE_DETECTION.unavailable
}

export const COMPILE_DEGRADED: Record<string, string> = {
  code_detection_unavailable: '当前版面引擎不能识别代码块',
  crop_unsupported: '部分视觉原子没有可定位裁图',
  crop_failed: '部分视觉原子裁图失败',
  vision_unavailable: '视觉理解模型不可用',
  vision_invalid_output: '视觉理解模型返回的结构不合规',
  provider_unresolved: '上游实际模型未解析，当前编译版本不可比较',
  reindex_validation_required: '存在历史出处，需先校验并确认后重建',
  compile_failed: '版面编译失败',
}
