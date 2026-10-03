import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

const { graph, evidence } = vi.hoisted(() => ({ graph: vi.fn(), evidence: vi.fn() }))
vi.mock('@/api', () => ({ knowledgeApi: { graph }, conversationsApi: { evidence } }))

import GraphView from '@/views/GraphView.vue'

it('shows printed labels on edge citations while evidence opens its fixed physical page', async () => {
  for (const label of ['iv', undefined, null]) {
    graph.mockResolvedValue({ data: { graph_version: 'ddp-graph/1', entities: [
      { id: 'subject', canonical_name: 'System', entity_type: 'system', aliases: [] },
      { id: 'object', canonical_name: 'Model', entity_type: 'model', aliases: [] },
    ], edges: [{ id: 'edge-1', subject_id: 'subject', object_id: 'object', predicate: 'uses',
      confidence: 0.9, evidence_ids: ['evidence-1'], unsupported: false, citations: [{
        evidence_id: 'evidence-1', page_idx: 3, printed_page_label: label,
        resolved: true, snippet: 'The system uses the model.',
      }],
    }] } })
    evidence.mockResolvedValue({ data: {
      id: 'evidence-1', resource_id: 'resource-1', source_version_id: 'version-1',
      document: { id: 'document-1', filename: 'manual.pdf' }, page_idx: 3,
      printed_page_label: label, seq: 7, parse_job_id: 'job-1', doc_version: 1,
      bbox: [10, 20, 110, 220], page_size: [800, 1200], kind: 'text',
      content: 'The system uses the model.', source_type: 'source', derived_from: null,
      crop_url: null, review_state: 'unreviewed', chunk_id: 'chunk-1', verifications: [],
    } })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/graph', component: GraphView },
      { path: '/documents/:id', component: { template: '<div />' } },
    ] })
    await router.push('/graph')
    await router.isReady()
    const wrapper = mount(GraphView, { global: {
      plugins: [router, ElementPlus],
      stubs: { GraphCanvas: true, ReviewQueue: true, ElSelect: true, ElSlider: true },
    } })
    await flushPromises()
    await wrapper.get('.edge-picker select').setValue('edge-1')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '关闭证据')!.trigger('click')
    expect(wrapper.get('button.citation').text()).toBe(label
      ? '印刷页 iv · PDF 第 4 页 · The system uses the model.'
      : '第 4 页 · The system uses the model.')
    await wrapper.get('button.citation').trigger('click')
    await flushPromises()
    const source = wrapper.findAll('a').find(link => link.text().includes('打开固定版本原文'))!
    await source.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.params.id).toBe('document-1')
    expect(router.currentRoute.value.query).toEqual({
      resource_id: 'resource-1', version_id: 'version-1', job: 'job-1', page: '4', chunk: 'chunk-1',
    })
    wrapper.unmount()
  }
})
