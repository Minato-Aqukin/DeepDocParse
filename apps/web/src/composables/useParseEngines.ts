import { ref } from 'vue'

import { documentsApi } from '@/api/documents'
import type { ParseEngineEntry } from '@/types/api'

/**
 * 解析引擎清单：`GET /api/parse-engines` 的前端唯一入口。
 *
 * 拿到一次就不再问：所有挂载点（上传对话框、重解析、设置页）共享同一个
 * in-flight promise 与同一份结果。清单在一次页面生命周期里被认为是静态的 ——
 * 注册表变了就刷新页面。没拿到（unavailable）则下次挂载时再问一次。
 *
 * - `ready`：服务端给了可用清单（observed 且至少一条）；
 * - `unavailable`：请求失败（桌面本地运行时根本没有这个端点，直接 404）、
 *   `capability_status === 'unknown'`、或清单为空 —— 调用方不要自己猜引擎，
 *   按"服务端缺省引擎"提交（`engine: ''`），这在任何运行时都是合法的。
 */
export type ParseEngineStatus = 'loading' | 'ready' | 'unavailable'

const engines = ref<ParseEngineEntry[]>([])
const defaultEngine = ref('')
const status = ref<ParseEngineStatus>('loading')
let inFlight: Promise<void> | null = null

function fetchEngines(): Promise<void> {
  if (!inFlight) {
    inFlight = documentsApi.parseEngines()
      .then(({ data }) => {
        if (data.capability_status === 'observed' && data.engines.length > 0) {
          // 照服务端顺序原样列，不重排
          engines.value = data.engines
          defaultEngine.value = data.default_engine
          status.value = 'ready'
        } else {
          status.value = 'unavailable'
        }
      })
      .catch(() => {
        status.value = 'unavailable'
      })
      .finally(() => {
        inFlight = null
      })
  }
  return inFlight
}

export function useParseEngines() {
  // 首个挂载点触发请求；loading 期间后挂载的共用同一个 in-flight promise。
  // 拿不到（unavailable）不当终局：下次挂载（再开一次上传框）再问一次 —— 一次网络抖动
  // 不该让整页都退回"服务端缺省"。重试期间保持 unavailable，不闪回 loading。
  if (status.value !== 'ready') void fetchEngines()
  return { engines, defaultEngine, status }
}

/** 单测专用：把模块状态清回初始态。 */
export function __resetParseEnginesForTest() {
  engines.value = []
  defaultEngine.value = ''
  status.value = 'loading'
  inFlight = null
}
