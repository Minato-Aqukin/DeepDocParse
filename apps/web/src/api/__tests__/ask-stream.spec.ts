import { afterEach, describe, expect, it, vi } from 'vitest'

import { askStream } from '@/api/conversations'
import { bootSource } from '@/platform/desktop'

/** 一次把整段 SSE 文本交给 fetch 的应答体，读完即"干净"地结束。 */
function answer(sse: string) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(sse, {
    status: 200, headers: { 'content-type': 'text/event-stream' },
  })))
}

async function run() {
  const errors: string[] = []
  const done = vi.fn()
  const settled = Promise.withResolvers<void>()
  askStream('c1', '复位延时是多少？', {
    onDelta: vi.fn(),
    onDone: done,
    onError: ({ code }) => errors.push(code),
    onSettled: settled.resolve,
  })
  await settled.promise
  return { errors, done }
}

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

afterEach(() => {
  vi.unstubAllGlobals()
  bootSource.value = null
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
})

describe('askStream 的结束判定', () => {
  it('流在 done 之前"干净"结束（生成中途进程被杀）报 stream_incomplete，而不是什么都不说', async () => {
    answer(frame('meta', { query_decision: {}, retrieval: {} }) + frame('delta', { text: 'RP2040 has' }))
    const { errors, done } = await run()
    expect(errors).toEqual(['stream_incomplete'])
    expect(done).not.toHaveBeenCalled()
  })

  it('有 done 帧的正常一轮不报错', async () => {
    answer(frame('delta', { text: '264KB' }) + frame('done', { message_id: 'm1', verified: false, degraded: null }))
    expect((await run()).errors).toEqual([])
  })

  it('服务端自己说明过的结束（只有 error 帧，如访问被撤销）不再追加一条中断', async () => {
    answer(frame('error', { message: 'source access was revoked', code: 'resource_access_revoked' }))
    expect((await run()).errors).toEqual(['resource_access_revoked'])
  })
})

describe('askStream 桌面数据源校验', () => {
  it('应答来自已切走的旧数据源（X-DDP-Source 不符）：整条流丢弃，报 source_changed，一个字都不显示', async () => {
    Object.defineProperty(window, 'ddpDesktop', { value: {}, configurable: true, writable: true })
    bootSource.value = { sourceId: 'local-1', kind: 'local', label: '乙', state: 'ready', readOnly: false,
      features: [], active: true, reason: null } as never
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      frame('delta', { text: '旧源的回答' }) + frame('done', { message_id: 'm1', verified: false, degraded: null }),
      { status: 200, headers: { 'content-type': 'text/event-stream', 'X-DDP-Source': 'local-0' } })))
    const deltas: string[] = []
    const errors: string[] = []
    const settled = Promise.withResolvers<void>()
    askStream('c1', '复位延时是多少？', {
      onDelta: (text) => deltas.push(text),
      onError: ({ code }) => errors.push(code),
      onSettled: settled.resolve,
    })
    await settled.promise
    expect(errors).toEqual(['source_changed'])
    expect(deltas).toEqual([])
  })
})
