import { expect, test, type Page } from '@playwright/test'

import { fakeLogin, stubApi } from './stub-api'

/**
 * 用例 3：解析状态全可见；且**待编译 → 编译中 → 编译完整**三态真正在 UI 上出现。
 * 阶段 5 另钉住 `code_detection` 的 native / heuristic / unavailable 三态。
 * 用例 4：**后端返回降级标记时界面必须显示原因，不许静默**（对应不变式 2）。
 *
 * 后端整个被 route 拦截打桩：这一层要验的是"界面把状态说清楚了没有"，
 * 不是后端对不对。打桩也让三态可以**确定性地**依次出现 ——
 * 真后端上"编译中"可能一闪而过，抓不稳。
 */

const BASE = {
  id: 'doc-1', filename: '技术手册.pdf', doc_id: 'h'.repeat(64), origin: 'upload',
  mime: 'application/pdf', size_bytes: 1024, page_count: 0,
  error: null, index_error: null, current_job_id: 'job-1',
  compile_status: 'ready', compile_degraded: [], compile_fingerprint: 'f'.repeat(64),
  layout_version: 'ddp-layout/1', code_detection: 'heuristic',
  uploaders: ['e2e'], can_delete: true,
  created_at: new Date().toISOString(),
}

// 状态取值必须来自 types/api.ts 的 ParseStatus（pending/running/archiving/
// succeeded/failed）。写一个不存在的值不会报错 —— parseStatusOf 会原样把它
// 当文案显示，于是断言永远找不到中文标签，看起来像"三态没出现"
const STATES = {
  pending: { ...BASE, status: 'pending', index_status: 'none' },
  running: { ...BASE, status: 'running', index_status: 'none' },
  // 归档中与阶段 5 新增的 compiling 是两个独立状态：前者取回并归档
  // service 结果，后者在本层裁图 / VLM 理解 / 建索引，不再混用一个文案。
  archiving: { ...BASE, status: 'archiving', index_status: 'none' },
  succeeded: { ...BASE, status: 'succeeded', index_status: 'ready', page_count: 12 },
} as const

async function login(page: Page) {
  await fakeLogin(page)
  // 先铺一层"什么都空着"的底，再由各用例覆盖自己关心的那个接口。
  // 少了这层，视图里其它接口会真打到 8080，后端没起时满屏 ECONNREFUSED
  await stubApi(page)
}

/** 让 /api/documents 返回给定的文档列表，其余接口给空。 */
async function stubDocuments(page: Page, docs: unknown[]) {
  // 谓词而不是 glob：`**/api/**` 会连 vite 提供的 `/src/api/*.ts` 一起拦下来，
  // 模块图一断整个应用就不挂载（见 stub-api.ts 的注释，这一脚踩过）
  await page.route(
    (url) => url.pathname === '/api/documents',
    (route) => route.fulfill({ json: docs }),
  )
  // 形状必须与 types/api.ts 的 DocumentStats 一致 —— 给错的话
  // StatCard 收到 undefined，Vue 抛一串 prop 告警，看起来像组件坏了
  await page.route(
    (url) => url.pathname.endsWith('/api/documents/stats/summary'),
    (route) => route.fulfill({ json: { documents: 1, pages: 12, askable: 1 } }),
  )
}

test('文档库把排队 / 解析中 / 归档中 / 完成每种状态都显示出来', async ({ page }) => {
  // **逐个状态各加载一次，而不是等轮询自己翻过去。**
  // 靠 3s 轮询驱动的写法在并行跑时会偶发红（实测两次全量里红了两次）——
  // 而**偶发红比没有测试更糟**：它教人忽略红色。
  // "轮询会不会自动翻状态"由 usePolling 的单测钉着，这里只管
  // "每一种状态在界面上都有说法"，这才是这条用例真正要守的东西。
  const cases = [
    ['pending', '排队中'],
    ['running', '解析中'],
    ['archiving', '归档中'],
    ['succeeded', '已完成'],
  ] as const

  for (const [status, label] of cases) {
    await login(page)
    await stubDocuments(page, [{ ...BASE, ...STATES[status] }])
    await page.goto('/#/documents')
    await expect(
      page.locator('#app'),
      `状态 ${status} 在界面上没有任何说法`,
    ).toContainText(label)
  }
})

test('code_detection 三态在文档库都有明确文案', async ({ page }) => {
  const cases = [
    ['native', '代码识别：原生'],
    ['heuristic', '代码识别：启发式'],
    ['unavailable', '代码识别：不可用'],
  ] as const
  await login(page)
  await stubDocuments(page, cases.map(([codeDetection], index) => ({
    ...BASE, id: `doc-${index}`, filename: `code-${index}.pdf`, code_detection: codeDetection,
  })))
  await page.goto('/#/documents')
  for (const [, label] of cases) {
    await expect(page.locator('#app')).toContainText(label)
  }
})

test('工作台把待编译 / 编译中 / 编译完整三态显示出来', async ({ page }) => {
  const cases = [
    ['pending', '待编译'],
    ['compiling', '编译中'],
    ['ready', '编译完整'],
  ] as const
  let currentCompileStatus: string = 'pending'
  await login(page)
  await page.route(
    (url) => /\/api\/documents\/[^/]+$/.test(url.pathname),
    (route) => route.fulfill({
      json: { ...BASE, ...STATES.succeeded, compile_status: currentCompileStatus },
    }),
  )
  for (const [caseStatus, label] of cases) {
    // 单一 route 通过可变当前值返回三态，避免循环注册多个相同 route
    // 后取决于 Playwright 的匹配先后，把测试写成偶发绿。
    currentCompileStatus = caseStatus
    await page.goto('/#/documents/doc-1')
    await expect(page.locator('#app')).toContainText(label)
  }
})

test('检索结果带降级标记时，界面必须说出原因', async ({ page }) => {
  await login(page)
  // 后端说这次只走了关键词路。**界面不能装作一切正常** —— 不变式 2
  // 又是谓词不是 glob：`**/api/search**` 会把 vite 提供的
  // `/src/api/search.ts` 一起拦下来，应用直接不挂载
  await page.route(
    (url) => url.pathname.startsWith('/api/search'),
    (route) => route.fulfill({
      json: {
        query: '锅炉',
        degraded: 'embedding_unavailable',
        groups: [{
          document_id: 'doc-1', filename: '技术手册.pdf',
          hits: [{ page_idx: 3, snippet: '命中片段', chunk_id: 'c1', similarity: 0.4 }],
        }],
      },
    }),
  )

  await page.goto('/#/search?q=锅炉')
  // **必须用会重试的断言。** SearchView 的 watch 是 immediate + async，
  // 而 page.goto 在 load 就返回了 —— 检索请求与渲染都还没完成。
  // `expect(string).toMatch()` 是一次性快照，不重试，机器一慢就无故变红
  // （验收实测在只改了别处的那一轮里抓到过一次）。
  await expect(
    page.locator('#app'),
    '降级了却没有在界面上给出任何原因',
  ).toContainText(/向量化服务不可用|已降级/)
})

test('解析失败时界面给出错误原因，而不是只说失败', async ({ page }) => {
  await login(page)
  await stubDocuments(page, [{
    ...BASE, status: 'failed', index_status: 'none',
    error: 'unknown_engine: mineru 未注册',
  }])

  await page.goto('/#/documents')
  await expect(page.locator('#app')).toContainText('失败')

  // 只说"失败"不够 —— 用户要知道为什么，否则和"我们挂了"分不开。
  // 原因挂在状态格的 el-tooltip 上（DocumentTable.vue），**要 hover 才出来**，
  // 所以不能对 innerText 断言：那样只能证明"页面上没有这段文字"，
  // 而它其实是有的，只是藏在 hover 后面。
  await page.getByText('失败', { exact: true }).first().hover()
  // `.el-popper` 会命中一堆（每个 tooltip 都有一个，多数是隐藏的），
  // 要找的是**可见的那个**
  await expect(
    page.locator('.el-popper:visible').filter({ hasText: /unknown_engine|未注册/ }),
  ).toBeVisible()
})

test.describe('PINNED · 已知缺陷，不是通过标准', () => {
  test('失败原因只在 hover 时出现 —— 键盘/触屏用户看不到', async ({ page }) => {
    // **这一条绿不代表这件事是对的。**
    // 名字里的 PINNED 是给读到"31 passed"的人看的：其中这一条钉的是一个
    // **被刻意保留的已知缺陷**，不是一条通过标准。
    //
    // 事实：原因确实展示了（不变式 2 满足），但唯一途径是鼠标 hover。
    // plan.md 阶段 8 的门禁里有"键盘可达"一条 —— 到那一步改成常驻文案时，
    // 这条会红。**那时候该做的是删掉这条用例，不是删掉那段改动。**
    // 现在不改，是因为阶段 0b 已经破例改过一处业务代码（AskPanel 的 blob 泄漏），
    // 而 tooltip 改常驻会动 DocumentTable 的布局，那是阶段 8 的活。
    await login(page)
    await stubDocuments(page, [{
      ...BASE, status: 'failed', index_status: 'none', error: 'unknown_engine: 未注册',
    }])
    await page.goto('/#/documents')
    await expect(page.locator('#app')).toContainText('失败')
    // 没 hover 之前，页面正文里找不到原因
    expect(await page.locator('#app').innerText()).not.toContain('unknown_engine')
  })
})
