import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { expect, test, type Page, type Route } from '@playwright/test'

import { realErrors, watchErrors } from './console-guard'
import { fakeLogin, stubApi } from './stub-api'

/**
 * 联邦任务页。夹具在 `fixtures/federation/`，**每个都由 corpus-api 的
 * `test_web_federation_fixtures.py` 按冻结契约校验**（x-ddp-enum 展开成真实取值）——
 * 这里的替身不比真实端点宽松。
 */
const fixture = (name: string) => JSON.parse(readFileSync(
  fileURLToPath(new URL(`./fixtures/federation/${name}`, import.meta.url)), 'utf-8'))

const ROOT = '8f14e45fceea167a5a36dedd4bea2543'
const ROOT2 = '1679091c5a880faf6fb5e6087eb1b2dc'

const evidenceDetail = {
  id: 'evidence-local-1', document: { id: 'doc-7', filename: '控制器手册-v2.pdf' },
  page_idx: 11, seq: 3, parse_job_id: 'job-3', doc_version: 2,
  bbox: [72, 540, 523, 588], page_size: [595, 842], kind: 'text',
  content: '最大输入电压：240 V', source_type: 'source', derived_from: null,
  crop_url: null, review_state: 'unreviewed', chunk_id: 'chunk-9', verifications: [],
}

async function api(page: Page, match: (url: URL, route: Route) => boolean, respond: (url: URL, route: Route) => Promise<void>) {
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const url = new URL(route.request().url())
    if (match(url, route)) return respond(url, route)
    return route.fallback()
  })
}

test.beforeEach(async ({ page }) => {
  await fakeLogin(page)
  await stubApi(page)
})

/**
 * 记录**整个过程中出现过**的全局 toast。
 *
 * 不能用 `expect(locator('.el-message')).toHaveCount(0)`：toast 是异步弹出、几秒后自己消失的，
 * 那条断言可能在它出现之前就满足了 —— 去掉被测的抑制开关它照样绿（实测确认过）。
 */
async function recordToasts(page: Page) {
  await page.addInitScript(() => {
    const seen: string[] = []
    ;(window as unknown as { __toasts: string[] }).__toasts = seen
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node instanceof HTMLElement && node.classList.contains('el-message')) {
            seen.push(node.textContent ?? '')
          }
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true })
  })
}

const toasts = (page: Page) => page.evaluate(() => (window as unknown as { __toasts: string[] }).__toasts)

test('任务列表按状态轴列出本人任务，翻页用服务端游标且能回到上一页', async ({ page }, info) => {
  const errors = watchErrors(page)
  const cursors: (string | null)[] = []
  await api(page, (url) => url.pathname === '/api/v1/tasks', async (url, route) => {
    const cursor = url.searchParams.get('cursor')
    cursors.push(cursor)
    expect(url.searchParams.get('limit')).toBe('20')
    await route.fulfill({ json: fixture(cursor ? 'task-list-last-page.json' : 'task-list.json') })
  })
  await page.goto('/#/tasks')
  await expect(page.getByRole('heading', { name: '联邦任务', exact: true })).toBeVisible()
  const rows = page.locator('tbody tr')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toContainText('PM-2 的最大输入电压是多少？')
  await expect(rows.nth(0)).toContainText('证据不足')
  await expect(rows.nth(1).locator('.ddp-status.is-live', { hasText: '执行中' })).toBeVisible()
  await expect(rows.nth(1)).toContainText('联邦公开范围')
  await page.screenshot({ path: info.outputPath('federation-task-list.png'), fullPage: true, animations: 'disabled' })

  await page.getByRole('button', { name: '下一页' }).click()
  await expect(rows).toHaveCount(1)
  await expect(rows.nth(0)).toContainText('更早的一个问题')
  await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled()
  await page.getByRole('button', { name: '上一页' }).click()
  await expect(rows).toHaveCount(2)
  expect(cursors).toEqual([null, fixture('task-list.json').next_cursor, null])

  await rows.nth(1).getByRole('link').click()
  await expect(page).toHaveURL(new RegExp(`#/tasks/${ROOT}$`))
  expect(realErrors(errors)).toEqual([])
})

test('执行中的任务轮询到落定：矛盾与未查全在答案之前，引用指回证据，本节点证据可开原文', async ({ page }, info) => {
  const errors = watchErrors(page)
  let reads = 0
  let settle = false
  const afters: string[] = []
  const planBodies: unknown[] = []
  await api(page, (url) => url.pathname.startsWith(`/api/v1/tasks/${ROOT}`) || url.pathname === '/api/v1/task-plans'
    || url.pathname === '/api/evidence/evidence-local-1', async (url, route) => {
    if (url.pathname === `/api/v1/tasks/${ROOT}`) {
      reads++
      return route.fulfill({ json: fixture(settle ? 'task-succeeded.json' : 'task-running.json') })
    }
    if (url.pathname.endsWith('/events')) {
      const after = url.searchParams.get('after') ?? ''
      afters.push(after)
      if (after === '0') return route.fulfill({ json: fixture('events-first.json') })
      if (after === '3' && settle) return route.fulfill({ json: fixture('events-later.json') })
      return route.fulfill({ json: { root_task_id: ROOT, events: [], next_seq: Number(after), complete: true } })
    }
    if (url.pathname.endsWith('/coverage')) return route.fulfill({ json: fixture('coverage.json') })
    if (url.pathname === '/api/v1/task-plans') {
      planBodies.push(route.request().postDataJSON())
      return route.fulfill({ json: fixture('plan.json') })
    }
    return route.fulfill({ json: evidenceDetail })
  })

  await page.goto(`/#/tasks/${ROOT}`)
  const axes = page.getByLabel('状态轴')
  await expect(axes.locator('.ddp-status.is-live', { hasText: '执行中' })).toBeVisible()
  await expect(page.getByText('还没有结果；执行结束后显示在这里。')).toBeVisible()
  await expect(page.getByText('计划已生成，等待批准')).toBeVisible()
  // 还在执行时轮询不停：再等过一个周期，读取次数必须增加
  const before = reads
  await expect.poll(() => reads, { timeout: 5_000 }).toBeGreaterThan(before)

  // 后端落定：下一次轮询拿到 succeeded
  settle = true
  await expect(axes).toContainText('已完成', { timeout: 10_000 })
  await expect(axes).toContainText('证据存在矛盾')
  await expect(axes).toContainText('部分')

  const result = page.getByLabel('任务结果')
  const text = await result.innerText()
  expect(text.indexOf('证据之间存在矛盾')).toBeLessThan(text.indexOf('回答'))
  expect(text.indexOf('只查了部分范围')).toBeLessThan(text.indexOf('回答'))
  await expect(page.getByLabel('证据矛盾')).toContainText('同一资料的版本不一致')
  await expect(page.getByLabel('未查到的范围')).toContainText('node-peer-c / collection-c')
  await expect(page.getByLabel('未查到的范围')).toContainText('peer_unavailable')

  await page.getByRole('button', { name: '查看证据 2' }).click()
  await expect(page.locator('#evidence-2')).toHaveClass(/focused/)
  await expect(page.locator('#evidence-2')).toContainText('远端 node-peer-b')
  await expect(page.locator('#evidence-2')).toContainText('原文由来源节点持有')
  await expect(page.locator('#evidence-1')).toContainText('第 12 页 · 块 3')
  await expect(page.locator('#evidence-1')).toContainText('本节点')

  await expect(page.getByLabel('覆盖账本')).toContainText('node-peer-c / collection-c')
  await expect(page.getByLabel('执行计划')).toContainText('远端 node-peer-b')
  await expect(page.getByLabel('执行计划')).toContainText('query_text')
  await expect(page.getByLabel('任务事件')).toContainText('已创建任务')
  await expect(page.getByLabel('任务事件')).toContainText('结果待确认')
  await page.screenshot({ path: info.outputPath('federation-task-detail.png'), fullPage: true, animations: 'disabled' })

  await page.locator('#evidence-1').getByRole('button', { name: '查看原文出处' }).click()
  await expect(page.getByText('最大输入电压：240 V')).toBeVisible()

  // 落定后停止轮询；计划只读一次，而且读的是这个任务
  const settledReads = reads
  await page.waitForTimeout(4_500)
  expect(reads).toBe(settledReads)
  expect(planBodies).toEqual([{ root_task_id: ROOT }])
  expect(afters.slice(0, 2)).toEqual(['0', '3'])
  expect(realErrors(errors)).toEqual([])
})

test('没有答案时说出契约里的原因；覆盖账本读不到时显示原因而不是空白', async ({ page }) => {
  await api(page, (url) => url.pathname.startsWith(`/api/v1/tasks/${ROOT2}`) || url.pathname === '/api/v1/task-plans', async (url, route) => {
    if (url.pathname === `/api/v1/tasks/${ROOT2}`) return route.fulfill({ json: fixture('task-insufficient.json') })
    if (url.pathname.endsWith('/events')) return route.fulfill({ json: { root_task_id: ROOT2, events: [], next_seq: 0, complete: true } })
    if (url.pathname.endsWith('/coverage')) {
      return route.fulfill({ status: 500, json: { error: { message: 'ledger store down', type: 'server_error', code: 'upstream_error' } } })
    }
    return route.fulfill({ status: 409, json: { error: { message: 'plan changed', type: 'invalid_request_error', code: 'plan_changed' } } })
  })
  await page.goto(`/#/tasks/${ROOT2}`)
  await expect(page.getByText('证据不足：本次取得的原文不足以支撑结论')).toBeVisible()
  await expect(page.getByText('没有生成回答：远端生成步骤未完成（peer_execution_timeout）')).toBeVisible()
  await expect(page.getByText('覆盖账本读取失败：upstream_error（ledger store down）')).toBeVisible()
  await expect(page.getByText('执行计划读取失败：plan_changed（plan changed）')).toBeVisible()
  await expect(page.getByText('执行计划没读到，暂时判断不了这条证据是否在本节点')).toBeVisible()
})

test('任务不存在与列表格式不兼容都显示可见原因，且不再弹一个说法不同的全局提示', async ({ page }) => {
  await recordToasts(page)
  await api(page, (url) => url.pathname === '/api/v1/tasks/missing-task' || url.pathname === '/api/v1/tasks', async (url, route) => {
    if (url.pathname === '/api/v1/tasks') return route.fulfill({ json: [] })
    return route.fulfill({ status: 404, json: { error: { message: 'task intent not found', type: 'invalid_request_error', code: 'task_not_found' } } })
  })
  await page.goto('/#/tasks/missing-task')
  await expect(page.getByRole('alert')).toContainText('任务不存在，或你没有权限查看。')
  // 页面自己说明了，就**不许再弹一个全局 toast**：那样页面上会有两个 role="alert"，
  // 而且 toast 显示的是后端原文（"task intent not found"），把这里刻意合并
  // 404 与无权限的模糊化绕过去了。
  expect(await toasts(page)).toEqual([])
  await expect(page.getByText('task intent not found')).toHaveCount(0)
  await page.goto('/#/tasks')
  await expect(page.getByRole('alert')).toContainText('格式不兼容')
  expect(await toasts(page)).toEqual([])
})
