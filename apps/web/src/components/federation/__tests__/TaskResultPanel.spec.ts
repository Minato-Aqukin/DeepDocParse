import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/components/evidence/EvidencePreview.vue', () => ({ default: { template: '<div />' } }))

import type { FederatedEvidence, TaskResult } from '@/federation/task-model'

import TaskResultPanel from '../TaskResultPanel.vue'

/**
 * Element Plus 在单测里没注册（见 `src/__tests__/setup.ts`）：这里只断言原生元素 ——
 * 引用按钮、降级提示、矛盾与未查到的目标。"查看原文出处"那种 el-button 在 e2e 里断言。
 */

function evidence(id: string, origin: string, page = 0, seq = 1): FederatedEvidence {
  return {
    evidence_id: id, origin_node_id: origin, authority_node_id: origin, resource_id: `res-${id}`,
    source_version_id: `ver-${id}`, parse_revision: 'parse-1', source_digest: 'sha256:' + 'a'.repeat(64),
    excerpt_digest: 'sha256:' + 'b'.repeat(64), locator: { kind: 'page_block', physical_page_index: page, seq },
    source_type: 'source', policy_revision: 'p-1',
  }
}

function result(overrides: Partial<TaskResult> = {}): TaskResult {
  return {
    answer: '最大输入电压一处写 240 V。[1]\n另一处写 120 V。[2]\n<img src=x onerror="alert(1)"> [9]',
    answer_reason: null,
    claim_evidence_bindings: [
      { claim_id: 'c1', claim_text: '最大输入电压一处写 240 V。', evidence_refs: ['ev-a'], structural_validation: 'passed', semantic_review: 'needs_review' },
    ],
    conflicts: [{ basis: 'version_divergence', evidence_refs: ['ev-a', 'ev-b'], semantic_review: 'needs_review' }],
    provider: { model: 'qwen3', location: 'local' },
    disclosure: { remote: false, payload: [] },
    validation_state: 'passed',
    retrieval_completeness: 'partial',
    evidence_sufficiency: 'conflicting',
    evidence: [evidence('ev-a', 'node-a', 11, 3), evidence('ev-b', 'node-b', 11, 3)],
    unretrieved_targets: [{ target_key: { origin_node_id: 'node-c', collection_id: 'c:1', operation: 'corpus.retrieve' }, state: 'unreachable', last_error: 'peer_unavailable' }],
    ...overrides,
  }
}

describe('TaskResultPanel', () => {
  it('矛盾先于答案给出，引用按钮指向证据编号；越界的 [9] 不变成按钮', () => {
    const wrapper = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    const text = wrapper.text()
    expect(text.indexOf('证据之间存在矛盾')).toBeLessThan(text.indexOf('回答'))
    expect(text).toContain('同一资料的版本不一致')
    const cites = wrapper.findAll('.answer button.cite').map((button) => button.text())
    expect(cites).toEqual(['[1]', '[2]'])
    expect(wrapper.find('.answer').text()).toContain('[9]')
  })

  it('生成文本按纯文本渲染：里面的 HTML 不会变成元素', () => {
    const wrapper = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    expect(wrapper.find('.answer img').exists()).toBe(false)
    expect(wrapper.find('.answer').text()).toContain('<img src=x onerror="alert(1)">')
  })

  it('只查了部分范围时列出没取回证据的目标与原因', () => {
    const wrapper = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    const note = wrapper.find('[aria-label="未查到的范围"]')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('node-c / c:1')
    expect(note.text()).toContain('peer_unavailable')
    const complete = mount(TaskResultPanel, { props: { result: result({ retrieval_completeness: 'complete', unretrieved_targets: [] }), coordinatorNodeId: 'node-a' } })
    expect(complete.find('[aria-label="未查到的范围"]').exists()).toBe(false)
  })

  it('证据不足时明说不下结论；没有答案时显示契约里的原因与细节', () => {
    const wrapper = mount(TaskResultPanel, { props: {
      result: result({ answer: null, answer_reason: 'delegated_execution_failed:peer_execution_timeout',
        evidence_sufficiency: 'insufficient', conflicts: [], claim_evidence_bindings: [] }),
      coordinatorNodeId: 'node-a',
    } })
    const text = wrapper.text()
    expect(text).toContain('证据不足：本次取得的原文不足以支撑结论')
    expect(text).toContain('没有生成回答：远端生成步骤未完成（peer_execution_timeout）')
  })

  it('证据标出本节点与远端；读不到计划时不假装知道来源在哪', () => {
    const known = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: 'node-a' } })
    const items = known.findAll('ol.evidence > li')
    expect(items[0]!.text()).toContain('本节点')
    expect(items[0]!.text()).toContain('第 12 页 · 块 3')
    expect(items[1]!.text()).toContain('远端 node-b')
    expect(items[1]!.text()).toContain('原文由来源节点持有')
    const unknown = mount(TaskResultPanel, { props: { result: result(), coordinatorNodeId: null } })
    expect(unknown.find('ol.evidence > li').text()).toContain('暂时判断不了这条证据是否在本节点')
  })
})
