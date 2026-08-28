import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const message = {
  id: 'm-agent', role: 'assistant', content: '有出处的结论。没有依据的猜测。',
  verified: false, degraded: null, created_at: new Date().toISOString(),
  citations: [],
  assertions: [
    {
      id: 'a1', position: 0, text: '有出处的结论。', evidence_ids: ['e1'], unsupported: false,
      verification: { state: 'passed', mode: 'auto' },
      citations: [{
        evidence_id: 'e1', source_type: 'source', chunk_id: 'ch1', parse_job_id: 'j1',
        seq: 1, page_idx: 0, bbox: [1, 2, 3, 4], crop_url: null, snippet: '原文',
        score: 0.02, similarity: 0.9, resolved: true,
      }],
    },
    {
      id: 'a2', position: 1, text: '没有依据的猜测。', evidence_ids: [], unsupported: true,
      verification: { state: 'unverified', mode: null }, citations: [],
    },
  ],
  query_decision: {
    need_retrieval: true, reason: '问题引入新事实', inherited_evidence_ids: [], degraded: null,
  },
  retrieval: { candidates: [
    {
      evidence_id: 'e1', document_id: 'd1', chunk_id: 'ch1', rank: 1,
      score: 0.02, similarity: 0.9, accepted: true, reason: 'top_candidate',
    },
    {
      evidence_id: 'e2', document_id: 'd1', chunk_id: 'ch2', rank: 2,
      score: 0.01, similarity: 0.2, accepted: false, reason: 'below_similarity_gate',
    },
  ] },
}

vi.mock('@/api', () => ({
  askStream: vi.fn(() => () => {}),
  conversationsApi: {
    list: vi.fn(async () => ({ data: [{ id: 'c1', title: '会话' }] })),
    messages: vi.fn(async () => ({ data: [message] })),
    create: vi.fn(), remove: vi.fn(),
  },
}))

vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(async () => null),
  renderMarkdown: (value: string) => value,
  resolveAuthedImages: async () => () => {},
}))

import AskPanel from '../AskPanel.vue'

const document = {
  id: 'd1', filename: 'manual.pdf', index_status: 'ready', index_error: null,
} as never

describe('AskPanel 的断言与门控轨迹', () => {
  it('逐条展示断言出处，并把无出处断言标成 unsupported', async () => {
    const wrapper = mount(AskPanel, { props: { document } })
    await flushPromises()

    expect(wrapper.findAll('.assertion')).toHaveLength(2)
    expect(wrapper.text()).toContain('有出处的结论。')
    expect(wrapper.text()).toContain('自动核对通过')
    expect(wrapper.text()).toContain('没有依据的猜测。')
    expect(wrapper.text()).toContain('无证据支持')
    expect(wrapper.findAllComponents({ name: 'CitationChip' })).toHaveLength(1)
  })

  it('把检索决策和被拒候选的原因留给用户复核', async () => {
    const wrapper = mount(AskPanel, { props: { document } })
    await flushPromises()

    expect(wrapper.text()).toContain('本轮执行检索')
    expect(wrapper.text()).toContain('问题引入新事实')
    expect(wrapper.text()).toContain('1 条通过 · 1 条拒绝')
    expect(wrapper.text()).toContain('below_similarity_gate')
  })
})
