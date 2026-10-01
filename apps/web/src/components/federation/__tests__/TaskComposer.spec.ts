import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  identity: vi.fn(), createScope: vi.fn(), generationCandidates: vi.fn(),
  createIntent: vi.fn(), plan: vi.fn(),
}))
vi.mock('@/api/tasks', () => ({ tasksApi: api }))
vi.mock('@/api/resources', () => ({ resourcesApi: { list: vi.fn().mockResolvedValue({ data: { items: [] } }) } }))

import TaskComposer from '@/components/federation/TaskComposer.vue'

const now = Date.now()
const future = new Date(now + 3600000).toISOString()
const expired = new Date(now - 3600000).toISOString()
const candidate = {
  node_id: 'node-c', state: 'approved', descriptor_valid_until: future,
  operation: 'rag.answer.cited', readiness: 'ready', accepting_admissions: true,
  observed_at: new Date(now - 1000).toISOString(), valid_until: future,
}
const scope = {
  manifest: {
    schema: 'ddp-scope-coverage/1#ScopeManifest', scope_id: 'scope-1',
    caller_scope_hash: 'sha256:' + 'a'.repeat(64), created_at: new Date(now).toISOString(), valid_until: future,
    registry_revision_vector: ['node-b', 'node-c', 'node-unknown', 'node-expired', 'node-pending'].map(node_id => ({ node_id, registry_revision: 1, fetched_at: new Date(now).toISOString() })),
    expanded_members: [{ origin_node_id: 'node-b', collection_id: 'col-b', operation: 'corpus.retrieve' }],
    unexpanded_subtrees: [], enumeration_state: 'sealed', manifest_digest: 'sha256:' + 'b'.repeat(64),
  },
  first_cursor: 'first', terminal_cursor: 'last', total_targets: 1,
  content_snapshot: 'not_frozen', expired: false, effective_enumeration_state: 'sealed',
}

beforeEach(() => {
  vi.clearAllMocks()
  api.identity.mockResolvedValue({ data: { identity: { authority_node_id: 'node-a', workspace_id: 'org-a' }, profile: { subject: 'user-a' } } })
  api.createScope.mockResolvedValue({ data: scope })
  api.generationCandidates.mockImplementation(async (operation: string) => ({ data: { items: [
    { ...candidate, operation },
    { ...candidate, node_id: 'node-unknown', operation, readiness: 'unknown', accepting_admissions: false },
    { ...candidate, node_id: 'node-expired', operation, descriptor_valid_until: expired },
    { ...candidate, node_id: 'node-pending', operation, state: 'pending' },
  ] } }))
  api.createIntent.mockResolvedValue({ data: { root_task_id: 'task-abc' } })
  api.plan.mockResolvedValue({ data: {} })
})

function composer() {
  return mount(TaskComposer, { props: { initialQuery: '额定电压是多少？' }, global: { plugins: [ElementPlus], stubs: { ScopeTargets: true } } })
}
async function radio(wrapper: VueWrapper, text: string) {
  await wrapper.findAll('label.el-radio').find(item => item.text() === text)!.find('input').setValue(true)
  await flushPromises()
}
async function seal(wrapper: VueWrapper) {
  await radio(wrapper, '联邦公开范围')
  await wrapper.findAll('button').find(item => item.text() === '生成范围清单')!.trigger('click')
  await flushPromises()
}
async function checkbox(wrapper: VueWrapper, node: string) {
  const label = wrapper.findAll('label.el-checkbox').find(item => item.text().includes(node))!
  await label.find('input[type="checkbox"]').setValue(true)
}

describe('TaskComposer approved compute recipients and Wiki', () => {
  it('lists fresh approved compute-only C separately, disables unknown, and never auto-consents recipients', async () => {
    const wrapper = composer()
    await flushPromises()
    await seal(wrapper)
    expect(wrapper.text()).toContain('仅算力节点')
    expect(wrapper.text()).not.toContain('node-expired')
    expect(wrapper.text()).not.toContain('node-pending')
    const unknown = wrapper.findAll('label.el-checkbox').find(item => item.text().includes('node-unknown'))!
    expect(unknown.find('input').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('input[type="checkbox"]').filter(item => (item.element as HTMLInputElement).checked).map(item => item.element.getAttribute('value'))).not.toContain('node-c')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.createIntent.mock.calls[0]![0].exploration_consent.allowed_recipients).toEqual([])
    wrapper.unmount()
  })

  it.each(['rag.answer.cited', 'wiki.pages'])('user-selected B+C consent supports %s through create and plan', async operation => {
    const wrapper = composer()
    await flushPromises()
    if (operation === 'wiki.pages') {
      await radio(wrapper, '构建 Wiki 草稿')
      await wrapper.find('input[placeholder="用固定证据解释什么？"]').setValue('设备参数')
    }
    await seal(wrapper)
    await checkbox(wrapper, 'node-b')
    await checkbox(wrapper, 'node-c')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const body = api.createIntent.mock.calls[0]![0]
    expect(body.exploration_consent.allowed_recipients).toEqual(['node-b', 'node-c'])
    expect(body.task_spec.operation).toBe(operation)
    if (operation === 'wiki.pages') expect(body.task_spec.requirements.wiki).toEqual({ title: '设备参数', max_pages: 4 })
    expect(api.plan).toHaveBeenCalledWith('task-abc')
    expect(wrapper.emitted('created')).toEqual([['task-abc']])
    wrapper.unmount()
  })

  it('failed capability refresh removes hidden compute consent but preserves selected data recipients', async () => {
    const wrapper = composer()
    await flushPromises()
    await seal(wrapper)
    await checkbox(wrapper, 'node-b')
    await checkbox(wrapper, 'node-c')
    api.generationCandidates.mockRejectedValueOnce(new Error('目录离线'))
    await wrapper.findAll('button').find(item => item.text() === '刷新生成能力')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('目录离线')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.createIntent.mock.calls[0]?.[0].exploration_consent.allowed_recipients).toEqual(['node-b'])
    wrapper.unmount()
  })

  it('discovery failure remains unknown, not an empty successful directory', async () => {
    api.generationCandidates.mockRejectedValueOnce(new Error('观测失败'))
    const wrapper = composer()
    await flushPromises()
    await seal(wrapper)
    expect(wrapper.text()).toContain('观测失败')
    expect(wrapper.text()).not.toContain('本次范围内没有描述新鲜')
    wrapper.unmount()
  })

  it('returning to federation scope requires a new manifest rather than showing cleared observations as empty', async () => {
    const wrapper = composer()
    await flushPromises()
    await seal(wrapper)
    await radio(wrapper, '本站公开')
    await radio(wrapper, '联邦公开范围')
    expect(wrapper.text()).toContain('穷查要先把范围枚举并封存下来')
    expect(wrapper.text()).not.toContain('本次范围内没有描述新鲜')
    await seal(wrapper)
    expect(api.generationCandidates).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('label.el-checkbox').some(item => item.text().includes('node-c'))).toBe(true)
    wrapper.unmount()
  })

  it('a ready claim with a future observation is disabled with its clock mismatch reason', async () => {
    api.generationCandidates.mockResolvedValueOnce({ data: { items: [{ ...candidate, observed_at: future }] } })
    const wrapper = composer()
    await flushPromises()
    await seal(wrapper)
    const compute = wrapper.findAll('label.el-checkbox').find(item => item.text().includes('node-c'))!
    expect(compute.find('input').attributes('disabled')).toBeDefined()
    expect(compute.text()).toContain('观测时间超前')
    wrapper.unmount()
  })
})
