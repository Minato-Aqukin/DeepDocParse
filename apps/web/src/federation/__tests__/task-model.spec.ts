import { TASK_STATUS_VALUES } from '@deepdocparse/contracts'
import { describe, expect, it } from 'vitest'

import { citationIndex, collectEvents, isSettled, splitReason, type EventPage, type FederatedEvidence, type TaskEvent } from '../task-model'

describe('isSettled', () => {
  it('按契约的 active 标记判断：进行中的状态继续轮询，终态停下', () => {
    expect(isSettled({ status: 'queued' })).toBe(false)
    expect(isSettled({ status: 'claimed' })).toBe(false)
    expect(isSettled({ status: 'running' })).toBe(false)
    expect(isSettled({ status: 'succeeded' })).toBe(true)
    expect(isSettled({ status: 'failed' })).toBe(true)
    expect(isSettled({ status: 'cancelled' })).toBe(true)
  })

  it('契约里的每个状态都有明确结论；认不出的状态不无限轮询', () => {
    for (const value of TASK_STATUS_VALUES) expect(typeof isSettled({ status: value })).toBe('boolean')
    expect(isSettled({ status: 'teleporting' as never })).toBe(true)
  })
})

describe('splitReason', () => {
  it('按第一个冒号拆成代码与细节', () => {
    expect(splitReason('receipt_binding_mismatch:plan_digest')).toEqual({ code: 'receipt_binding_mismatch', detail: 'plan_digest' })
    expect(splitReason('peer_unavailable:http_503')).toEqual({ code: 'peer_unavailable', detail: 'http_503' })
    expect(splitReason('insufficient_evidence')).toEqual({ code: 'insufficient_evidence', detail: null })
  })
})

describe('citationIndex', () => {
  it('答案里的 [n] 对应证据列表的第 n 条（从 1 开始）', () => {
    const index = citationIndex([{ evidence_id: 'ev-a' }, { evidence_id: 'ev-b' }] as FederatedEvidence[])
    expect(index.get('ev-a')).toBe(1)
    expect(index.get('ev-b')).toBe(2)
    expect(index.get('ev-missing')).toBeUndefined()
  })
})

describe('collectEvents', () => {
  const event = (seq: number): TaskEvent => ({ seq, type: 'plan_ready', at: '2026-09-15T08:00:00Z' })

  it('按 seq 去重：重连后服务端重发同一段，界面不出现两条同号事件', async () => {
    const pages: EventPage[] = [
      { root_task_id: 'r', events: [event(2), event(3)], next_seq: 3, complete: true },
    ]
    const result = await collectEvents(async () => pages.shift()!, 1, [2])
    expect(result.events.map((item) => item.seq)).toEqual([3])
    expect(result.next).toBe(3)
  })

  it('没追平就继续读，追平或空页就停', async () => {
    const afters: number[] = []
    const pages: EventPage[] = [
      { root_task_id: 'r', events: [event(1)], next_seq: 1, complete: false },
      { root_task_id: 'r', events: [event(2)], next_seq: 2, complete: true },
    ]
    const result = await collectEvents(async (after: number) => { afters.push(after); return pages.shift()! }, 0, [])
    expect(afters).toEqual([0, 1])
    expect(result.events.map((item) => item.seq)).toEqual([1, 2])

    const empty = await collectEvents(async () => ({ root_task_id: 'r', events: [], next_seq: 9, complete: false }), 9, [])
    expect(empty.events).toEqual([])
    expect(empty.next).toBe(9)
  })

  it('一次刷新最多读 cap 页，剩下的留给下一轮', async () => {
    let calls = 0
    const result = await collectEvents(async (after: number) => {
      calls++
      return { root_task_id: 'r', events: [event(after + 1)], next_seq: after + 1, complete: false }
    }, 0, [], { cap: 3 })
    expect(calls).toBe(3)
    expect(result.next).toBe(3)
  })

  it('这次续读作废了就立刻停，不再白打后面的请求', async () => {
    let calls = 0
    let alive = true
    const result = await collectEvents(async (after: number) => {
      calls++
      alive = false   // 第一页回来时路由已经切走
      return { root_task_id: 'r', events: [event(after + 1)], next_seq: after + 1, complete: false }
    }, 0, [], { stale: () => !alive })
    expect(calls).toBe(1)
    expect(result.events.map((item) => item.seq)).toEqual([1])
  })

  it('形状不对当场抛，不把 after 变成 undefined 反复从头拉', async () => {
    await expect(collectEvents(async () => ({ root_task_id: 'r', events: [event(1)] } as never), 0, []))
      .rejects.toThrow('事件流格式不兼容')
  })
})
