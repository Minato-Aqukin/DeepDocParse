import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  documentsApi: {
    list: vi.fn(),
    stats: vi.fn(),
    remove: vi.fn(),
    exportUrl: vi.fn(),
  },
  downloadAs: vi.fn(),
  downloadViaSignedUrl: vi.fn(),
}))

vi.mock('@/api/resource-context', () => ({
  documentContext: vi.fn(() => ({})),
}))

vi.mock('@/utils/reindex', () => ({ validateAndReindex: vi.fn() }))

import { documentsApi } from '@/api'
import DocumentsView from '@/views/DocumentsView.vue'
import type { DocumentInfo } from '@/types/api'

function activeDoc(): DocumentInfo {
  return {
    id: 'd1', resource_id: null, source_version_id: null, filename: 'a.pdf',
    doc_id: 'hash', origin: 'web', mime: 'application/pdf', size_bytes: 10,
    page_count: 1, status: 'running', error: null, index_status: 'ready',
    index_error: null, compile_status: 'ready', compile_degraded: [],
    compile_fingerprint: '', layout_version: 'ddp-layout/1', code_detection: 'unavailable',
    current_job_id: 'job-1', created_at: new Date().toISOString(),
    uploaders: ['alice'], can_delete: true,
  }
}

async function mountView() {
  setActivePinia(createPinia())
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/documents', name: 'documents', component: DocumentsView },
      { path: '/search', name: 'search', component: { template: '<div />' } },
    ],
  })
  await router.push('/documents')
  await router.isReady()
  // el-select 自带递归更新问题时只桩掉下拉（DocumentFilters.debounce.spec.ts 同款），
  // 轮询断言与下拉渲染无关。
  const wrapper = mount(DocumentsView, { global: { plugins: [router, ElementPlus],
    stubs: { 'el-select': true, 'el-option': true, 'el-dropdown': true,
      'el-dropdown-menu': true, 'el-dropdown-item': true } } })
  return wrapper
}

describe('DocumentsView 卸载 mid-flight', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(documentsApi.stats).mockResolvedValue({ data: { documents: 1, pages: 1, askable: 0 } } as never)
  })

  it('首刷在飞时卸载：resolve 后不许起轮询（无孤儿 interval）', async () => {
    try {
      let releaseList!: (value: { data: DocumentInfo[] }) => void
      const listGate = new Promise<{ data: DocumentInfo[] }>((res) => { releaseList = res })
      vi.mocked(documentsApi.list).mockReturnValue(listGate as never)

      const wrapper = await mountView()
      await flushPromises()
      // 首刷的 list 还在飞：页面先被卸载（路由切走）
      wrapper.unmount()
      // 现在首刷返回，内容还是"有任务在动" —— 但组件已经没了，不许起轮询。
      // 判据是行为（推进时钟也不再刷），不是 vi.getTimerCount()：Element Plus
      // 的 tooltip/popover 在 jsdom 里也会挂定时器，数时钟数会数到它们头上。
      releaseList({ data: [activeDoc()] })
      await flushPromises()
      const callsAfterResolve = vi.mocked(documentsApi.list).mock.calls.length
      await vi.advanceTimersByTimeAsync(10_000)
      await flushPromises()
      expect(vi.mocked(documentsApi.list).mock.calls.length).toBe(callsAfterResolve)
    } finally {
      vi.useRealTimers()
    }
  })

  it('对照：挂载中 resolve 会正常起轮询', async () => {
    try {
      let releaseList!: (value: { data: DocumentInfo[] }) => void
      const listGate = new Promise<{ data: DocumentInfo[] }>((res) => { releaseList = res })
      vi.mocked(documentsApi.list).mockReturnValue(listGate as never)

      const wrapper = await mountView()
      await flushPromises()
      releaseList({ data: [activeDoc()] })
      await flushPromises()
      // 还在挂载中：有活动任务就该起轮询 —— tick 会再刷（不数时钟总数，见上）。
      const callsBefore = vi.mocked(documentsApi.list).mock.calls.length
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(vi.mocked(documentsApi.list).mock.calls.length).toBeGreaterThan(callsBefore)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
