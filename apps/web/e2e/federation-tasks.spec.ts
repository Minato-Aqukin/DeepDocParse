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

/**
 * 计划的有效期在夹具里是一个固定时刻，而"计划有没有过期"是拿真实时钟比的 ——
 * 直接用夹具的话这个用例会在那一刻之后永远红。所以发给界面之前把有效期改到"现在之后"，
 * 形状仍然是那份过了冻结契约的夹具。
 */
/**
 * 已认证的身份握手。签许可要用的三样（本节点 / 工作区 / 用户）都从这里来 ——
 * 界面不从 auth store 取：那份 profile 只在设置页加载过，任务页上是空的。
 */
const HANDSHAKE = {
  protocol_version: 'ddp-client/1',
  identity: { environment_id: 'node-center-a', authority_node_id: 'node-center-a', workspace_id: 'org-1' },
  profile: { issuer: 'node-center-a', subject: 'u-1' },
  capabilities: [], profiles: [], capability_status: {}, accepting_admissions: true,
}

function stillValid<T extends { valid_until: string; budget?: { deadline: string } }>(plan: T): T {
  const until = new Date(Date.now() + 30 * 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z')
  return { ...plan, valid_until: until, ...(plan.budget ? { budget: { ...plan.budget, deadline: until } } : {}) }
}

test('创建任务：不勾问题原文就真的不外发，而且没有远端目标时许可必须是 local_only', async ({ page }) => {
  const errors = watchErrors(page)
  const intents: Record<string, unknown>[] = []
  await api(page, (url) => ['/api/v1/tasks', '/api/v1/capabilities', '/api/v1/federation/scopes',
    '/api/v1/task-intents', '/api/v1/task-plans'].includes(url.pathname), async (url, route) => {
    if (url.pathname === '/api/v1/tasks' && route.request().method() === 'GET') return route.fulfill({ json: fixture('task-list-last-page.json') })
    if (url.pathname === '/api/v1/capabilities') return route.fulfill({ json: HANDSHAKE })
    if (url.pathname === '/api/v1/federation/scopes') return route.fulfill({ status: 201, json: fixture('scope-sealed.json') })
    if (url.pathname === '/api/v1/task-intents') {
      intents.push(route.request().postDataJSON())
      return route.fulfill({ status: 201, json: { root_task_id: ROOT2, planning_state: 'draft', status: 'queued' } })
    }
    return route.fulfill({ json: stillValid(fixture('plan-ready.json')) })
  })

  await page.goto('/#/tasks')
  await page.getByRole('button', { name: '新建任务' }).click()
  // Element Plus 的 radio/checkbox 真实 input 是透明的、被样式层盖住，点不到 —— 点它的文字。
  const composer = page.getByRole('form', { name: '新建联邦任务' })
  await composer.getByRole('textbox').fill('额定电压是多少？')

  // 先确认没有远端目标时界面就说了"不会发出一个字节"，且根本不显示外发许可那一块
  await expect(composer.getByText('没有远端目标：本次不会向任何其他节点发出一个字节。')).toBeVisible()
  await expect(composer.getByRole('group', { name: '外发许可' })).toHaveCount(0)

  // 切到联邦范围并生成清单：清单里有一个远端节点，外发许可这才出现
  await composer.getByText('联邦公开范围', { exact: true }).click()
  await composer.getByRole('button', { name: '生成范围清单' }).click()
  await expect(composer.getByText('远端节点 node-peer-b')).toBeVisible()
  const egress = composer.getByRole('group', { name: '外发许可' })
  await expect(egress).toBeVisible()

  // **把问题原文取消勾选** —— 界面不许替用户补回去
  await expect(egress.getByRole('checkbox', { name: '问题原文' })).toBeChecked()
  await egress.getByText('问题原文', { exact: true }).click()
  await expect(egress.getByRole('checkbox', { name: '问题原文' })).not.toBeChecked()
  await composer.getByRole('button', { name: '创建任务并规划' }).click()
  await expect(page).toHaveURL(new RegExp(`#/tasks/${ROOT2}$`))

  expect(intents).toHaveLength(1)
  const consent = (intents[0] as { exploration_consent: Record<string, unknown> }).exploration_consent
  expect(consent.egress_mode).toBe('listed_nodes')
  expect(consent.allowed_recipients).toEqual(['node-peer-b'])
  // 这条就是本用例的全部意义：勾掉了就一个字节都不发
  expect(consent.allowed_payload).toEqual([])
  const spec = (intents[0] as { task_spec: Record<string, unknown> }).task_spec
  expect(spec.execution_policy).toEqual({ mode: 'trusted_federation', coordinator_ref: 'node-center-a' })
  expect((spec.consent_refs as { exploration: string }).exploration).toBe(consent.consent_id)
  expect(realErrors(errors)).toEqual([])
})

test('批准计划：许可覆盖到中继节点，受理用的是界面上显示的那一份摘要', async ({ page }) => {
  const errors = watchErrors(page)
  const plan = stillValid(fixture('plan-ready.json'))
  let approval: Record<string, unknown> | null = null
  let submission: Record<string, unknown> | null = null
  let submitted = false
  await api(page, (url) => url.pathname.startsWith('/api/v1/task') || url.pathname === '/api/v1/capabilities', async (url, route) => {
    const method = route.request().method()
    if (url.pathname === '/api/v1/capabilities') return route.fulfill({ json: HANDSHAKE })
    if (url.pathname === `/api/v1/tasks/${ROOT2}` && method === 'GET') {
      return route.fulfill({ json: submitted ? { ...fixture('task-queued.json'), status: 'running', planning_state: 'approved' } : fixture('task-queued.json') })
    }
    if (url.pathname === `/api/v1/tasks/${ROOT2}/events`) return route.fulfill({ json: { root_task_id: ROOT2, events: [], next_seq: 0, complete: true } })
    if (url.pathname === '/api/v1/task-plans') return route.fulfill({ json: plan })
    if (url.pathname === `/api/v1/task-plans/${ROOT2}/approve`) {
      approval = route.request().postDataJSON()
      return route.fulfill({ json: { ...plan, planning_state: 'approved' } })
    }
    if (url.pathname === '/api/v1/tasks' && method === 'POST') {
      submission = route.request().postDataJSON()
      submitted = true
      return route.fulfill({ status: 202, json: { ...fixture('task-queued.json'), status: 'running', planning_state: 'approved' } })
    }
    return route.fallback()
  })

  await page.goto(`/#/tasks/${ROOT2}`)
  const approvalPanel = page.getByRole('region', { name: '计划审阅' })
  await expect(approvalPanel).toBeVisible()
  // 中继也是接收方 —— 用户批准之前必须看得见它
  await expect(approvalPanel.getByText('node-peer-b、node-relay-c')).toBeVisible()
  await expect(approvalPanel.getByText('问题原文')).toBeVisible()

  await approvalPanel.getByRole('button', { name: '批准并执行' }).click()
  await expect(page.getByRole('region', { name: '计划审阅' })).toHaveCount(0)

  const approved = approval as unknown as { plan_digest: string; execution_consent: Record<string, unknown> }
  expect(approved.plan_digest).toBe(plan.plan_digest)
  expect(approved.execution_consent.plan_digest).toBe(plan.plan_digest)
  // 执行者 + 数据边两端 + 中继，一个都不能少，否则协调者 egress_denied
  expect(approved.execution_consent.allowed_recipients).toEqual(['node-center-a', 'node-peer-b', 'node-relay-c'])
  expect(approved.execution_consent.allowed_edges).toEqual(['edge-query-2'])
  expect(approved.execution_consent.retention).toBe('temporary')
  // 受理的是刚批的那一份，不是别的修订
  expect(submission as unknown as { plan_digest: string }).toMatchObject({ root_task_id: ROOT2, plan_digest: plan.plan_digest })
  expect(realErrors(errors)).toEqual([])
})

test('取消与续跑按协调者的判据给按钮：规划没批准过就不给续跑', async ({ page }) => {
  let status = { ...fixture('task-queued.json'), status: 'failed', planning_state: 'ready' }
  await api(page, (url) => url.pathname.startsWith('/api/v1/task'), async (url, route) => {
    if (url.pathname === `/api/v1/tasks/${ROOT2}/events`) return route.fulfill({ json: { root_task_id: ROOT2, events: [], next_seq: 0, complete: true } })
    if (url.pathname === `/api/v1/tasks/${ROOT2}/cancel`) {
      status = { ...status, status: 'cancelled' }
      return route.fulfill({ json: status })
    }
    if (url.pathname === `/api/v1/tasks/${ROOT2}`) return route.fulfill({ json: status })
    return route.fallback()
  })

  // 失败了，但计划从没批准过 —— 协调者的 resume 会 403，所以界面不该给这个按钮
  await page.goto(`/#/tasks/${ROOT2}`)
  await expect(page.getByRole('button', { name: '补做未完成目标' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '取消任务' })).toHaveCount(0)   // failed 已落定

  // 批准过的失败任务才给续跑
  status = { ...status, status: 'failed', planning_state: 'approved' }
  await page.reload()
  await expect(page.getByRole('button', { name: '补做未完成目标' })).toBeVisible()

  // 还在跑的任务给取消，取消后按钮消失（终态）
  status = { ...status, status: 'running' }
  await page.reload()
  const cancel = page.getByRole('button', { name: '取消任务' })
  await expect(cancel).toBeVisible()
  await cancel.click()
  await expect(page.getByRole('button', { name: '取消任务' })).toHaveCount(0)
  // 限定在状态轴里：结果区的"任务已取消，没有结果。"也含这三个字
  await expect(page.getByLabel('状态轴').getByText('已取消')).toBeVisible()
})
