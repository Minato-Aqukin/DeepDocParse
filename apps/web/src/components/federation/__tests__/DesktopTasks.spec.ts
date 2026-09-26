import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'

import ElementPlus from 'element-plus'
import { resourcesApi } from '@/api/resources'
import { bootSource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'
import type { Profile } from '@/types/api'
import type { Router } from 'vue-router'

function setDesktop(bridge: unknown) {
  Object.defineProperty(window, 'ddpDesktop', { value: bridge, configurable: true, writable: true })
}

function clearDesktop() {
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
}

const admin: Profile = {
  id: 'u', username: 'owner', email: null, role: 'admin',
  organization_id: 'o', created_at: '',
}

function localSource() {
  return {
    sourceId: 'local-0', kind: 'local', label: '本机工作区', state: 'ready',
    readOnly: false, features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'],
    active: true, reason: null,
  } as never
}

function centerSource() {
  return {
    sourceId: 'center-0', kind: 'center', label: '研究中心', state: 'ready',
    readOnly: true, features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'],
    active: true, reason: null,
  } as never
}

function stubBridge(overrides: Record<string, unknown> = {}) {
  return {
    sourceList: vi.fn(async () => ({ ok: true, value: [] })),
    sourceActivate: vi.fn(async () => ({ ok: true, value: {} })),
    clientReadDraft: vi.fn(async () => ({ ok: true, value: null })),
    clientSaveDraft: vi.fn(async () => ({ ok: true, value: { revision: 1 } })),
    clientQuery: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    clientPlanPropose: vi.fn(async () => ({ ok: true, value: { plan_id: 'plan-1' } })),
    clientPlanProposeFile: vi.fn(async () => ({ ok: true, value: { plan_id: 'plan-9' } })),
    clientPlanList: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    clientPlanGet: vi.fn(async () => ({ ok: true, value: null })),
    clientPlanApprove: vi.fn(async () => ({ ok: true, value: {} })),
    clientPlanReviewCenter: vi.fn(async () => ({ ok: true, value: { plan_id: 'plan-2' } })),
    clientPlanRevoke: vi.fn(async () => ({ ok: true, value: {} })),
    clientPlanCancel: vi.fn(async () => ({ ok: true, value: {} })),
    clientPlanDispatch: vi.fn(async () => ({ ok: true, value: {} })),
    clientPlanResume: vi.fn(async () => ({ ok: true, value: {} })),
    clientPlanReconcile: vi.fn(async () => ({ ok: true, value: null })),
    clientPlanFetchDelivery: vi.fn(async () => ({ ok: true, value: null })),
    clientPlanConfirmDelivery: vi.fn(async () => ({ ok: true, value: {} })),
    clientReceipt: vi.fn(async () => ({ ok: true, value: null })),
    ...overrides,
  }
}

// 本文件断言 el-button 的渲染结果（disabled/点击）：按 `src/__tests__/setup.ts`
// 的约定在这里局部注册 ElementPlus，不动全局配置。
function plugins(router: Router) {
  return { plugins: [router, ElementPlus] }
}

function makeRouter(path = '/') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/tasks', name: 'federation-tasks', component: { template: '<div />' } },
      { path: '/tasks/new', name: 'federation-task-new', component: { template: '<div />' } },
      { path: '/sources', name: 'sources', component: { template: '<div />' } },
      { path: '/tasks/local/:planId', name: 'federation-task-local', component: { template: '<div />' } },
      { path: '/wiki', name: 'wiki', component: { template: '<div />' } },
    ],
  })
  void router.push(path)
  return router
}

import TaskPrepareView from '@/views/TaskPrepareView.vue'
import TasksView from '@/views/TasksView.vue'
import CenterProposeSwitch from '@/components/federation/CenterProposeSwitch.vue'
import LocalTaskDetail from '@/components/federation/LocalTaskDetail.vue'
import LocalTaskPrepare from '@/components/federation/LocalTaskPrepare.vue'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.stubGlobal('crypto', { randomUUID: () => 'test-key-0001' })
  clearDesktop()
  bootSource.value = null
  sessionStorage.clear()
  setActivePinia(createPinia())
  localStorage.clear()
  vi.spyOn(resourcesApi, 'list').mockResolvedValue({ data: { items: [], has_more: false } } as never)
})

describe('/tasks/new 的 query 预填', () => {
  it('本机源下 query/purpose/title 预填进准备表单，草稿随后才可覆盖', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [
        { sourceId: 'center-0', kind: 'center', state: 'ready', label: '研究中心' },
      ] })),
      clientQuery: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/new?query=' + encodeURIComponent('控制器工作温度是多少？') + '&purpose=wiki&title=' + encodeURIComponent('控制器说明'))
    await router.isReady()
    const wrapper = mount(LocalTaskPrepare, { global: plugins(router) })
    await flushPromises()
    expect((wrapper.find('#task-query').element as HTMLTextAreaElement).value).toBe('控制器工作温度是多少？')
    expect((wrapper.find('#task-purpose').element as HTMLSelectElement).value).toBe('wiki')
    expect((wrapper.find('#task-wiki-title').element as HTMLTextAreaElement).value).toBe('控制器说明')
    wrapper.unmount()
  })

  it('切源暂存优先于 URL 参数：只认当前本机源的，用后即删', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [] })),
      clientQuery: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    sessionStorage.setItem('ddp.task-prefill', JSON.stringify({
      sourceId: 'local-0', query: '暂存的问题', purpose: 'wiki', title: '暂存标题',
    }))
    const router = makeRouter('/tasks/new?query=' + encodeURIComponent('URL 的问题'))
    await router.isReady()
    const wrapper = mount(LocalTaskPrepare, { global: plugins(router) })
    await flushPromises()
    expect((wrapper.find('#task-query').element as HTMLTextAreaElement).value).toBe('暂存的问题')
    expect((wrapper.find('#task-wiki-title').element as HTMLTextAreaElement).value).toBe('暂存标题')
    expect(sessionStorage.getItem('ddp.task-prefill')).toBeNull()
    wrapper.unmount()
  })

  it('他源的暂存不占用当前源：URL 参数照常用', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [] })),
      clientQuery: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    sessionStorage.setItem('ddp.task-prefill', JSON.stringify({ sourceId: 'local-9', query: '别源的问题' }))
    const router = makeRouter('/tasks/new?query=' + encodeURIComponent('本源的问题'))
    await router.isReady()
    const wrapper = mount(LocalTaskPrepare, { global: plugins(router) })
    await flushPromises()
    expect((wrapper.find('#task-query').element as HTMLTextAreaElement).value).toBe('本源的问题')
    wrapper.unmount()
  })
})

describe('批准走原生对话框', () => {
  const detailOf = (plan: Record<string, unknown>, federation: Record<string, unknown> | null = null) => ({
    plan, federation,
    verification: { state: 'unavailable', expected: null, actual: null },
  })

  const readyPlan = () => detailOf({
    plan_id: 'plan-1', scope_digest: 'sha256:' + 'd'.repeat(64), planning_state: 'ready',
    revoked: false, consents: {},
    scope: {
      task_spec: { query: '控制器工作温度是多少？' }, retention: 'temporary',
      output_locations: ['local:workspace-0'], input_manifest: [], payload_bindings: [],
      transport_bindings: [],
      plan: { plan_digest: 'sha256:' + 'c'.repeat(64), steps: [], data_edges: [],
        budget: { max_requests: 4, max_bytes: 4096, max_generation_tokens: 0, max_hops: 1,
          deadline: '2030-01-01T00:00:00Z' } },
      exploration: {},
    },
  } as never)

  it('批准带 userConfirmed:true 与计划当前 scopeDigest', async () => {
    const bridge = stubBridge({
      clientPlanGet: vi.fn(async () => ({ ok: true, value: readyPlan() })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/local/plan-1')
    await router.isReady()
    const wrapper = mount(LocalTaskDetail, { global: plugins(router) })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '批准探索…')!.trigger('click')
    await flushPromises()
    expect(bridge.clientPlanApprove).toHaveBeenCalledTimes(1)
    expect(bridge.clientPlanApprove).toHaveBeenCalledWith({
      connectionId: 'local-0', planId: 'plan-1', phase: 'exploration',
      scopeDigest: 'sha256:' + 'd'.repeat(64), userConfirmed: true,
      idempotencyKey: expect.any(String),
    })
    wrapper.unmount()
  })

  it('宿主报告 approval_cancelled：显示已取消批准，不授予许可', async () => {
    const bridge = stubBridge({
      clientPlanGet: vi.fn(async () => ({ ok: true, value: readyPlan() })),
      clientPlanApprove: vi.fn(async () => ({ ok: false, error: { code: 'approval_cancelled' } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/local/plan-1')
    await router.isReady()
    const wrapper = mount(LocalTaskDetail, { global: plugins(router) })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '批准探索…')!.trigger('click')
    await flushPromises()
    await flushPromises()
    expect(bridge.clientPlanApprove).toHaveBeenCalledTimes(1)
    expect(wrapper.find('p.error').text()).toContain('已取消批准，没有授予任何外发许可。')
    wrapper.unmount()
  })

  it('宿主报告 plan_changed：显示计划已变更需重新批准', async () => {
    const bridge = stubBridge({
      clientPlanGet: vi.fn(async () => ({ ok: true, value: readyPlan() })),
      clientPlanApprove: vi.fn(async () => ({ ok: false, error: { code: 'plan_changed' } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/local/plan-1')
    await router.isReady()
    const wrapper = mount(LocalTaskDetail, { global: plugins(router) })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '批准探索…')!.trigger('click')
    await flushPromises()
    await flushPromises()
    expect(wrapper.find('p.error').text()).toContain('执行计划已变更，需重新批准')
    wrapper.unmount()
  })
})

describe('确认交付只认本地校验', () => {
  const detailOf = (verified: string, deliveryState = 'pending') => ({
    plan: {
      plan_id: 'plan-1', scope_digest: 'sha256:' + 'd'.repeat(64), planning_state: 'approved',
      revoked: false, consents: { exploration: {}, execution: {} },
      scope: { task_spec: { query: 'q' }, retention: 'temporary', input_manifest: [], payload_bindings: [],
        transport_bindings: [], plan: null, exploration: {} },
    },
    federation: { state: 'succeeded', root_task_id: 'root-1',
      delivery: { id: 'delivery-1', state: deliveryState, result_manifest_digest: 'sha256:' + 'f'.repeat(64),
        result: { answer: '控制器工作温度为 40°C。' } } },
    verification: { state: verified, expected: 'sha256:' + 'f'.repeat(64), actual: 'sha256:' + 'f'.repeat(64) },
  })

  it('校验没通过时确认按钮禁用', async () => {
    const bridge = stubBridge({
      clientPlanGet: vi.fn(async () => ({ ok: true, value: detailOf('failed') })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/local/plan-1')
    await router.isReady()
    const wrapper = mount(LocalTaskDetail, { global: plugins(router) })
    await flushPromises()
    const confirm = wrapper.findAll('button').find((b) => b.text() === '确认交付')!
    expect((confirm.element as HTMLButtonElement).disabled).toBe(true)
    expect(bridge.clientPlanConfirmDelivery).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('校验通过且交付待确认时按钮可用', async () => {
    const bridge = stubBridge({
      clientPlanGet: vi.fn(async () => ({ ok: true, value: detailOf('passed') })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/local/plan-1')
    await router.isReady()
    const wrapper = mount(LocalTaskDetail, { global: plugins(router) })
    await flushPromises()
    const confirm = wrapper.findAll('button').find((b) => b.text() === '确认交付')!
    expect((confirm.element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })
})

describe('中心源只读', () => {
  it('/tasks 中心源：列表仍可读，新建禁用并写明原因，切换入口在', async () => {
    setDesktop(stubBridge())
    bootSource.value = centerSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks')
    await router.isReady()
    const wrapper = mount(TasksView, { global: plugins(router) })
    await flushPromises()
    expect(wrapper.text()).toContain('中心在桌面里只读')
    const disabled = wrapper.findAll('button').find((b) => b.text() === '新建任务')!
    expect((disabled.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.findComponent(CenterProposeSwitch).exists()).toBe(true)
    wrapper.unmount()
  })

  it('/tasks/new 中心源：不再循环回只读页，给出本机工作区切换', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [
        { sourceId: 'local-0', kind: 'local', state: 'ready', label: '本机工作区' },
        { sourceId: 'center-0', kind: 'center', state: 'ready', label: '研究中心' },
      ] })),
    })
    setDesktop(bridge)
    bootSource.value = centerSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/new?query=' + encodeURIComponent('中心看到的问题'))
    await router.isReady()
    const wrapper = mount(TaskPrepareView, { global: plugins(router) })
    await flushPromises()
    expect(wrapper.findComponent(CenterProposeSwitch).exists()).toBe(true)
    expect(wrapper.text()).toContain('联邦任务只在本机账本准备与批准')
    expect(wrapper.text()).not.toContain('问题已带过去')
    // 只列本机工作区：中心源本身不是切换目标。
    expect(wrapper.text()).toContain('本机工作区')
    expect(wrapper.text()).not.toContain('研究中心')
    wrapper.unmount()
  })

  it('没有本机源时指到 /sources', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [] })),
    })
    setDesktop(bridge)
    bootSource.value = centerSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/new')
    await router.isReady()
    const wrapper = mount(CenterProposeSwitch, {
      props: { query: 'q' },
      global: plugins(router),
    })
    await flushPromises()
    expect(wrapper.text()).toContain('去数据源页打开本机工作区')
    wrapper.unmount()
  })
})

describe('文件任务走 clientPlanProposeFile', () => {
  it('选一个已就绪版本提交：文件名取存量，inputs 恰好一项', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [
        { sourceId: 'center-0', kind: 'center', state: 'ready', label: '研究中心' },
      ] })),
      clientQuery: vi.fn(async () => { throw new Error('inputs come from /api/resources, not bridge projections') }),
    })
    // Lockable inputs are the local source's ready versions from /api/resources.
    vi.spyOn(resourcesApi, 'list').mockResolvedValue({ data: { has_more: false, items: [{
      id: 'resource-0', display_name: '甲的技术手册', versions: [{ id: 'version-0', version_no: 1, filename: '甲的技术手册.pdf',
        source_digest: 'a'.repeat(64), size_bytes: 2048, parse_status: 'succeeded', index_status: 'ready' }],
    }] } } as never)
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/new')
    await router.isReady()
    const wrapper = mount(LocalTaskPrepare, { global: plugins(router) })
    await flushPromises()
    await wrapper.find('#task-operation').setValue('file')
    await wrapper.find('#task-center').setValue('center-0')
    // Same-named files in different resources stay distinguishable: resource + version + filename.
    expect(wrapper.find('#task-file').text()).toContain('甲的技术手册 · v1 · 甲的技术手册.pdf')
    await wrapper.find('#task-file').setValue('version-0')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(bridge.clientPlanProposeFile).toHaveBeenCalledTimes(1)
    expect(bridge.clientPlanProposeFile).toHaveBeenCalledWith({
      connectionId: 'local-0', centerConnectionId: 'center-0',
      filename: '甲的技术手册.pdf',
      inputs: [{ ref: 'version-0', digest: 'sha256:' + 'a'.repeat(64), sizeBytes: 2048 }],
      retention: 'temporary', validMinutes: 120,
      idempotencyKey: expect.any(String),
    })
    expect(bridge.clientPlanPropose).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('没选版本时提交按钮禁用，不发请求', async () => {
    const bridge = stubBridge({
      sourceList: vi.fn(async () => ({ ok: true, value: [
        { sourceId: 'center-0', kind: 'center', state: 'ready', label: '研究中心' },
      ] })),
      clientQuery: vi.fn(async () => ({ ok: true, value: { items: [] } })),
    })
    setDesktop(bridge)
    bootSource.value = localSource()
    useAuthStore().profile = admin
    const router = makeRouter('/tasks/new')
    await router.isReady()
    const wrapper = mount(LocalTaskPrepare, { global: plugins(router) })
    await flushPromises()
    await wrapper.find('#task-operation').setValue('file')
    await wrapper.find('#task-center').setValue('center-0')
    await flushPromises()
    expect((wrapper.find('button[type="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    expect(bridge.clientPlanProposeFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
