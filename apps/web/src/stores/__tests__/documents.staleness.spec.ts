import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { list } = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api', () => ({
  documentsApi: { list, stats: vi.fn(async () => ({ data: { documents: 0, pages: 0, askable: 0 } })) },
}))

import { useDocumentsStore } from '../documents'
import type { DocumentInfo } from '@/types/api'

function row(id: string, filename: string, index_status: DocumentInfo['index_status'] = 'ready'): DocumentInfo {
  return {
    id, filename, doc_id: `d-${id}`, origin: 'web', mime: 'application/pdf',
    resource_id: null, source_version_id: null,
    size_bytes: 1, page_count: 1, status: 'succeeded', error: null,
    index_status: index_status, index_error: null, compile_status: 'failed',
    compile_degraded: [], compile_fingerprint: '',
    layout_version: 'ddp-layout/1', code_detection: 'unavailable', current_job_id: 'job',
    created_at: new Date().toISOString(), uploaders: ['alice'], can_delete: true,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('documents store 取列表竞态', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    list.mockReset()
  })

  it('后发先到：旧请求的晚到响应不许冲掉新筛选的结果', async () => {
    const store = useDocumentsStore()
    const oldReq = deferred<{ data: DocumentInfo[] }>()
    const newReq = deferred<{ data: DocumentInfo[] }>()
    list.mockReturnValueOnce(oldReq.promise).mockReturnValueOnce(newReq.promise)

    const first = store.fetchList()
    // 用户在旧请求飞行途中改了筛选又发一次
    store.filters.q = 'abc'
    const second = store.fetchList()
    // 旧响应晚到（按旧参数查的 all 行），新响应先到
    newReq.resolve({ data: [row('new', 'abc-manual.pdf')] })
    await second
    oldReq.resolve({ data: [row('old', 'unrelated.pdf')] })
    await first

    expect(store.items.map((d) => d.id)).toEqual(['new'])
  })

  it('请求参数在发出前快照：await 之后改筛选不影响已发请求，也不用新后过滤套旧行', async () => {
    const store = useDocumentsStore()
    const req = deferred<{ data: DocumentInfo[] }>()
    list.mockReturnValueOnce(req.promise)

    store.filters.indexStatus = 'ready'
    const pending = store.fetchList()
    // 飞行途中用户切了后过滤条件：已发请求的参数必须是旧快照
    store.filters.indexStatus = 'failed'
    req.resolve({ data: [row('a', 'a.pdf', 'ready'), row('b', 'b.pdf', 'failed')] })
    await pending

    expect(list).toHaveBeenCalledTimes(1)
    expect(list.mock.calls[0]![0]).toMatchObject({ q: undefined, status: undefined })
    // 后过滤用的是请求发出时的 'ready'，不是飞行途中改成的 'failed'
    expect(store.items.map((d) => d.id)).toEqual(['a'])
  })

  it('stale 的请求不许复位 loading：复位是最新那次的责任', async () => {
    const store = useDocumentsStore()
    const oldReq = deferred<{ data: DocumentInfo[] }>()
    const newReq = deferred<{ data: DocumentInfo[] }>()
    list.mockReturnValueOnce(oldReq.promise).mockReturnValueOnce(newReq.promise)

    const first = store.fetchList()
    const second = store.fetchList()
    expect(store.loading).toBe(true)
    oldReq.resolve({ data: [] })
    await first
    // 旧请求结束了，最新请求还在飞：loading 必须还亮着
    expect(store.loading).toBe(true)
    newReq.resolve({ data: [] })
    await second
    expect(store.loading).toBe(false)
  })
})
