import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'

import { authApi, TOKEN_KEY } from '@/api'
import type { Profile } from '@/types/api'
import { bootSource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

import { authGuard } from '../guard'
import { routes } from '../routes'

function setDesktop(bridge: unknown) {
  Object.defineProperty(window, 'ddpDesktop', { value: bridge, configurable: true, writable: true })
}

function clearDesktop() {
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
}

function desktopRoutes() {
  return routes.filter((r) => r.meta?.platform !== 'web')
}

function makeDesktopRouter() {
  const router = createRouter({ history: createMemoryHistory(), routes: desktopRoutes() })
  router.beforeEach(authGuard)
  return router
}

function readySource(overrides: Record<string, unknown> = {}) {
  return {
    sourceId: 's1', kind: 'local', label: 'ws', state: 'ready',
    readOnly: false, features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'],
    active: true, reason: null, ...overrides,
  } as never
}

beforeEach(() => {
  vi.restoreAllMocks()
  clearDesktop()
  bootSource.value = null
  setActivePinia(createPinia())
  localStorage.clear()
})

describe('桌面守卫', () => {
  it('无源时受保护路由去数据源页（首运落点），不去登录页', async () => {
    setDesktop({})
    bootSource.value = null
    const router = makeDesktopRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('sources')
    expect(router.currentRoute.value.query.reason).toBe('no_active_source')
  })

  it('signed_out 源去数据源页并带过期原因', async () => {
    setDesktop({})
    bootSource.value = readySource({ state: 'signed_out', reason: 'source_signed_out' })
    const router = makeDesktopRouter()
    await router.push('/documents')
    expect(router.currentRoute.value.name).toBe('sources')
    expect(router.currentRoute.value.query.reason).toBe('source_signed_out')
  })

  it('可用源放行内容路由', async () => {
    setDesktop({})
    bootSource.value = readySource()
    vi.spyOn(authApi, 'me').mockResolvedValue({
      data: { id: 'u', username: 'owner', email: null, role: 'admin', organization_id: 'o', created_at: '' },
      status: 200, statusText: 'OK', headers: {}, config: { headers: {} },
    } as never)
    const router = makeDesktopRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('resources')
  })

  it('当前源缺能力 → 去数据源页并带 not_supported_locally', async () => {
    setDesktop({})
    bootSource.value = readySource({ features: ['resources', 'documents'] })
    vi.spyOn(authApi, 'me').mockResolvedValue({
      data: { id: 'u', username: 'owner', email: null, role: 'admin', organization_id: 'o', created_at: '' },
      status: 200, statusText: 'OK', headers: {}, config: { headers: {} },
    } as never)
    const router = makeDesktopRouter()
    await router.push('/wiki')
    expect(router.currentRoute.value.name).toBe('sources')
    expect(router.currentRoute.value.query.reason).toBe('not_supported_locally')
  })

  it('桌面只读源下 canUpload 为 false（按钮禁用不断言角色）', () => {
    setDesktop({})
    bootSource.value = readySource({ kind: 'center', readOnly: true })
    const auth = useAuthStore()
    auth.profile = {
      id: 'u', username: 'member', email: null, role: 'admin',
      organization_id: 'o', created_at: '',
    } satisfies Profile
    expect(auth.readOnly).toBe(true)
    expect(auth.canUpload).toBe(false)
  })

  it('本机源下高角色可上传', () => {
    setDesktop({})
    bootSource.value = readySource({ kind: 'local', readOnly: false })
    const auth = useAuthStore()
    auth.profile = {
      id: 'u', username: 'owner', email: null, role: 'admin',
      organization_id: 'o', created_at: '',
    } satisfies Profile
    expect(auth.readOnly).toBe(false)
    expect(auth.canUpload).toBe(true)
  })
})

describe('桌面路由表', () => {
  it('桌面不注册 login/members/keys/usage/settings/extractions/graph', () => {
    setDesktop({})
    const router = makeDesktopRouter()
    for (const name of ['login', 'members', 'keys', 'usage', 'settings', 'extractions', 'graph']) {
      expect(router.hasRoute(name), `桌面不应注册 ${name}`).toBe(false)
    }
  })

  it('桌面注册 sources/models/desktop-settings/updates', () => {
    setDesktop({})
    const router = makeDesktopRouter()
    for (const name of ['sources', 'local-models', 'desktop-settings', 'updates']) {
      expect(router.hasRoute(name), `桌面应注册 ${name}`).toBe(true)
    }
  })

  it('浏览器路由表不受影响（含 login 与 members）', () => {
    const names = routes.filter((r) => r.name).map((r) => String(r.name))
    for (const name of ['login', 'members', 'keys', 'usage', 'settings', 'extractions', 'graph']) {
      expect(names).toContain(name)
    }
  })

  it('桌面根：无源 → /sources，有源 → /resources', () => {
    const root = routes.find((r) => r.path === '/')
    const redirect = root?.redirect as (() => string) | undefined
    expect(typeof redirect).toBe('function')
    clearDesktop()
    expect(redirect!()).toBe('/resources')
    setDesktop({})
    bootSource.value = null
    expect(redirect!()).toBe('/sources')
    bootSource.value = readySource()
    expect(redirect!()).toBe('/resources')
  })

  it('desktop-only 路由在浏览器守卫下回退到资源库', async () => {
    // 静态 import 即可：vue-router 在浏览器与桌面都是同一个包，
    // 这里只是用另一套路由表建第二个 router 实例。
    clearDesktop()
    localStorage.setItem(TOKEN_KEY, 't')
    vi.spyOn(authApi, 'me').mockResolvedValue({
      data: { id: 'u', username: 'c', email: null, role: 'contributor', organization_id: 'o', created_at: '' },
      status: 200, statusText: 'OK', headers: {}, config: { headers: {} },
    } as never)
    const router = createRouter({ history: createMemoryHistory(), routes })
    router.beforeEach(authGuard)
    await router.push('/sources')
    expect(router.currentRoute.value.name).toBe('resources')
  })
})
