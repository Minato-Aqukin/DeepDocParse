import { expect, test } from '@playwright/test'

import { PATHS, realErrors, watchErrors } from './console-guard'
import { fakeLogin, stubApi } from './stub-api'

/**
 * 用例 5：全部路由逐条首屏渲染成功、console 零 error。
 *
 * 这是整套 e2e 里最便宜也最容易退化的一条门禁。它抓的是
 * 「按钮点了没反应」「路由白屏」这一类 —— vue-tsc 与 jsdom 单测都够不着。
 *
 * 未登录时大部分路由会被守卫弹回登录页；那**也是**一次真实渲染，
 * 同样不许报错。带 token 的那一组另外跑（见下面 describe）。
 */

test.describe('未登录时每条路由都渲染得出来', () => {
  test.beforeEach(async ({ page }) => { await stubApi(page) })

  for (const path of PATHS) {
    test(`${path} 首屏无 console error`, async ({ page }) => {
      const errors = watchErrors(page)
      await page.goto(`/#${path}`)
      // 渲染成功的判据：body 里有内容，不是白屏
      await expect(page.locator('#app')).not.toBeEmpty()
      expect(realErrors(errors), `${path} 报了 console error`).toEqual([])
    })
  }
})

test.describe('已登录时每条路由都渲染得出来', () => {
  test.beforeEach(async ({ page }) => {
    // 守卫只看 store 里有没有 token，而 store 从 localStorage 初始化。
    // 塞一个假 token 就能走到受保护页面 —— 这一组只验"渲染得出来"，不验后端
    await fakeLogin(page)
    await stubApi(page)
  })

  for (const path of PATHS) {
    test(`${path} 首屏无 console error`, async ({ page }) => {
      const errors = watchErrors(page)
      await page.goto(`/#${path}`)
      await expect(page.locator('#app')).not.toBeEmpty()
      expect(realErrors(errors), `${path} 报了 console error`).toEqual([])
    })
  }
})

test('未登录访问受保护路由 -> 跳登录并带 redirect', async ({ page }) => {
  await stubApi(page)
  await page.goto('/#/documents/abc123')
  await expect(page).toHaveURL(/#\/login\?redirect=/)
  // redirect 必须是完整原路径，否则登录后回不到用户本来要去的地方
  expect(decodeURIComponent(page.url())).toContain('redirect=/documents/abc123')
})
