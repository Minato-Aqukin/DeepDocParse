import { expect, test, type ConsoleMessage, type Page } from '@playwright/test'

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

/** 收集 console error/warn 与未处理 promise rejection；阶段 7 起 warning 也必须为零。 */
function watchErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error' || msg.type() === 'warning') {
      errors.push(`console.${msg.type()}: ${msg.text()}`)
    }
  })
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))
  return errors
}

/**
 * 允许列表 —— **保持为空，别往里加东西。**
 *
 * 曾经这里放着 `Failed to load resource` / `net::ERR_` 之类，理由是"后端没起"。
 * 但 `stubApi()` 把 API 整个挡在网络层之外之后，正常情况下**一条网络错误都不该有**
 * （已实测：清空这个列表，27 条路由用例照样全绿）。
 * 既然如此，留着它只有一个作用：**某天真出了网络层的错，被它悄悄吞掉**。
 *
 * 真要加一条，先问：这个错误是"环境噪声"还是"我们暂时不想修的 bug"？
 * 后者应该留着让它红，或者显式 `test.fixme`，而不是塞进这里。
 */
const IGNORED: RegExp[] = [
  // **只有这一条，而且它不是"应用可能出的错"。**
  // 并行跑时 vite dev server 与浏览器之间偶发的连接拆除竞态：
  // 30 条用例跑几十轮才出现一次，且单独反复加载同一页 6 次一次都复现不出来
  // （用 requestfailed 探针验过，零命中）。应用代码没有任何路径能产生它。
  //
  // 曾经这里还有 `Failed to load resource` 与整个 `net::ERR_` 家族，
  // 理由是"后端没起"。`stubApi()` 之后那个理由不成立了 —— 实测清空整张表
  // 27 条路由用例照样全绿。**留着宽的允许列表只有一个作用：某天真出了
  // 网络层的错，被它悄悄吞掉。** 所以只保留这一条最窄的。
  //
  // 想再加一条前先问：这是"环境噪声"还是"我们暂时不想修的 bug"？
  // 后者应该留着让它红，或者显式 test.fixme，而不是塞进这里。
  /net::ERR_SOCKET_NOT_CONNECTED/,
]

function realErrors(errors: string[]): string[] {
  return errors.filter((e) => !IGNORED.some((re) => re.test(e)))
}

// routes.ts 里的全部可达路径（含三条兼容重定向与一条兜底）
const PATHS = [
  '/', '/login', '/documents', '/documents/demo-id', '/documents/demo-id/versions',
  '/extractions', '/search', '/keys', '/usage', '/settings',
  '/wiki', '/graph',
  '/dashboard', '/task/demo-id', '/no/such/path',
]

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
