import { expect, test, type Page } from '@playwright/test'

import { PATHS, realErrors, watchErrors } from './console-guard'
import { fakeLogin, stubApi } from './stub-api'

/**
 * `plan.md` 阶段 8 §3「前端无 bug 的可操作定义」里**不需要真实部署**的那几条门禁。
 *
 * 「零 bug」没人能承诺，这里把它翻译成一组会红的判据：
 * 两套主题 · 零投影与零字距（DESIGN-GUIDE §7）· 不破版 · 键盘可达。
 * 剩下的「干净机器全新部署」与「真实用户路径」要一台能跑起全栈的机器，不在这里。
 *
 * **这些门禁进 CI，不是一次性检查** —— 否则下一个 commit 就退化了。
 */

// ---------------------------------------------------------------------------
// 门禁一 · 两套主题都不破
// ---------------------------------------------------------------------------

test.describe('深色主题下每条路由同样零 error/warn', () => {
  test.use({ colorScheme: 'dark' })
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  for (const path of PATHS) {
    test(`${path} 深色首屏干净`, async ({ page }) => {
      const errors = watchErrors(page)
      await page.goto(`/#${path}`)
      await expect(page.locator('#app')).not.toBeEmpty()
      expect(realErrors(errors), `${path} 在深色主题下报错`).toEqual([])
    })
  }
})

// ---------------------------------------------------------------------------
// 门禁二 · DESIGN-GUIDE §7 里能机械判定的：零投影 · 字距
// ---------------------------------------------------------------------------

/**
 * 投影只表示"浮起来"（准则五）。浮层类组件本来就该有投影，因此豁免；
 * 豁免靠祖先选择器判断，**不是按元素名单**，免得新增一个静态卡片就得改名单。
 */
const FLOATING = ['.el-popper', '.el-dialog', '.el-overlay', '.el-message',
                  '.el-drawer', '.el-notification', '.el-message-box', '.el-tooltip__popper',
                  // vite 的 Vue DevTools 浮层，dev server 才注入，不是本项目的代码
                  '.vue-devtools__panel', '#vue-devtools-anchor']

async function shadowOffenders(page: Page) {
  return page.evaluate((floating) => {
    const bad: string[] = []
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      if (floating.some((selector) => el.closest(selector))) continue
      const shadow = getComputedStyle(el).boxShadow
      if (!shadow || shadow === 'none' || el === document.activeElement) continue
      // **判据是"有没有模糊半径"，不是"有没有 box-shadow"。**
      // 准则五禁的是让东西浮起来的投影，而 box-shadow 在 Element Plus 里
      // 大量被用来画线：输入框的 `0 0 0 1px inset` 是边框，分段按钮的
      // `-1px 0 0 0` 是分隔线，项目自己的 ddp-element-plus.css 也用内阴影做焦点环。
      // 这些模糊半径都是 0 —— 是描边，不是投影。
      // 逗号可能出现在 rgb(...) 里，所以按括号深度切，不能直接 split(',')。
      // 多重阴影必须逐条判：只看整串的第 3 个长度值，第二条的模糊半径会被漏掉。
      const layers: string[] = []
      let depth = 0
      let current = ''
      for (const ch of shadow) {
        if (ch === '(') depth++
        else if (ch === ')') depth--
        if (ch === ',' && depth === 0) { layers.push(current); current = '' } else current += ch
      }
      layers.push(current)
      const lifted = layers.some((layer) => {
        if (layer.includes('inset')) return false
        const lengths = [...layer.matchAll(/(-?[\d.]+)px/g)].map((m) => parseFloat(m[1]!))
        return (lengths[2] ?? 0) > 0      // 第三个长度是模糊半径
      })
      if (lifted) {
        bad.push(`${el.tagName.toLowerCase()}.${el.className || '(no class)'} -> ${shadow}`)
      }
    }
    return bad.slice(0, 8)
  }, FLOATING)
}

/**
 * 字距归零（准则六），但**按设计系统实际实现的那条规则判**，不是按字面。
 *
 * `ddp-base.css` 给 h1/h2 留了 `-.02em`，紧挨着的注释写明理由：
 * 「负字距只给大字号。中文在 21px 以下会被负字距挤糊」。
 * 所以真正的判据是**小字号上不许有字距** —— 直接写成"全站必须为 0"的话，
 * 这条门禁从第一天起就是红的，而红的原因是判据抄错了，不是界面坏了；
 * 那种门禁的结局只有一个：被人加进允许列表，然后再也不响。
 */
const TRACKING_MIN_FONT_PX = 21

async function letterSpacingOffenders(page: Page) {
  return page.evaluate((minFont) => {
    const bad: string[] = []
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      const style = getComputedStyle(el)
      const spacing = parseFloat(style.letterSpacing)
      if (!spacing || Number.isNaN(spacing)) continue
      if (parseFloat(style.fontSize) >= minFont) continue
      bad.push(`${el.tagName.toLowerCase()}.${el.className || '(no class)'}`
        + ` -> ${style.letterSpacing} @ ${style.fontSize}`)
    }
    return bad.slice(0, 8)
  }, TRACKING_MIN_FONT_PX)
}

test.describe('DESIGN-GUIDE §7 自查清单里机械可判的两条', () => {
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  // 抽样而不是全量：这两条是全局 CSS 的性质，一页破了基本页页都破，
  // 而全量跑 15 条路由 × 2 项会把 e2e 时长翻倍，换不到相应的信息量。
  for (const path of ['/documents', '/documents/demo-id', '/graph', '/wiki', '/usage']) {
    test(`${path} 静态容器零投影、字距归零`, async ({ page }) => {
      await page.goto(`/#${path}`)
      await expect(page.locator('#app')).not.toBeEmpty()
      expect(await shadowOffenders(page),
             '静态容器不许有 box-shadow（准则五：投影只表示"浮起来"）').toEqual([])
      expect(await letterSpacingOffenders(page),
             `字距只许出现在 ${TRACKING_MIN_FONT_PX}px 以上的标题上（准则六：中文被负字距挤糊）`)
        .toEqual([])
    })
  }
})

// ---------------------------------------------------------------------------
// 门禁二之二 · 一个界面只有一种红（准则一）
// ---------------------------------------------------------------------------

/**
 * `--ddp-cite`（出处）与 `--ddp-danger`（出错）是**仅有的两个红**。
 * 别处冒出来的红 —— 硬编码的色值、组件库自带的那支 —— 都算违规：
 * 界面上同时有两种红，"红 = 这里要看"这条约定就失效了。
 *
 * 判据不是"有没有用 var(--ddp-cite)"（那查源码就行，且查不到运行时结果），
 * 而是**算出来的颜色偏红却不等于这两个令牌**。深浅两档的令牌值不同，
 * 所以先从 :root 上把当前档的值读出来再比。
 */
async function strayReds(page: Page) {
  return page.evaluate(() => {
    const root = getComputedStyle(document.documentElement)
    const probe = document.createElement('span')
    document.body.appendChild(probe)
    const resolve = (token: string) => {
      probe.style.color = root.getPropertyValue(token).trim()
      return getComputedStyle(probe).color
    }
    const sanctioned = new Set([resolve('--ddp-cite'), resolve('--ddp-danger')])
    probe.remove()

    const reddish = (value: string) => {
      const [r, g, b, a = '1'] = (value.match(/[\d.]+/g) || []) as string[]
      if (!r || parseFloat(a) < 0.2) return false
      const [red, green, blue] = [parseFloat(r), parseFloat(g), parseFloat(b!)]
      return red > 120 && red > green * 1.4 && red > blue * 1.4
    }

    const stray: string[] = []
    let sanctionedSeen = 0
    // **必须连伪元素一起扫。** 状态点是 `.ddp-status::before` 的背景色
    // （规范：形状是第一通道、颜色是第二通道），只看元素本身的话，
    // 整个界面最主要的红一个都看不见 —— 第一版就是这样，
    // 拿失败态文档跑出来"这一页零处红"。
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      for (const pseudo of [null, '::before', '::after']) {
        const style = getComputedStyle(el, pseudo)
        if (pseudo && style.content === 'none') continue
        for (const property of ['color', 'backgroundColor', 'borderTopColor',
                                'borderLeftColor', 'outlineColor'] as const) {
          const value = style[property]
          if (!reddish(value)) continue
          if (sanctioned.has(value)) { sanctionedSeen++; continue }
          stray.push(`${el.tagName.toLowerCase()}.${el.className || '(no class)'}`
            + `${pseudo || ''} ${property}=${value}`)
        }
      }
    }
    return { stray: stray.slice(0, 8), sanctionedSeen }
  })
}

test.describe('红只给出处与出错（准则一）', () => {
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  test('失败状态里的红全部来自那两个令牌，没有第二支红', async ({ page }) => {
    // 造一份"解析失败 + 索引失败"的文档：这一页必须真的有红，
    // 否则这条门禁就是在空页面上恒真 —— 下面 sanctionedSeen 那条断言钉住这点。
    await page.route((url) => url.pathname === '/api/documents', (route) => route.fulfill({
      json: [{
        id: 'bad-id', filename: 'broken.pdf', doc_id: 'b'.repeat(64), origin: 'upload',
        mime: 'application/pdf', size_bytes: 1, page_count: 1, status: 'failed',
        error: '解析引擎返回 unknown_engine', index_status: 'failed',
        index_error: 'embedding_unavailable', compile_status: 'failed',
        compile_degraded: ['vision_unavailable'], compile_fingerprint: null,
        layout_version: 'ddp-layout/1', code_detection: 'unavailable',
        current_job_id: 'job-1', created_at: new Date().toISOString(),
        uploaders: ['e2e'], can_delete: true,
      }],
    }))
    await page.goto('/#/documents')
    await expect(page.getByText('broken.pdf')).toBeVisible()

    const { stray, sanctionedSeen } = await strayReds(page)
    expect(sanctionedSeen,
           '这一页一点红都没有，门禁等于空跑 —— 打桩没造出失败态？').toBeGreaterThan(0)
    expect(stray, '除了 --ddp-cite / --ddp-danger，界面上不许有第二支红').toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 门禁三 · 不破版：页面本身永远不许横向滚动
// ---------------------------------------------------------------------------

/**
 * 破版的判据是**主内容区被撑宽**，不是 `document.body` 溢出。
 *
 * 第一版写的就是 body，**而那是条假守卫**：应用外壳的 `main.el-main` 是
 * `overflow-x: auto`，任何溢出都被它吸收掉，`documentElement.scrollWidth`
 * 永远等于视口宽。实测把表格钉成 3000px，body 纹丝不动（1280/1280），
 * 而 `main` 已经 3016/1072 —— 用户看到的正是整个版面横着滑。
 *
 * 宽表格、长代码块**应该**在自己的容器里滚（`.el-table__body-wrapper`、
 * `overflow-x:auto` 的 `.wrap`），那不算破版；撑到主内容区才算。
 */
async function mainOverflow(page: Page) {
  return page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('main.el-main') ?? document.body
    return { where: main.tagName.toLowerCase(),
             scrollWidth: main.scrollWidth, clientWidth: main.clientWidth,
             docScroll: document.documentElement.scrollWidth,
             docClient: document.documentElement.clientWidth }
  })
}

async function expectNoBodyOverflow(page: Page, what: string) {
  const box = await mainOverflow(page)
  expect(box.scrollWidth,
         `${what}：主内容区被撑宽（${box.where} ${box.scrollWidth} > ${box.clientWidth}）`)
    .toBeLessThanOrEqual(box.clientWidth + 1)
  expect(box.docScroll, `${what}：整页被撑宽（${box.docScroll} > ${box.docClient}）`)
    .toBeLessThanOrEqual(box.docClient + 1)
}

const LONG_NAME = `${'超长文件名'.repeat(40)}-${'a'.repeat(120)}.pdf`

test.describe('极端内容不破版', () => {
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  test('超长文件名不把文档库撑宽', async ({ page }) => {
    await page.route((url) => url.pathname === '/api/documents', (route) => route.fulfill({
      json: [{
        id: 'long-id', filename: LONG_NAME, doc_id: 'd'.repeat(64), origin: 'upload',
        mime: 'application/pdf', size_bytes: 1, page_count: 1, status: 'succeeded',
        error: null, index_status: 'ready', index_error: null, compile_status: 'ready',
        compile_degraded: [], compile_fingerprint: 'f'.repeat(64),
        layout_version: 'ddp-layout/1', code_detection: 'heuristic',
        current_job_id: 'job-1', created_at: new Date().toISOString(),
        uploaders: ['e2e'], can_delete: true,
      }],
    }))
    await page.goto('/#/documents')
    await expect(page.locator('#app')).not.toBeEmpty()
    await expectNoBodyOverflow(page, '超长文件名')
  })

  test('200 列的宽表在自己的容器里滚，不撑宽页面', async ({ page }) => {
    // 用 markdown 的管道表，不用裸 HTML：渲染器没开 `html:true`，
    // 裸 `<table>` 会被当文本转义掉，表根本进不了 DOM（第一版就栽在这）。
    const header = `| ${Array.from({ length: 200 }, (_, i) => `列 ${i}`).join(' | ')} |`
    const divider = `| ${Array.from({ length: 200 }, () => '---').join(' | ')} |`
    const cells = `| ${Array.from({ length: 200 }, (_, i) => `单元格 ${i}`).join(' | ')} |`
    await page.route((url) => /\/documents\/[^/]+\/result$/.test(url.pathname),
      (route) => route.fulfill({ json: {
        document_id: 'demo-id', job_id: 'job-1', filename: 'wide.pdf', page_count: 1,
        markdown: `# 宽表\n\n${header}\n${divider}\n${cells}\n`,
        images: [],
      } }))
    await page.goto('/#/documents/demo-id')
    await expect(page.locator('#app')).not.toBeEmpty()
    // 结果面板默认是"按页"视图，markdown 压根不渲染 —— 不切过去的话
    // 这条用例是**空跑的**（第一版正是如此：200 列的表从未出现在 DOM 里，
    // 用例却绿着）。切完再断言表真的在，前提坏掉就红，不会静默失效。
    // 点 label 不点 radio：Element Plus 的 `<input type=radio>` 是 opacity:0 的，
    // `getByRole('radio')` 会卡在可见性检查上直到用例超时（实测 60s 挂掉）。
    await page.locator('.el-radio-button', { hasText: 'Markdown' }).click()
    await expect(page.locator('.markdown-body table th').first()).toBeVisible()
    expect(await page.locator('.markdown-body table th').count()).toBe(200)
    await expectNoBodyOverflow(page, '200 列的表')
  })

  test('零结果与单条结果都给得出话，且不破版', async ({ page }) => {
    await page.goto('/#/search')
    await expect(page.locator('#app')).not.toBeEmpty()
    await page.getByRole('textbox').first().fill('找不到的东西')
    await page.keyboard.press('Enter')
    await expect(page.getByText('没有命中')).toBeVisible()
    await expectNoBodyOverflow(page, '零结果')

    await page.route((url) => url.pathname === '/api/search', (route) => route.fulfill({
      // 形状照 types/api.ts 的 SearchResult，别照记忆写
      json: { query: '唯一', degraded: null, groups: [{
        document_id: 'demo-id', filename: 'demo.pdf', hits: [{
          chunk_id: 'c1', parse_job_id: 'job-1', seq: 0, page_idx: 0,
          bbox: [0, 0, 10, 10],
          snippet: '唯一的一条结果', score: 0.03, similarity: 0.9,
        }],
      }] },
    }))
    await page.getByRole('textbox').first().fill('唯一')
    await page.keyboard.press('Enter')
    await expect(page.getByText('唯一的一条结果')).toBeVisible()
    await expectNoBodyOverflow(page, '单条结果')
  })
})

// ---------------------------------------------------------------------------
// 门禁四 · 键盘可达
// ---------------------------------------------------------------------------

test.describe('键盘走得完主路径且焦点看得见', () => {
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  test('Tab 能从文档库走到主操作，且每一跳焦点都可见', async ({ page }) => {
    await page.goto('/#/documents')
    await expect(page.locator('#app')).not.toBeEmpty()

    const seen: string[] = []
    let invisible: string | null = null
    for (let i = 0; i < 25; i++) {
      await page.keyboard.press('Tab')
      const info = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null
        if (!el || el === document.body) return null
        // **焦点环不一定画在被聚焦的元素上。** Element Plus 聚焦 input，
        // 环画在外面的 `.el-input__wrapper` 上 —— 只看 activeElement 会误报。
        const ring = (node: HTMLElement) => {
          const style = getComputedStyle(node)
          return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
            || (style.boxShadow !== 'none' && style.boxShadow !== '')
        }
        let node: HTMLElement | null = el
        let visible = false
        for (let up = 0; node && up < 3 && !visible; up++, node = node.parentElement) {
          visible = ring(node)
        }
        return { label: `${el.tagName.toLowerCase()}.${el.className || ''}`.slice(0, 60), visible }
      })
      if (!info) continue
      seen.push(info.label)
      if (!info.visible && invisible === null) invisible = info.label
    }

    expect(seen.length, 'Tab 一个可聚焦元素都没走到').toBeGreaterThan(3)
    expect(invisible, `这个元素聚焦了却看不出来：${invisible}`).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 门禁五 · 三态齐全：出错时必须说出原因（不变式 2）
// ---------------------------------------------------------------------------

/**
 * 阶段 7 新加的三块界面（Wiki / 图谱 / 复核队列）此前只验过"数据正常时长什么样"。
 * 后端挂掉时它们各自 `catch` 住并写 `errorText` —— 但**没有任何用例证明那句话
 * 真的出现在屏幕上**，而"静默地什么都不显示"正是本项目反复吃亏的形态：
 * 用户看到一个空列表，以为语料里就是没有东西。
 *
 * 判据有两条，缺一不可：① 错误态可见；② **说得出原因**（不是光一句"失败了"）。
 */
const FAILING = { status: 500, json: { error: { message: 'upstream exploded', code: 'upstream_error' } } }

test.describe('后端出错时界面必须说出原因', () => {
  test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubApi(page) })

  for (const [path, api, label] of [
    ['/wiki', '/api/wiki', 'Wiki 列表'],
    ['/graph', '/api/knowledge/graph', '图谱'],
  ] as const) {
    test(`${label}加载失败时给出错误态而不是空列表`, async ({ page }) => {
      await page.route((url) => url.pathname === api, (route) => route.fulfill(FAILING))
      await page.goto(`/#${path}`)
      const alert = page.locator('.el-alert--error').first()
      await expect(alert, `${label} 挂了却什么都没说`).toBeVisible()
      // 原因必须带上：只说"加载失败"等于把排查成本全推给用户
      await expect(alert).toContainText(/失败/)
      await expect(alert).not.toHaveText(/^\s*(加载失败|失败)\s*$/)
    })
  }

  test('复核队列加载失败时同样有错误态', async ({ page }) => {
    await page.route((url) => url.pathname === '/api/reviews', (route) => route.fulfill(FAILING))
    await page.goto('/#/graph')
    await expect(page.getByText(/复核队列加载失败/)).toBeVisible()
  })
})
