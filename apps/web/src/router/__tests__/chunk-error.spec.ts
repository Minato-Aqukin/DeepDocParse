import { flushPromises } from '@vue/test-utils'
import { ElMessage } from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isNavigationFailure, NavigationFailureType } from 'vue-router'
import type * as VueRouter from 'vue-router'

const lazy = vi.hoisted(() => ({ load: vi.fn() }))
vi.mock('../routes', () => ({ routes: [
  { path: '/resources', name: 'resources', component: { template: '<div>资源</div>' } },
  { path: '/sources', name: 'sources', component: () => lazy.load() },
] }))
vi.mock('../guard', () => ({ authGuard: () => true }))
vi.mock('vue-router', async (original) => {
  const actual = await original<typeof VueRouter>()
  return { ...actual, createWebHashHistory: actual.createMemoryHistory }
})

afterEach(async () => {
  ElMessage.closeAll()
  await flushPromises()
  vi.useRealTimers()
  await vi.waitFor(() => expect(document.querySelectorAll('.el-message')).toHaveLength(0))
  vi.unstubAllGlobals()
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true })
  vi.resetModules()
})

describe('界面文件加载失败', () => {
  it.each([false, true])('浏览器 / 桌面（%s）导航失败保留当前页，持续提示并仅按用户选择重载', async (desktop) => {
    Object.defineProperty(window, 'ddpDesktop', { value: desktop ? {} : undefined, configurable: true })
    const reload = vi.fn()
    vi.stubGlobal('location', { reload })
    // Recreate the module's router for each platform and exercise its loading boundary.
    const { default: router } = await import('../index')
    await router.push('/resources')
    const error = new TypeError('Failed to fetch dynamically imported module: ddp://app/assets/SourcesView-old.js')
    lazy.load.mockRejectedValue(error)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await expect(router.push('/sources')).rejects.toBe(error)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('resources')
    const notice = document.querySelector('.el-message')
    expect(notice?.textContent).toContain('界面文件已变更或缺失')
    expect(notice?.textContent).toContain('重新加载窗口')
    vi.advanceTimersByTime(30_000)
    await flushPromises()
    expect(document.querySelector('.el-message')).toBe(notice)
    expect(reload).not.toHaveBeenCalled()
    await expect(router.push('/sources')).rejects.toBe(error)
    await flushPromises()
    expect(document.querySelectorAll('.el-message')).toHaveLength(1)
    const button = notice?.querySelector('button')
    expect(button).not.toBeNull()
    button?.click()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})

it('普通导航异常保留诊断信息，反复失败只显示一条持续提示', async () => {
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
  // Recreate the router to exercise its module-installed navigation error handler.
  const { default: router } = await import('../index')
  await router.push('/resources')
  const error = new Error('navigation guard failed')
  lazy.load.mockRejectedValue(error)
  await expect(router.push('/sources')).rejects.toBe(error)
  await expect(router.push('/sources')).rejects.toBe(error)
  await flushPromises()
  expect(logged).toHaveBeenCalledTimes(2)
  expect(logged).toHaveBeenCalledWith(error)
  expect(document.querySelectorAll('.el-message')).toHaveLength(1)
  expect(document.querySelector('.el-message')?.textContent).toContain('navigation guard failed')
})

it('守卫中止与重定向不显示页面异常提示', async () => {
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
  // Each test needs the same real handler on a fresh router.
  const { default: router } = await import('../index')
  await router.push('/resources')
  router.addRoute({ path: '/redirect', component: { template: '<div />' } })
  router.beforeEach((to) => {
    if (to.path === '/sources') return false
    if (to.path === '/redirect') return '/resources'
    return true
  })
  const aborted = await router.push('/sources')
  expect(isNavigationFailure(aborted, NavigationFailureType.aborted)).toBe(true)
  await router.push('/redirect')
  await flushPromises()
  expect(router.currentRoute.value.name).toBe('resources')
  expect(document.querySelectorAll('.el-message')).toHaveLength(0)
  expect(logged).not.toHaveBeenCalled()
})
