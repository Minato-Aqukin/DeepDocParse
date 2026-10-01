import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'

const { route, replace } = vi.hoisted(() => ({
  route: { query: {} as Record<string, string | undefined> }, replace: vi.fn(),
}))
vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => ({ replace }) }))

import SourcesView from '@/views/SourcesView.vue'
import { bootSource, type SourceSummary } from '@/platform/desktop'
import { authGuard } from '@/router/guard'
import { useAuthStore } from '@/stores/auth'
import type { RouteLocationNormalized } from 'vue-router'

function source(sourceId: string, overrides: Partial<SourceSummary> = {}): SourceSummary {
  return { sourceId, kind: 'center', label: sourceId, state: 'unavailable', readOnly: true,
    features: [], active: false, reason: null, ...overrides }
}

beforeEach(() => {
  setActivePinia(createPinia())
  bootSource.value = null
  route.query = {}
  replace.mockImplementation(async (target: { query: Record<string, string | undefined> }) => { route.query = target.query })
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
})

it('offers reconnect for both active and non-active centers that are not ready, not ready centers or local workspaces', async () => {
  window.ddpDesktop = {
    sourceList: vi.fn(async () => ({ ok: true, value: [
      source('blocked-center'), source('active-center', { active: true, state: 'signed_out' }),
      source('ready-center', { state: 'ready' }), source('local', { kind: 'local', readOnly: false }),
    ] })),
  } as never
  const wrapper = mount(SourcesView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  const rows = wrapper.findAll('.source-row')
  expect(rows[0]!.findAll('button').map(button => button.text())).toContain('重新连接')
  expect(rows[1]!.findAll('button').map(button => button.text())).toContain('重新连接')
  expect(rows[2]!.findAll('button').map(button => button.text())).not.toContain('重新连接')
  expect(rows[3]!.findAll('button').map(button => button.text())).not.toContain('重新连接')
})

it('reconnects without activating, prevents a duplicate click while pending, and displays a labelled host failure', async () => {
  let finish!: (result: { ok: false; error: { code: string } }) => void
  const sourceReconnect = vi.fn(() => new Promise<{ ok: false; error: { code: string } }>(resolve => { finish = resolve }))
  const sourceActivate = vi.fn()
  window.ddpDesktop = {
    sourceList: vi.fn(async () => ({ ok: true, value: [
      source('local', { kind: 'local', active: true, state: 'ready', readOnly: false }), source('blocked-center'),
    ] })),
    sourceReconnect, sourceActivate,
  } as never
  const wrapper = mount(SourcesView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  const reconnect = wrapper.findAll('.source-row')[1]!.findAll('button').find(button => button.text() === '重新连接')!
  await reconnect.trigger('click')
  expect(sourceReconnect).toHaveBeenCalledWith({ sourceId: 'blocked-center' })
  expect(reconnect.element.disabled).toBe(true)
  await reconnect.trigger('click')
  expect(sourceReconnect).toHaveBeenCalledTimes(1)
  finish({ ok: false, error: { code: 'source_signed_out' } })
  await flushPromises()
  expect(wrapper.get('[role=\"alert\"]').text()).toContain('重新连接失败：登录已过期，请重新连接该中心')
  expect(reconnect.element.disabled).toBe(false)
  expect(sourceActivate).not.toHaveBeenCalled()
  expect(wrapper.findAll('.source-row')[0]!.text()).toContain('local（当前）')
})

it('shows an unsuccessful readiness result rather than reporting a successful reconnect', async () => {
  window.ddpDesktop = {
    sourceList: vi.fn(async () => ({ ok: true, value: [source('blocked-center')] })),
    sourceReconnect: vi.fn(async () => ({ ok: true, value: source('blocked-center', {
      state: 'signed_out', reason: 'source_signed_out',
    }) })),
  } as never
  const wrapper = mount(SourcesView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  await wrapper.findAll('.source-row')[0]!.findAll('button').find(button => button.text() === '重新连接')!.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=\"alert\"]').text()).toContain('重新连接失败：登录已过期，请重新连接该中心')
})

it('makes a rejected reconnect visible and clears the pending control', async () => {
  window.ddpDesktop = {
    sourceList: vi.fn(async () => ({ ok: true, value: [source('blocked-center')] })),
    sourceReconnect: vi.fn(async () => { throw new Error('IPC unavailable') }),
  } as never
  const wrapper = mount(SourcesView, { global: { plugins: [ElementPlus], config: { errorHandler: () => {} } } })
  await flushPromises()
  const reconnect = wrapper.findAll('.source-row')[0]!.findAll('button').find(button => button.text() === '重新连接')!
  await reconnect.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=\"alert\"]').text()).toContain('重新连接失败')
  expect(reconnect.element.disabled).toBe(false)
})

it('restores authenticated navigation after reconnecting the active center and removes its stale reason', async () => {
  const unavailable = source('active-center', { active: true, features: ['resources'] })
  let current = unavailable
  bootSource.value = unavailable
  route.query = { reason: 'no_active_source' }
  const sourceActivate = vi.fn()
  window.ddpDesktop = {
    sourceList: vi.fn(async () => ({ ok: true, value: [current] })),
    sourceReconnect: vi.fn(async () => {
      current = { ...unavailable, state: 'ready' }
      return { ok: true, value: current }
    }),
    sourceActivate,
  } as never
  const resources = { name: 'resources', fullPath: '/resources', meta: { public: true, features: ['resources'] } } as unknown as RouteLocationNormalized
  const auth = useAuthStore()
  expect(auth.isAuthenticated).toBe(false)
  expect(await authGuard(resources)).toEqual({ name: 'sources', query: {} })
  const wrapper = mount(SourcesView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  await wrapper.findAll('.source-row')[0]!.findAll('button').find(button => button.text() === '重新连接')!.trigger('click')
  await flushPromises()
  expect(auth.isAuthenticated).toBe(true)
  expect(await authGuard(resources)).toBe(true)
  expect(bootSource.value?.sourceId).toBe('active-center')
  expect(route.query.reason).toBeUndefined()
  expect(sourceActivate).not.toHaveBeenCalled()
})
