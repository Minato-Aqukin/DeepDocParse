import type { EngineChoice } from '@/types/api'

/** 本地偏好（设置页写、上传对话框读）。放 localStorage：属于这台机器的习惯，不必上后端。 */
const ENGINE_KEY = 'ddp.pref.engine'

export function loadEnginePreference(): EngineChoice {
  try {
    const raw = localStorage.getItem(ENGINE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as EngineChoice
      // 空串是合法值（=用服务端缺省引擎），不能当成"没存过"丢掉
      if (parsed && typeof parsed.engine === 'string') {
        return { engine: parsed.engine, options: parsed.options ?? {} }
      }
    }
  } catch {
    // 存坏了就当没有，不能让偏好把上传功能拖垮
  }
  // 没存过：不自己猜引擎 —— 空串即"服务端决定"，表单拿到清单后会落到具体引擎上
  return { engine: '', options: {} }
}

export function saveEnginePreference(choice: EngineChoice) {
  localStorage.setItem(ENGINE_KEY, JSON.stringify(choice))
}
