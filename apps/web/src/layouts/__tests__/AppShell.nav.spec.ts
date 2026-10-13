import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import AppShell from '@/layouts/AppShell.vue'
import { bootSource } from '@/platform/desktop'
import { routes } from '@/router/routes'
import { useAuthStore } from '@/stores/auth'

// 浏览器侧边栏：内容能力（features）是桌面"当前源"的概念，浏览器上恒为空。
// 曾经 Web 也套用这层过滤，资源库/文档库/检索/Wiki 在侧边栏里全部消失，没有任何报错。
async function sidebar(setup: () => void) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: routes.filter((r) => r.meta?.platform !== 'desktop'),
  })
  setup()
  await router.push('/search')
  const wrapper = mount(AppShell, {
    // 顶栏的用户下拉与侧边栏无关；它在 jsdom 里会触发 ElementPlus 的递归更新告警
    global: { plugins: [ElementPlus, router], stubs: { RouterView: true, ElDropdown: true } },
  })
  await flushPromises()
  return wrapper.findAll('.el-menu-item').map((item) => item.text())
}

describe('AppShell 浏览器侧边栏', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    bootSource.value = null
    Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
  })

  it('登录用户看得到内容页，也看得到账号与开发者页', async () => {
    const items = await sidebar(() => { useAuthStore().token = 'session' })
    expect(items).toEqual(expect.arrayContaining(['资源库', '文档库', '全文检索', '知识 Wiki', 'API Key', '成员与角色']))
  })

  it('游客只看得到访客路由', async () => {
    const items = await sidebar(() => { useAuthStore().guestEnabled = true })
    expect(items).toEqual(expect.arrayContaining(['资源库', '文档库', '全文检索', '知识 Wiki']))
    for (const hidden of ['API Key', '成员与角色', '结构化抽取', '联邦任务', '用量', '设置']) {
      expect(items).not.toContain(hidden)
    }
  })
})
