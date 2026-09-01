import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const evidence = {
  id: 'e1', document: { id: 'd1', filename: 'manual.pdf' }, page_idx: 2, seq: 7,
  parse_job_id: 'job-1', doc_version: 3, bbox: [10, 20, 110, 220], page_size: [800, 1200],
  kind: 'figure', content: '图中显示 42。', source_type: 'generated', derived_from: 'source-e0',
  crop_url: '/api/evidence/e1/crop', review_state: 'unreviewed', chunk_id: 'chunk-1',
  verifications: [],
}

vi.mock('@/api', () => ({
  conversationsApi: {
    evidence: vi.fn(async () => ({ data: evidence })),
    verifyEvidence: vi.fn(async () => ({ data: { review_state: 'passed' } })),
  },
}))
vi.mock('@/utils/markdown', () => ({ fetchAuthedImage: vi.fn(async () => null) }))

import EvidencePreview from '../EvidencePreview.vue'

describe('EvidencePreview 的四层证据检查', () => {
  it('显示文档→页→块→原子，并声明原始像素和生成理解回源', async () => {
    const wrapper = mount(EvidencePreview, { props: { evidenceId: 'e1' } })
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('文档manual.pdf')
    expect(text).toContain('页第 3 页')
    expect(text).toContain('块#7')
    expect(text).toContain('原子figure · 生成理解')
    expect(text).toContain('1:1 原始像素')
    expect(text).toContain('最终依据回到源 Evidence source-e0')
    expect(wrapper.find('.crop-scroll img').exists()).toBe(false)
  })

  it('只提供通过、标疑、驳回三种人工标注，不提供编辑入口', async () => {
    const wrapper = mount(EvidencePreview, { props: { evidenceId: 'e1' } })
    await flushPromises()

    expect(wrapper.text()).toContain('只做标注，不修改内容')
    expect(wrapper.text()).not.toContain('编辑')
    const labels = wrapper.findAll('.review-actions el-button').map((button) => button.text())
    expect(labels).toEqual(['通过', '标疑', '驳回'])
  })
})
