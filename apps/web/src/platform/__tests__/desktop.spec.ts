import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiUrl, bootSource, checkSourceResponse, egressStatus, initDesktopSource, isDesktop, sourceErrorLabel, workspaceFailure } from '@/platform/desktop'

function setDesktop(bridge: unknown) {
  Object.defineProperty(window, 'ddpDesktop', { value: bridge, configurable: true, writable: true })
}

function clearDesktop() {
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
}

beforeEach(() => {
  clearDesktop()
  bootSource.value = null
})

describe('apiUrl', () => {
  it('浏览器里原样返回相对路径', () => {
    expect(apiUrl('/api/resources')).toBe('/api/resources')
  })

  it('桌面下相对路径指回宿主 ddp://app', () => {
    setDesktop({})
    expect(apiUrl('/api/resources')).toBe('ddp://app/api/resources')
  })

  it('桌面下无前导斜杠也补上', () => {
    setDesktop({})
    expect(apiUrl('api/search?q=x')).toBe('ddp://app/api/search?q=x')
  })

  it('绝对地址（预签名 https、宿主改写的 _object）一律原样返回', () => {
    setDesktop({})
    expect(apiUrl('https://s3.example.com/part?sig=abc')).toBe('https://s3.example.com/part?sig=abc')
    expect(apiUrl('ddp://app/_object/opaque-id')).toBe('ddp://app/_object/opaque-id')
  })
})

describe('启动时当前源正在重连', () => {
  it('等它落定再给出启动源，而不是当成没有数据源', async () => {
    vi.useFakeTimers()
    const summary = (state: string) => ({ sourceId: 's1', kind: 'local', label: 'ws', state, readOnly: false,
      features: ['resources'], active: true, reason: null })
    const states = ['connecting', 'connecting', 'ready']
    setDesktop({ sourceList: vi.fn(async () => ({ ok: true, value: [summary(states.shift() ?? 'ready')] })),
      onSourceChange: () => () => {} })
    const booted = initDesktopSource()
    await vi.advanceTimersByTimeAsync(1000)
    await booted
    vi.useRealTimers()
    expect(bootSource.value?.state).toBe('ready')
  })
})

describe('来源校验', () => {
  it('浏览器永远通过', () => {
    expect(checkSourceResponse('other-source')).toBe(true)
  })

  it('桌面无启动源时通过（旧宿主或首运）', () => {
    setDesktop({})
    expect(checkSourceResponse('anything')).toBe(true)
  })

  it('桌面缺头时通过（旧宿主不带 X-DDP-Source）', () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 's1', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    expect(checkSourceResponse(null)).toBe(true)
  })

  it('桌面下头与启动源一致才通过', () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 's1', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    expect(checkSourceResponse('s1')).toBe(true)
    expect(checkSourceResponse('s2')).toBe(false)
  })
})

describe('顶栏外发状态', () => {
  it('无源、本地、中心三态文案', () => {
    expect(egressStatus(null)).toEqual({ text: '未选择数据源', kind: 'none' })
    expect(egressStatus({
      sourceId: 's', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    })).toEqual({ text: '本机执行 · 原件不外发', kind: 'local' })
    expect(egressStatus({
      sourceId: 's', kind: 'center', label: 'c', state: 'ready',
      readOnly: true, features: [], active: true, reason: null,
    })).toEqual({ text: '只读 · 写入须作为联邦任务发起并批准', kind: 'center' })
  })
})

describe('isDesktop', () => {
  it('无桥时为 false，有桥时为 true', () => {
    expect(isDesktop()).toBe(false)
    setDesktop({})
    expect(isDesktop()).toBe(true)
  })
})

describe('数据源错误文案', () => {
  it('连接中心失败的宿主码（desktop_error）也有中文文案，不显示「未知取值」', () => {
    expect(sourceErrorLabel('identity_mismatch')).toBe('环境身份与已配对记录不一致。')
    expect(sourceErrorLabel('authentication_required')).toBe('此身份需要重新认证。')
    expect(sourceErrorLabel('approved_plan_required')).toBe('中心在桌面里只读；写操作请作为联邦任务发起并批准')
    expect(sourceErrorLabel('made_up')).toBe('未知取值（made_up）')
  })

  it('落账的失败码只出现一次：有文案写「文案（码）」，未知码的兜底已带码不再重复', () => {
    expect(workspaceFailure('transport_error')).toBe('读取中心时连接中断，没有确认任何结果；可以再次读取，不会重复任何写入。（transport_error）')
    expect(workspaceFailure('made_up')).toBe('未知取值（made_up）')
  })
})

describe('desktop 模块无副作用', () => {
  it('import 时不读宿主', async () => {
    // 故意走动态 import：要验证的是"模块求值本身"不碰宿主，
    // 静态 import 在文件头就求值了，测不到这件事。
    vi.resetModules()
    setDesktop({ sourceList: vi.fn(async () => { throw new Error('must not call') }) })
    await import('@/platform/desktop')
    expect(isDesktop()).toBe(true)
  })
})
