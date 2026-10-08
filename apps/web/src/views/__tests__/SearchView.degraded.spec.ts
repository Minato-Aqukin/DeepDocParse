import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const { query } = vi.hoisted(() => ({ query: vi.fn() }))
vi.mock('@/api', () => ({ searchApi: { query } }))

import SearchView from '@/views/SearchView.vue'
import type { SearchHit } from '@/types/api'

function hit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    chunk_id: 'chunk-1', page_idx: 3, printed_page_label: null, bbox: null,
    score: 0.03, similarity: null, snippet: 'A matching paragraph.',
    ...overrides,
  }
}

function groups(degraded: string | null, hits: SearchHit[] = [hit()]) {
  return { query: 'manual', degraded, groups: [{
    document_id: 'document-1', resource_id: 'resource-1', source_version_id: 'version-1',
    source_version_no: 2, parse_revision: 'job-1', filename: 'manual.pdf', hits,
  }] }
}

async function mountWithQuery(q: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/search', name: 'search', component: SearchView },
    { path: '/documents/:id', name: 'workbench', component: { template: '<div />' } },
  ] })
  await router.push(`/search?q=${q}`)
  await router.isReady()
  const wrapper = mount(SearchView, { global: { plugins: [router, ElementPlus] } })
  await flushPromises()
  return { wrapper, router }
}

describe('SearchView 降级与稳定定位', () => {
  // 每个降级码都必须有一条可见提示（不变式 2）。具体文案来自契约生成物
  // （keyword_unavailable 由契约 enums.yaml 生成，前端只消费生成物），
  // 这里不断言文案逐字，只断言"有一条非空可见提示"。
  it.each([
    'vision_unavailable', 'upstream_error', 'keyword_unavailable',
    'no_hits', 'client_aborted', 'answer_unavailable',
  ])('降级码 %s 有可见提示，不再静默', async (code) => {
    query.mockResolvedValue({ data: groups(code) })
    const { wrapper } = await mountWithQuery('manual')
    const alerts = wrapper.findAll('.degraded')
    expect(alerts.length).toBe(1)
    expect(alerts[0]!.text().trim().length).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('无降级时不渲染任何降级提示', async () => {
    query.mockResolvedValue({ data: groups(null) })
    const { wrapper } = await mountWithQuery('manual')
    expect(wrapper.find('.degraded').exists()).toBe(false)
    wrapper.unmount()
  })

  it('命中携带稳定定位键时导航 query 带上 seq/parse_job/evidence/page_size', async () => {
    query.mockResolvedValue({ data: groups(null, [hit({
      chunk_id: 'chunk-9', page_idx: 5, seq: 42, parse_job_id: 'job-9',
      evidence_id: 'ev-9', page_size: [595, 842],
    })]) })
    const { wrapper, router } = await mountWithQuery('manual')
    await wrapper.get('a.hit').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({
      chunk: 'chunk-9', page: '6', seq: '42', parse_job: 'job-9',
      evidence: 'ev-9', page_size: '595x842',
    })
    wrapper.unmount()
  })

  it('旧后端缺失定位字段时导航回退到 chunk/page，不带空键', async () => {
    query.mockResolvedValue({ data: groups(null) })
    const { wrapper, router } = await mountWithQuery('manual')
    await wrapper.get('a.hit').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({
      resource_id: 'resource-1', version_id: 'version-1', job: 'job-1',
      chunk: 'chunk-1', page: '4',
    })
    wrapper.unmount()
  })
})
