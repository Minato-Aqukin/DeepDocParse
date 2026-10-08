import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  documentsApi: {
    get: vi.fn(),
    result: vi.fn(),
    pages: vi.fn(async () => ({ data: { document_id: 'd1', pages: [] } })),
    sourceViewUrl: vi.fn(async () => ({ data: { url: '/api/files/x' } })),
    validateIndex: vi.fn(),
  },
  downloadAs: vi.fn(),
  downloadViaSignedUrl: vi.fn(),
  conversationsApi: { evidence: vi.fn() },
}))

vi.mock('@/api/resource-context', () => ({
  documentContext: vi.fn(() => ({})),
}))

vi.mock('@/components/ask/AskPanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/viewer/PdfCanvas.vue', () => ({
  default: {
    name: 'PdfCanvasStub',
    props: ['pageIdx', 'highlights'],
    template: '<div />',
  },
}))
vi.mock('@/components/viewer/ResultPane.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/evidence/EvidencePreview.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/utils/reindex', () => ({ validateAndReindex: vi.fn() }))

import { documentsApi } from '@/api'
import WorkbenchView from '@/views/WorkbenchView.vue'

const indexingDoc = {
  id: 'd1', filename: 'manual.pdf', doc_id: 'hash', origin: 'web', mime: 'application/pdf',
  resource_id: null, source_version_id: null, size_bytes: 10, page_count: 6,
  // 解析已落定但索引还在建：succeeded 让 load() 走进第二段 await（result/pages），
  // 索引 active 让 reload 落定后起轮询 —— 两个条件各管一段，缺一就钉不住守卫。
  status: 'succeeded', error: null, index_status: 'indexing', index_error: null,
  compile_status: 'ready', compile_degraded: [], compile_fingerprint: '',
  layout_version: 'ddp-layout/1', code_detection: 'unavailable', current_job_id: 'job-1',
  created_at: new Date().toISOString(), uploaders: ['alice'], can_delete: true,
}

async function mountView() {
  setActivePinia(createPinia())
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/documents/:id', name: 'workbench', component: WorkbenchView }],
  })
  await router.push({ name: 'workbench', params: { id: 'd1' } })
  await router.isReady()
  const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus],
    stubs: { 'el-dropdown': true, 'el-dropdown-menu': true, 'el-dropdown-item': true } } })
  return wrapper
}

describe('WorkbenchView 卸载 mid-flight', () => {
  it('首刷在飞时卸载：resolve 后不许起轮询（无孤儿 interval）', async () => {
    vi.useFakeTimers()
    try {
      // load() 先 await get（立刻回来，document 落定为"索引还在建"），
      // 接着 await Promise.all(result/…)：把 result 门住，使"卸载"恰好落在这段里。
      vi.mocked(documentsApi.get).mockResolvedValue({ data: indexingDoc } as never)
      let releaseResult!: (value: { data: { document_id: string; markdown: string } }) => void
      const resultGate = new Promise<{ data: { document_id: string; markdown: string } }>(
        (res) => { releaseResult = res })
      vi.mocked(documentsApi.result).mockReturnValue(resultGate as never)

      const wrapper = await mountView()
      await flushPromises()
      // result 还在飞：页面先被卸载
      wrapper.unmount()
      releaseResult({ data: { document_id: 'd1', markdown: '# 手册' } })
      await flushPromises()
      // 判据是行为（推进时钟也不再刷），不是 vi.getTimerCount()：
      // Element Plus 的 tooltip/popover 也会挂定时器。
      const callsAfterResolve = vi.mocked(documentsApi.get).mock.calls.length
      await vi.advanceTimersByTimeAsync(10_000)
      await flushPromises()
      expect(vi.mocked(documentsApi.get).mock.calls.length).toBe(callsAfterResolve)
    } finally {
      vi.useRealTimers()
    }
  })

  it('对照：挂载中 resolve 会正常起轮询', async () => {
    vi.useFakeTimers()
    try {
      let releaseGet!: (value: { data: typeof indexingDoc }) => void
      const getGate = new Promise<{ data: typeof indexingDoc }>((res) => { releaseGet = res })
      vi.mocked(documentsApi.get).mockReturnValue(getGate as never)
      vi.mocked(documentsApi.result)
        .mockResolvedValue({ data: { document_id: 'd1', markdown: '# 手册' } } as never)

      const wrapper = await mountView()
      await flushPromises()
      releaseGet({ data: indexingDoc })
      await flushPromises()
      const callsBefore = vi.mocked(documentsApi.get).mock.calls.length
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(vi.mocked(documentsApi.get).mock.calls.length).toBeGreaterThan(callsBefore)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
