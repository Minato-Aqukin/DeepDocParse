import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'

import { bootSource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'
import type { Profile } from '@/types/api'

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

function stubBridge() {
  return {
    sourceList: vi.fn(async () => ({ ok: true, value: [] })),
    sourceActivate: vi.fn(async () => ({ ok: true, value: {} })),
  }
}

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  askStream: vi.fn(() => () => undefined),
  conversationsApi: {
    list: vi.fn(async () => ({ data: [] })),
    messages: vi.fn(async () => ({ data: [] })),
    create: vi.fn(), remove: vi.fn(),
  },
}))

vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(async () => null),
  renderMarkdown: (value: string) => value,
  resolveAuthedImages: async () => () => {},
}))

import AskPanel from '@/components/ask/AskPanel.vue'
import { askStream, conversationsApi } from '@/api'

const document = {
  id: 'd1', filename: 'manual.pdf', index_status: 'ready', index_error: null,
} as never

const stubs = {
  'el-input': {
    props: ['modelValue'], emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
  return mount(AskPanel, {
    props: { document },
    global: { plugins: [router], stubs },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  clearDesktop()
  bootSource.value = null
})

describe('AskPanel 中心只读入口', () => {
  it('浏览器不显示"作为联邦任务发起"', async () => {
    useAuthStore().profile = admin
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).not.toContain('作为联邦任务发起')
    wrapper.unmount()
  })

  it('中心只读显示入口并带预填问题跳到 /tasks/new', async () => {
    setDesktop(stubBridge())
    bootSource.value = {
      sourceId: 's', kind: 'center', label: 'c', state: 'ready',
      readOnly: true, features: [], active: true, reason: null,
    }
    useAuthStore().profile = admin
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/tasks/new', name: 'federation-task-new', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    const wrapper = mount(AskPanel, {
      props: { document },
      global: { plugins: [router], stubs },
    })
    await flushPromises()
    await wrapper.find('textarea').setValue('第 3 页讲了什么？')
    // Unregistered el-button renders its props as attributes: disabled="true" / "false".
    const button = (label: string) => wrapper.findAll('el-button').find((b) => b.text() === label)
    expect(button('发送')?.attributes('disabled')).toBe('true')
    expect(button('新会话')?.attributes('disabled')).toBe('true')
    await button('新会话')!.trigger('click')
    await wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(conversationsApi.create).not.toHaveBeenCalled()
    expect(askStream).not.toHaveBeenCalled()
    const entry = wrapper.findAll('el-button').find((b) => b.text() === '作为联邦任务发起')
    expect(entry?.exists()).toBe(true)
    await entry!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('federation-task-new')
    expect(router.currentRoute.value.query.query).toBe('第 3 页讲了什么？')
    wrapper.unmount()
  })

  it('本机源不显示入口', async () => {
    setDesktop(stubBridge())
    bootSource.value = {
      sourceId: 's', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    useAuthStore().profile = admin
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).not.toContain('作为联邦任务发起')
    wrapper.unmount()
  })
})
