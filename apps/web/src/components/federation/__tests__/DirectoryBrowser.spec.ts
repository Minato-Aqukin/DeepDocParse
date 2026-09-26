import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { createSnapshot } = vi.hoisted(() => ({ createSnapshot: vi.fn() }))
vi.mock('@/api/directory', () => ({ directoryApi: { createSnapshot, members: vi.fn(), nodes: vi.fn() } }))

import DirectoryBrowser from '@/components/federation/DirectoryBrowser.vue'
import { bootSource } from '@/platform/desktop'

beforeEach(() => {
  setActivePinia(createPinia())
  createSnapshot.mockReset()
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
  bootSource.value = null
})

describe('DirectoryBrowser 中心只读', () => {
  it('桌面中心源下封存（POST）禁用并写明原因，点击也不发请求', async () => {
    Object.defineProperty(window, 'ddpDesktop', { value: {}, configurable: true, writable: true })
    bootSource.value = { sourceId: 'center-0', kind: 'center', label: '研究中心', state: 'ready', readOnly: true,
      features: [], active: true, reason: null } as never
    const wrapper = mount(DirectoryBrowser)
    const seal = wrapper.findAll('el-button').find((b) => b.text() === '封存当前可见目录')!
    // Unregistered el-button renders its props as attributes: disabled="true" / "false".
    expect(seal.attributes('disabled')).toBe('true')
    expect(wrapper.text()).toContain('中心在桌面里只读；写操作请作为联邦任务发起并批准')
    await seal.trigger('click')
    expect(createSnapshot).not.toHaveBeenCalled()
  })

  it('浏览器里照常可封存', () => {
    const wrapper = mount(DirectoryBrowser)
    const seal = wrapper.findAll('el-button').find((b) => b.text() === '封存当前可见目录')!
    expect(seal.attributes('disabled')).toBe('false')
    expect(wrapper.text()).not.toContain('中心在桌面里只读')
  })
})
