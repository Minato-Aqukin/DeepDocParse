import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const { query } = vi.hoisted(() => ({ query: vi.fn() }))
vi.mock('@/api', () => ({ searchApi: { query } }))

import SearchView from '@/views/SearchView.vue'

it('shows printed labels when available and always opens the fixed physical PDF page', async () => {
  for (const label of ['iv', undefined, null]) {
    query.mockResolvedValue({ data: { query: 'manual', degraded: null, groups: [{
      document_id: 'document-1', resource_id: 'resource-1', source_version_id: 'version-1',
      source_version_no: 2, parse_revision: 'job-1', filename: 'manual.pdf', hits: [{
        chunk_id: 'chunk-1', page_idx: 3, printed_page_label: label, bbox: null,
        score: 0.03, similarity: null, snippet: 'A matching paragraph.',
      }],
    }] } })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/search', name: 'search', component: SearchView },
      { path: '/documents/:id', name: 'workbench', component: { template: '<div />' } },
    ] })
    await router.push('/search?q=manual')
    await router.isReady()
    const wrapper = mount(SearchView, { global: { plugins: [router, ElementPlus] } })
    await flushPromises()
    expect(wrapper.get('.page.ddp-cite-page').text()).toBe(label
      ? '印刷页 iv · PDF 第 4 页' : 'PDF 第 4 页')
    await wrapper.get('a.hit').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('workbench')
    expect(router.currentRoute.value.params.id).toBe('document-1')
    expect(router.currentRoute.value.query).toEqual({
      resource_id: 'resource-1', version_id: 'version-1', job: 'job-1', chunk: 'chunk-1', page: '4',
    })
    wrapper.unmount()
  }
})
