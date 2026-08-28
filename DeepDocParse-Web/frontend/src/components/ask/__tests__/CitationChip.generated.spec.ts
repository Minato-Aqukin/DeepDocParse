import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CitationChip from '../CitationChip.vue'

describe('CitationChip 的生成物标记', () => {
  it('VLM 派生描述不能冒充原文出处', () => {
    const wrapper = mount(CitationChip, {
      props: {
        index: 1,
        citation: {
          chunk_id: 'c1', evidence_id: 'derived-e1', source_type: 'generated',
          derived_from: 'source-e1', parse_job_id: 'j1', seq: 0,
          page_idx: 0, bbox: [1, 2, 3, 4], crop_url: null,
          snippet: '延迟在 80ms 后趋稳', score: 0.03, similarity: 0.8, resolved: true,
        },
      },
    })
    expect(wrapper.text()).toContain('生成理解 → 原子出处')
    expect(wrapper.text()).toContain('第 1 页')
  })
})
