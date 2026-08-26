import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

import { authGuard } from '../guard'
import { routes } from '../routes'

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
})

describe('路由表本身', () => {
  it('每条具名路由都有 title（afterEach 拿它写文档标题）', () => {
    const named = routes.filter((r) => r.name)
    expect(named.length).toBeGreaterThan(0)
    const missing = named.filter((r) => !r.meta?.title).map((r) => String(r.name))
    expect(missing).toEqual([])
  })

  it('只有登录页是 public —— 多一个就是把内容暴露出去了', () => {
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
