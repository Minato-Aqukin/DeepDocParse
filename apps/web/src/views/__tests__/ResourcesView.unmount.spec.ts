import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/http', () => ({ http: { get }, downloadAs: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import ResourcesView from '@/views/ResourcesView.vue'
import { bootSource } from '@/platform/desktop'
import type { Resource } from '@/api/resources'

function activeResource(): Resource {
  return {
    id: 'resource-1', organization_id: 'org-1', owner_id: 'u-1',
    uploader_ref: { issuer: 'source-A', subject: 'u-1' },
    display_name: '许可技术手册', publication: 'private',
    versions: [{
      id: 'version-1', resource_id: 'resource-1', version_no: 1,
      document_id: 'document-1', source_digest: `sha256:${'b'.repeat(64)}`,
      filename: 'manual.pdf', size_bytes: 1200, parse_job_id: 'job-1',
      // 版本还在解析：load 落定后会起轮询 —— 卸载 mid-flight 的用例就靠
      // 这个形状让"起了不该起的轮询"暴露出来（落定后 list 会被反复调用）。
      parse_status: 'running', index_status: 'ready',
    }],
  }
}

async function mountView() {
  const wrapper = mount(ResourcesView, { global: { plugins: [ElementPlus], stubs: { UploadDialog: true } } })
  await flushPromises()
  return wrapper
}

describe('ResourcesView 卸载 mid-flight', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    bootSource.value = null
    Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
  })

  it('刷新在飞时卸载：resolve 后不许另起轮询（无孤儿 interval）', async () => {
    // 先让首刷在真时钟下落定（有版本在动，轮询已起）：假时钟下 flushPromises
    // 冲不完 load() 的整条 promise 链，资源落不进 resources，hasActive 恒假。
    get.mockResolvedValue({ data: { items: [activeResource()], has_more: false } } as never)
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('许可技术手册')
    let releaseList!: (value: { data: { items: Resource[]; has_more: boolean } }) => void
    const listGate = new Promise<{ data: { items: Resource[]; has_more: boolean } }>(
      (res) => { releaseList = res })
    get.mockReturnValue(listGate as never)
    await wrapper.findAll('button').find((b) => b.text() === '刷新')!.trigger('click')
    await flushPromises()
    // 刷新在飞：页面先被卸载（卸载时 onUnmounted 停掉已有轮询）。
    wrapper.unmount()
    // 删掉 load 尾的世代守卫，resolve 后会另起一个没人清的 interval。
    vi.useFakeTimers()
    try {
      releaseList({ data: { items: [activeResource()], has_more: false } })
      await flushPromises()
      // 判据是行为（推进时钟也不再刷），不是 vi.getTimerCount()：
      // Element Plus 的 tooltip 也会挂定时器。
      const callsAfterResolve = get.mock.calls.length
      await vi.advanceTimersByTimeAsync(10_000)
      await flushPromises()
      expect(get.mock.calls.length).toBe(callsAfterResolve)
    } finally {
      vi.useRealTimers()
    }
  })

  it('对照：挂载中 resolve 会正常起轮询', async () => {
    vi.useFakeTimers()
    try {
      let releaseList!: (value: { data: { items: Resource[]; has_more: boolean } }) => void
      const listGate = new Promise<{ data: { items: Resource[]; has_more: boolean } }>(
        (res) => { releaseList = res })
      get.mockReturnValue(listGate as never)
      const wrapper = await mountView()
      releaseList({ data: { items: [activeResource()], has_more: false } })
      await flushPromises()
      const callsBefore = get.mock.calls.length
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(get.mock.calls.length).toBeGreaterThan(callsBefore)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
