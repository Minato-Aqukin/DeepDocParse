import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import type { ConsoleMessage, Page } from '@playwright/test'

/**
 * console 门禁的公共部分。**抽出来是为了只有一份允许列表** ——
 * 阶段 8 的深色主题一组要用同一套判据，复制一份的话两边迟早漂开，
 * 而漂开的方向必然是"某一边悄悄放宽了"。
 */

/** 收集 console error/warn 与未处理 promise rejection；阶段 7 起 warning 也必须为零。 */
export function watchErrors(page: Page): string[] {
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
  /net::ERR_SOCKET_NOT_CONNECTED/,
]

export function realErrors(errors: string[]): string[] {
  return errors.filter((e) => !IGNORED.some((re) => re.test(e)))
}

/**
 * 全部可达路径 —— **从 routes.ts 现读，不手抄一份**。
 *
 * 手抄的清单会漂：加了一个页面却忘了加进这里，"全路由零 console error"
 * 这条门禁就悄悄不覆盖它了 —— 而门禁漏掉的那一页，恰恰是最新、最可能出问题的。
 * 合仓时加 `/members` 就撞到了这件事。
 *
 * 用正则读文件而不是 import：e2e 不在任何 tsconfig 的 include 里
 * （Playwright 自己转译），走 `@/` 别名要额外配一套解析。
 * 这里只需要路径字面量，正则够了，而且**读不到就当场报错**，不会静默变空。
 */
function discoverPaths(): string[] {
  const source = readFileSync(
    fileURLToPath(new URL('../src/router/routes.ts', import.meta.url)), 'utf-8')
  const paths = [...source.matchAll(/path:\s*'([^']+)'/g)].map((m) => m[1]!)
  if (paths.length < 10) {
    throw new Error(`只从 routes.ts 解析出 ${paths.length} 条路径 —— 解析逻辑坏了，` +
      '而坏掉的表现是门禁静默地什么都不检查')
  }
  return [
    // 动态段填一个固定 id：桩 API 按它返回演示数据。
    // **带正则的段（catch-all 的 `:pathMatch(.*)*`）跳过** ——
    // 它没有一个"具体路径"，由下面那条兜底用例覆盖
    ...paths.filter((p) => !p.includes('(')).map((p) => p.replace(/:[A-Za-z_]+/g, 'demo-id')),
    '/no/such/path',
  ]
}

export const PATHS = discoverPaths()
