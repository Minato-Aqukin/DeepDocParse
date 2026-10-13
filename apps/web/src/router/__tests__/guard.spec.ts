import { AxiosHeaders, type AxiosResponse } from 'axios'
import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'

import { authApi, TOKEN_KEY } from '@/api'
import type { Profile } from '@/types/api'
import { useAuthStore } from '@/stores/auth'

import { authGuard } from '../guard'
import { routes } from '../routes'

const profile: Profile = {
  id: 'core-user', username: 'contributor', email: null, role: 'contributor',
  organization_id: 'core-org', created_at: '2026-09-23T00:00:00Z',
}
const profileResponse = () => ({
  data: profile, status: 200, statusText: 'OK', headers: {},
  config: { headers: new AxiosHeaders() },
})
const guestResponse = (enabled: boolean) => ({
  data: { enabled }, status: 200, statusText: 'OK', headers: {},
  config: { headers: new AxiosHeaders() },
})
/**
 * 用例 6：**未登录访问受保护路由 → 跳登录并带 redirect。**
 * 用例 5 的一半：**每条路由都声明齐全**（见文件末尾那组）。
 *
 * **守卫是 import 来的，不是复制的。** 这条曾经栽过：早先版本在这里
 * 抄了一份守卫逻辑去测，于是把 `router/index.ts` 里真正的 `redirect`
 * 去掉之后，单测**照样全绿** —— 而"跳登录要带 redirect"正是
 * plan.md 首批必须覆盖的六条之一。现在守卫抽在 `router/guard.ts`，
 * 生产与用例引用同一份；改真守卫这里必红。
 */
function makeRouter() {
  const router = createRouter({ history: createMemoryHistory(), routes })
  router.beforeEach(authGuard)
  return router
}

describe('路由守卫', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(authApi, 'me').mockResolvedValue(profileResponse())
    // 默认关着游客模式：老用例测的都是"关着时的行为"，不能因为守卫多问了一次开关就变。
    vi.spyOn(authApi, 'guest').mockResolvedValue(guestResponse(false))
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('未登录访问受保护路由 -> 跳登录并带上 redirect', async () => {
    const router = makeRouter()
    await router.push('/documents/abc123')
    expect(router.currentRoute.value.name).toBe('login')
    // redirect 必须带上**完整路径**，否则登录后回不到用户原本要去的地方
    expect(router.currentRoute.value.query.redirect).toBe('/documents/abc123')
  })

  it('未登录访问登录页不跳转', async () => {
    const router = makeRouter()
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('login')
  })

  it('本站资源入口要求登录', async () => {
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/resources')
  })

  it('已登录访问登录页 -> 回文档库', async () => {
    const router = makeRouter()
    useAuthStore().token = 'fake-jwt'
    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('documents')
  })

  it('已登录时受保护路由放行', async () => {
    const router = makeRouter()
    useAuthStore().token = 'fake-jwt'
    await router.push('/search')
    expect(router.currentRoute.value.name).toBe('search')
  })

  it('未知路径落到文档库，不是白屏', async () => {
    const router = makeRouter()
    useAuthStore().token = 'fake-jwt'
    await router.push('/this/does/not/exist')
    expect(router.currentRoute.value.name).toBe('documents')
  })

  it('浏览器重启后恢复服务端角色，资源上传权限不会静默消失', async () => {
    localStorage.setItem(TOKEN_KEY, 'persisted-session')
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('resources')
    expect(useAuthStore().canUpload).toBe(true)
    expect(useAuthStore().role).toBe('contributor')
  })

  it('恢复会话被拒绝时清除身份并保留原始返回路径', async () => {
    localStorage.setItem(TOKEN_KEY, 'expired-session')
    vi.mocked(authApi.me).mockRejectedValue({ response: { status: 401 } })
    const router = makeRouter()
    await router.push('/documents/fixed?version_id=v1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/documents/fixed?version_id=v1')
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
  })

  it('账号服务暂时故障不销毁会话，也不授予写权限', async () => {
    localStorage.setItem(TOKEN_KEY, 'valid-session')
    vi.mocked(authApi.me).mockRejectedValue({ response: { status: 503 } })
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('resources')
    expect(useAuthStore().isAuthenticated).toBe(true)
    expect(useAuthStore().canUpload).toBe(false)
    expect(useAuthStore().profileError).toBe('unavailable')
  })

  it('退出后迟到的账号信息不能复活身份或写权限', async () => {
    const auth = useAuthStore()
    auth.token = 'old-session'
    const { promise, resolve } = Promise.withResolvers<AxiosResponse<Profile>>()
    vi.mocked(authApi.me).mockReturnValue(promise)
    const pending = auth.fetchProfile()
    auth.logout()
    resolve(profileResponse())
    await pending
    expect(auth.isAuthenticated).toBe(false)
    expect(auth.username).toBe('')
  })
})

describe('游客模式守卫（浏览器）', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(authApi, 'me').mockResolvedValue(profileResponse())
    setActivePinia(createPinia())
    localStorage.clear()
  })

  function mockGuest(enabled: boolean) {
    vi.spyOn(authApi, 'guest').mockResolvedValue(guestResponse(enabled))
  }

  it('开着时未登录进访客路由不跳登录（/resources、/search、/documents/:id）', async () => {
    mockGuest(true)
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('resources')
    await router.push('/search?q=手册')
    expect(router.currentRoute.value.name).toBe('search')
    await router.push('/documents/abc123')
    expect(router.currentRoute.value.name).toBe('workbench')
    // 游客不许调 /api/auth/me：上面三段导航一个 me 请求都不该有。
    expect(authApi.me).not.toHaveBeenCalled()
    expect(useAuthStore().isGuest).toBe(true)
  })

  it('开着时未登录进非访客路由回登录并带 redirect', async () => {
    mockGuest(true)
    const router = makeRouter()
    for (const [path, name] of [['/keys', 'keys'], ['/members', 'members'],
      ['/tasks', 'federation-tasks']] as const) {
      await router.push(path)
      expect(router.currentRoute.value.name).toBe('login')
      expect(router.currentRoute.value.query.redirect).toBe(path)
      expect(name).toBe(router.getRoutes().find((r) => r.path === path)?.name)
    }
  })

  it('关着时未登录进访客路由也回登录（老行为不变）', async () => {
    mockGuest(false)
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/resources')
    await router.push('/search')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/search')
  })

  it('开关问不到时按关处理，回登录', async () => {
    vi.spyOn(authApi, 'guest').mockRejectedValue(new Error('network down'))
    const router = makeRouter()
    await router.push('/resources')
    expect(router.currentRoute.value.name).toBe('login')
    expect(useAuthStore().guestEnabled).toBe(false)
  })

  it('游客进登录页不跳转；带着 redirect 登录后能回去', async () => {
    mockGuest(true)
    const router = makeRouter()
    await router.push({ name: 'login', query: { redirect: '/resources' } })
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/resources')
  })

  it('访客路由就是 resources / documents / workbench / versions / search / wiki 六个', () => {
    const guestNames = routes.filter((r) => r.meta?.guest).map((r) => String(r.name)).sort()
    expect(guestNames).toEqual(
      ['documents', 'resources', 'search', 'versions', 'wiki', 'workbench'].sort())
  })
})

describe('路由表本身', () => {
  it('每条具名路由都有 title（afterEach 拿它写文档标题）', () => {
    const named = routes.filter((r) => r.name)
    expect(named.length).toBeGreaterThan(0)
    const missing = named.filter((r) => !r.meta?.title).map((r) => String(r.name))
    expect(missing).toEqual([])
  })

  it('只有登录免本站登录；资料由各自身份边界授权', () => {
    const publicNames = routes.filter((r) => r.meta?.public).map((r) => String(r.name))
    expect(publicNames).toEqual(['login'])
  })

  it('每条具名路由都能被解析出来（组件路径写错在这里就红）', () => {
    const router = makeRouter()
    for (const r of routes.filter((x) => x.name)) {
      expect(router.hasRoute(r.name!), `路由 ${String(r.name)} 没注册上`).toBe(true)
    }
  })
})
