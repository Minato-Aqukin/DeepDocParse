import { DEFAULT_ENGINE, defaultOptions } from '@/constants/engines'
import type { EngineChoice } from '@/types/api'

/** 本地偏好（设置页写、上传对话框读）。放 localStorage：属于这台机器的习惯，不必上后端。 */
const ENGINE_KEY = 'ddp.pref.engine'

export function loadEnginePreference(): EngineChoice {
  try {
    const raw = localStorage.getItem(ENGINE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as EngineChoice
      if (parsed?.engine) return { engine: parsed.engine, options: parsed.options ?? {} }
    }
  } catch {
    // 存坏了就当没有，不能让偏好把上传功能拖垮
  }
  return { engine: DEFAULT_ENGINE, options: defaultOptions(DEFAULT_ENGINE) }
}

export function saveEnginePreference(choice: EngineChoice) {
  localStorage.setItem(ENGINE_KEY, JSON.stringify(choice))
}
