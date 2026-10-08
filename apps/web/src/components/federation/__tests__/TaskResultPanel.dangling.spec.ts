import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/components/evidence/EvidencePreview.vue', () => ({ default: { template: '<div />' } }))

import type { FederatedEvidence, TaskResult } from '@/federation/task-model'

import TaskResultPanel from '../TaskResultPanel.vue'

function evidence(id: string): FederatedEvidence {
  return {
    evidence_id: id, origin_node_id: 'node-a', authority_node_id: 'node-a',
    resource_id: `res-${id}`, source_version_id: `ver-${id}`, parse_revision: 'parse-1',
    source_digest: 'sha256:' + 'a'.repeat(64), excerpt_digest: 'sha256:' + 'b'.repeat(64),
    locator: { kind: 'page_block', physical_page_index: 0, seq: 1 },
    source_type: 'source', policy_revision: 'p-1',
  }
}

function result(): TaskResult {
  return {
    // [9] 越界（只有 2 条证据），[0] 非法编号
    answer: '电压一处写 240 V。[1]\n另一处写 120 V。[9]\n还有一行写 100 V。[0]',
    answer_reason: null,
    // c-good 引用 e-gone：evidence 里没有
    claim_evidence_bindings: [
      { claim_id: 'c1', claim_text: '电压 240 V。', evidence_refs: ['ev-a', 'ev-gone'],
        structural_validation: 'passed', semantic_review: 'needs_review' },
    ],
    conflicts: [{ basis: 'version_divergence', evidence_refs: ['ev-a', 'ev-phantom'],
      semantic_review: 'needs_review' }],
    provider: { model: 'qwen3', location: 'local' },
    disclosure: { remote: false, payload: [] },
    validation_state: 'passed',
    retrieval_completeness: 'complete',
    evidence_sufficiency: 'conflicting',
    evidence: [evidence('ev-a'), evidence('ev-b')],
    unretrieved_targets: [],
  }
}

describe('TaskResultPanel 掉队引用警告', () => {
  it('越界 [9]/[0] 不再是无声纯文本：红字标出 + 整段引用缺失警告', () => {
    const wrapper = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    const dangling = wrapper.findAll('.answer .dangling').map((s) => s.text())
    expect(dangling).toEqual(['[9]', '[0]'])
    // 合法引用仍是按钮
    expect(wrapper.findAll('.answer button.cite').map((b) => b.text())).toEqual(['[1]'])
    expect(wrapper.find('.answer').text()).toContain('引用缺失')
    expect(wrapper.find('.answer').text()).toContain('[9]')
  })

  it('冲突行与主张行掉队的 evidence_ref 旁边打引用缺失 tag', () => {
    const wrapper = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    const text = wrapper.text()
    // 冲突行的 ev-phantom、主张行的 ev-gone 都 points nowhere
    expect(text).toContain('ev-phantom')
    expect(text).toContain('ev-gone')
    expect(wrapper.findAll('.ddp-status').filter((s) => s.text().includes('引用缺失')).length)
      .toBeGreaterThanOrEqual(2)
  })

  it('引用全部有效时不出现引用缺失警告', () => {
    const clean: TaskResult = {
      ...result(), answer: '电压 240 V。[1]',
      claim_evidence_bindings: [],
      conflicts: [{ basis: 'version_divergence', evidence_refs: ['ev-a', 'ev-b'],
        semantic_review: 'needs_review' }],
    }
    const wrapper = mount(TaskResultPanel, { props: { result: clean, coordinatorNodeId: 'node-a' } })
    expect(wrapper.find('.answer .dangling').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('引用缺失')
  })
})
