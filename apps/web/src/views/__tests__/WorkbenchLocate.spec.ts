import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import type { EvidenceDetail } from '@/types/api'

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  documentsApi: {
    get: vi.fn(),
    result: vi.fn(async () => ({ data: { document_id: 'd1', markdown: '# 手册' } })),
    pages: vi.fn(),
    sourceViewUrl: vi.fn(async () => ({ data: { url: '/api/files/x' } })),
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

import { conversationsApi, documentsApi } from '@/api'
import WorkbenchView from '@/views/WorkbenchView.vue'

const doc = {
  id: 'd1', filename: 'manual.pdf', doc_id: 'hash', origin: 'web', mime: 'application/pdf',
  resource_id: null, source_version_id: null, size_bytes: 10, page_count: 6,
  status: 'succeeded', error: null, index_status: 'ready', index_error: null,
  compile_status: 'ready', compile_degraded: [], compile_fingerprint: '',
  layout_version: 'ddp-layout/1', code_detection: 'unavailable', current_job_id: 'job-1',
  created_at: new Date().toISOString(), uploaders: ['alice'], can_delete: true,
}

const pages = {
  document_id: 'd1', pages: [
    { page_idx: 2, page_size: [595, 842], blocks: [
      { chunk_id: 'chunk-new', seq: 42, page_idx: 2, bbox: [10, 20, 30, 40],
        page_size: [595, 842], text: '目标段落' },
    ] },
  ],
}

function evidenceDetail(): EvidenceDetail {
  return {
    id: 'ev-9', resource_id: null, source_version_id: null,
    document: { id: 'd1', filename: 'manual.pdf' },
    page_idx: 2, printed_page_label: null, seq: 42, parse_job_id: 'job-9', doc_version: 1,
    bbox: [10, 20, 30, 40], page_size: [595, 842], kind: 'block', content: '目标段落',
    source_type: 'source', derived_from: null, crop_url: null, review_state: 'unreviewed',
    chunk_id: 'chunk-old', verifications: [],
  }
}

async function mountWithQuery(query: Record<string, string>) {
  setActivePinia(createPinia())
  vi.mocked(documentsApi.get).mockResolvedValue({ data: doc } as never)
  vi.mocked(documentsApi.pages).mockResolvedValue({ data: pages } as never)
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/documents/:id', name: 'workbench', component: WorkbenchView }],
  })
  await router.push({ name: 'workbench', params: { id: 'd1' }, query })
  await router.isReady()
  // 下载下拉（ElDropdown）在 jsdom 零布局下进 popper 量测循环报递归更新，
  // 与定位断言无关，桩掉；PdfCanvas 已有专用桩保留 pageIdx/highlights 断言。
  const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus],
    stubs: { 'el-dropdown': true, 'el-dropdown-menu': true, 'el-dropdown-item': true } } })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('WorkbenchView 外部定位', () => {
  it('evidence 参数走 evidence/resolve：按证据快照定页并画 bbox 高亮', async () => {
    vi.mocked(conversationsApi.evidence).mockResolvedValue({ data: evidenceDetail() } as never)
    const wrapper = await mountWithQuery({ evidence: 'ev-9', page: '1', chunk: 'chunk-old' })
    expect(vi.mocked(conversationsApi.evidence)).toHaveBeenCalledWith('ev-9')
    const canvas = wrapper.findComponent({ name: 'PdfCanvasStub' })
    expect(canvas.props('pageIdx')).toBe(2)
    const highlights = canvas.props('highlights') as { bbox: number[] | null; pageSize: number[] | null }[]
    expect(highlights).toHaveLength(1)
    expect(highlights[0]!.bbox).toEqual([10, 20, 30, 40])
    expect(highlights[0]!.pageSize).toEqual([595, 842])
    wrapper.unmount()
  })

  it('无 evidence 参数时 seq 优先于 chunk_id 匹配（chunk 重铸后仍能定位）', async () => {
    const wrapper = await mountWithQuery({ page: '3', chunk: 'chunk-gone', seq: '42' })
    expect(vi.mocked(conversationsApi.evidence)).not.toHaveBeenCalled()
    const canvas = wrapper.findComponent({ name: 'PdfCanvasStub' })
    // seq=42 在第 3 页（page_idx=2）找到块：选中态高亮落在该块上
    expect(canvas.props('pageIdx')).toBe(2)
    const highlights = canvas.props('highlights') as { bbox: number[] | null }[]
    expect(highlights).toHaveLength(1)
    expect(highlights[0]!.bbox).toEqual([10, 20, 30, 40])
    wrapper.unmount()
  })

  it('evidence 接口失败时回退到 chunk/page 参数，不卡死定位', async () => {
    vi.mocked(conversationsApi.evidence).mockRejectedValue(new Error('404'))
    const wrapper = await mountWithQuery({ evidence: 'ev-missing', page: '3', chunk: 'chunk-new' })
    const canvas = wrapper.findComponent({ name: 'PdfCanvasStub' })
    expect(canvas.props('pageIdx')).toBe(2)
    const highlights = canvas.props('highlights') as { bbox: number[] | null }[]
    expect(highlights).toHaveLength(1)
    wrapper.unmount()
  })
})
