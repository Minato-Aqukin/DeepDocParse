import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { EvidenceDetail } from '@/types/api'
import type { ResourceContext } from '@/api/resource-context'

interface EvidenceResponse { data: EvidenceDetail }

const { loadEvidence, verifyEvidence } = vi.hoisted(() => ({
  loadEvidence: vi.fn<(id: string, context?: ResourceContext) => Promise<EvidenceResponse>>(),
  verifyEvidence: vi.fn(),
}))
vi.mock('@/api', () => ({
  conversationsApi: { evidence: loadEvidence, verifyEvidence },
}))
vi.mock('@/utils/markdown', () => ({ fetchAuthedImage: vi.fn(async () => null) }))

import EvidencePreview from '../EvidencePreview.vue'
import { bootSource } from '@/platform/desktop'

function evidence(resourceId: string, filename: string): EvidenceDetail {
  return {
    id: 'shared-evidence', resource_id: resourceId, source_version_id: `${resourceId}-v1`,
    document: { id: 'shared-document', filename }, page_idx: 2, seq: 7,
    parse_job_id: 'job-1', doc_version: 1, bbox: [10, 20, 110, 220], page_size: [800, 1200],
    kind: 'text', content: 'Original source content.', source_type: 'source', derived_from: null,
    crop_url: null, review_state: 'unreviewed', chunk_id: 'chunk-1', verifications: [],
  }
}

describe('EvidencePreview fixed source context', () => {
  it('does not replace the selected source with a late response for another resource', async () => {
    const previous = Promise.withResolvers<EvidenceResponse>()
    loadEvidence.mockImplementation((_id, context) => context?.resource_id === 'old'
      ? previous.promise
      : Promise.resolve({ data: evidence('new', 'selected-version.pdf') }))
    const wrapper = mount(EvidencePreview, {
      props: { evidenceId: 'shared-evidence', context: { resource_id: 'old', version_id: 'old-v1' } },
    })
    await wrapper.setProps({ context: { resource_id: 'new', version_id: 'new-v1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('selected-version.pdf')
    previous.resolve({ data: evidence('old', 'previous-version.pdf') })
    await flushPromises()
    expect(wrapper.text()).toContain('selected-version.pdf')
    expect(wrapper.text()).not.toContain('previous-version.pdf')
    wrapper.unmount()
  })
})

describe('EvidencePreview locator gaps are explicit', () => {
  // el-empty 在单测里没注册（见 src/__tests__/setup.ts），description 以属性留在 DOM 上
  it('no bbox: says page-only and never claims a whole-page box exists', async () => {
    loadEvidence.mockResolvedValue({ data: { ...evidence('r', 'manual.pdf'), bbox: null } })
    const wrapper = mount(EvidencePreview, { props: { evidenceId: 'shared-evidence' } })
    await flushPromises()
    const empty = wrapper.find('el-empty').attributes('description') ?? ''
    expect(empty).toContain('没有区域坐标')
    expect(empty).toContain('第 3 页')
    expect(empty).not.toContain('整页 bbox')
    wrapper.unmount()
  })

  it('evidence no longer in the current index is labelled historical, current one is not', async () => {
    loadEvidence.mockResolvedValue({ data: { ...evidence('r', 'manual.pdf'), chunk_id: null } })
    const historical = mount(EvidencePreview, { props: { evidenceId: 'shared-evidence' } })
    await flushPromises()
    expect(historical.find('.locator-note').text()).toContain('不在当前索引里')
    historical.unmount()

    loadEvidence.mockResolvedValue({ data: evidence('r', 'manual.pdf') })
    const current = mount(EvidencePreview, { props: { evidenceId: 'shared-evidence' } })
    await flushPromises()
    expect(current.find('.locator-note').exists()).toBe(false)
    current.unmount()
  })
})

describe('EvidencePreview 中心只读', () => {
  it('桌面中心源下人工核对（POST）禁用并写明原因，点击不提交', async () => {
    Object.defineProperty(window, 'ddpDesktop', { value: {}, configurable: true, writable: true })
    bootSource.value = { sourceId: 'center-0', kind: 'center', label: '研究中心', state: 'ready', readOnly: true,
      features: [], active: true, reason: null } as never
    try {
      loadEvidence.mockResolvedValue({ data: evidence('r', 'manual.pdf') })
      const wrapper = mount(EvidencePreview, { props: { evidenceId: 'shared-evidence' } })
      await flushPromises()
      const pass = wrapper.findAll('el-button').find((b) => b.text() === '通过')!
      // Unregistered el-button renders its props as attributes: disabled="true" / "false".
      expect(pass.attributes('disabled')).toBe('true')
      expect(wrapper.text()).toContain('中心在桌面里只读；写操作请作为联邦任务发起并批准')
      await pass.trigger('click')
      await flushPromises()
      expect(verifyEvidence).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      bootSource.value = null
      Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
    }
  })
})
