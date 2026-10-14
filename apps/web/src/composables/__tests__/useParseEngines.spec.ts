import { AxiosError, AxiosHeaders } from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { http } from '@/api/http'
import { __resetParseEnginesForTest, useParseEngines } from '../useParseEngines'
import type { ParseEngines } from '@/types/api'

const originalAdapter = http.defaults.adapter

function enginesResponse(payload: ParseEngines) {
  return async (config: InternalAxiosRequestConfig) => ({
    data: payload, status: 200, statusText: 'OK', headers: new AxiosHeaders(), config,
  })
}

const observed = (engines: ParseEngines['engines']): ParseEngines => ({
  default_engine: 'borndigital', capability_status: 'observed', engines,
})

function failed(status: number, config: InternalAxiosRequestConfig) {
  return new AxiosError(`Request failed with status code ${status}`, 'ERR_BAD_RESPONSE', config, undefined, {
    data: {}, status, statusText: String(status), headers: new AxiosHeaders(), config,
  })
}

beforeEach(() => {
  __resetParseEnginesForTest()
})

afterEach(() => {
  http.defaults.adapter = originalAdapter
})

describe('useParseEngines', () => {
  it('ready：清单与服务端缺省按服务端顺序原样给出', async () => {
    http.defaults.adapter = enginesResponse(observed([
      { engine: 'vlm-ocr', model: 'deepseek-ocr-2', readiness: 'ready' },
      { engine: 'borndigital', model: 'borndigital', readiness: 'ready' },
    ]))
    const { engines, defaultEngine, status } = useParseEngines()
    expect(status.value).toBe('loading')
    await flushPromises()
    expect(status.value).toBe('ready')
    expect(engines.value.map((e) => e.engine)).toEqual(['vlm-ocr', 'borndigital'])
    expect(defaultEngine.value).toBe('borndigital')
  })

  it('capability_status 为 unknown 时 unavailable（engines 为空，不自己猜引擎）', async () => {
    http.defaults.adapter = enginesResponse({
      default_engine: 'borndigital', capability_status: 'unknown', engines: [],
    })
    const { engines, status } = useParseEngines()
    await flushPromises()
    expect(status.value).toBe('unavailable')
    expect(engines.value).toEqual([])
  })

  it('observed 但清单为空同样 unavailable', async () => {
    http.defaults.adapter = enginesResponse(observed([]))
    const { status } = useParseEngines()
    await flushPromises()
    expect(status.value).toBe('unavailable')
  })

  it('请求失败（桌面本地运行时的 404 也走这里）时 unavailable', async () => {
    http.defaults.adapter = async (config) => {
      throw failed(404, config)
    }
    const { engines, defaultEngine, status } = useParseEngines()
    await flushPromises()
    expect(status.value).toBe('unavailable')
    expect(engines.value).toEqual([])
    expect(defaultEngine.value).toBe('')
  })

  it('一页只取一次：多处挂载共享同一个 in-flight 请求', async () => {
    let calls = 0
    http.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
      calls += 1
      return {
        data: observed([{ engine: 'borndigital', model: 'borndigital', readiness: 'ready' }]),
        status: 200, statusText: 'OK', headers: new AxiosHeaders(), config,
      }
    }
    const first = useParseEngines()
    const second = useParseEngines()
    const third = useParseEngines()
    await flushPromises()
    expect(calls).toBe(1)
    // 落定之后再挂载也不重发：同一页里清单是静态的
    const fourth = useParseEngines()
    expect(calls).toBe(1)
    for (const api of [first, second, third, fourth]) {
      expect(api.status.value).toBe('ready')
      expect(api.engines.value).toHaveLength(1)
    }
  })

  it('发的是 GET /api/parse-engines 且失败不弹全局 toast', async () => {
    let seen: InternalAxiosRequestConfig | undefined
    http.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
      seen = config
      throw failed(500, config)
    }
    useParseEngines()
    await Promise.resolve()
    await Promise.resolve()
    // suppressErrorToast 由拦截器消费，这里只能断言请求上带了这个标记 ——
    // 对应变异：把 suppressErrorToast 删掉，这里必须变红
    expect(seen?.url).toBe('/api/parse-engines')
    expect(seen?.method?.toLowerCase()).toBe('get')
    expect(seen?.suppressErrorToast).toBe(true)
  })
})
