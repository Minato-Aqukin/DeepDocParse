import { readFileSync } from 'node:fs'

import { expect, test, type Page } from '@playwright/test'

import { realErrors, watchErrors } from './console-guard'
import { stubApi } from './stub-api'

/**
 * 桌面 AppShell（数据源 + 宿主 `/api` 代理）的浏览器侧用例。
 *
 * 宿主的真实行为（令牌只在主进程、中心非 GET 零出网、`_object` 改写）由
 * `apps/desktop/test/*.test.mjs` 与真窗口冒烟钉住；这里驱动的是同一套 Web 页面：
 * `window.ddpDesktop` 换成记录调用的桩，`ddp://app/**` 的 fetch/XHR 落回同源，
 * 由 `stubApi` 与各用例的路由应答，形状与本机子集一致。
 */
type Call = { name: string; input: Record<string, any> }
type Source = Record<string, any>

function localSource(sourceId: string, label: string, active: boolean): Source {
  return { sourceId, kind: 'local', label, state: 'ready', readOnly: false,
    features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'], active, reason: null,
    environment: { environmentId: 'env-' + sourceId, workspaceId: 'ws-' + sourceId, authorityNodeId: 'env-' + sourceId },
    profile: { profileId: 'profile-' + sourceId, issuer: 'env-' + sourceId, subject: 'owner' } }
}

async function desktopShell(page: Page, options: {
  sources?: Source[]
  handle?: (name: string, input: Record<string, any>) => unknown
} = {}) {
  const calls: Call[] = []
  const drafts = new Map<string, { revision: number; value: unknown }>()
  const sources = options.sources ?? [localSource('local-0', '甲的工作区', true)]
  await stubApi(page)
  await page.exposeBinding('desktopShellCall', async (_source, { name, input = {} }) => {
    calls.push({ name, input })
    const custom = options.handle?.(name, input)
    if (custom !== undefined) return custom
    const ok = (value: unknown) => ({ ok: true, value })
    if (name === 'sourceList') return ok(sources)
    if (name === 'sourceActivate') {
      const target = sources.find(item => item.sourceId === input.sourceId)
      if (!target) return { ok: false, error: { code: 'source_unavailable' } }
      for (const item of sources) item.active = item.sourceId === target.sourceId
      return ok(target)
    }
    if (name === 'hostStatus') {
      return ok({ secrets: { backend: 'basic_text', persistentAvailable: false }, lifecycle: 'close_stops_owned_local_tasks' })
    }
    const draftKey = `${input.connectionId}:${input.key}`
    if (name === 'clientReadDraft') return ok(drafts.get(draftKey) ?? null)
    if (name === 'clientSaveDraft') {
      const previous = drafts.get(draftKey)
      if ((previous?.revision ?? 0) !== input.expectedRevision) return { ok: false, error: { code: 'revision_conflict' } }
      drafts.set(draftKey, { revision: input.expectedRevision + 1, value: input.value })
      return ok({ revision: input.expectedRevision + 1 })
    }
    if (name === 'clientPlanList') return ok({ items: [] })
    return { ok: false, error: { code: 'unsupported_operation' } }
  })
  await page.addInitScript(() => {
    const call = (name: string, input?: unknown) =>
      (window as unknown as { desktopShellCall: (value: unknown) => Promise<unknown> }).desktopShellCall({ name, input })
    const methods = ['sourceList', 'sourceActivate', 'sourceRemove', 'workspaceOpen', 'centerConnect', 'hostStatus',
      'clientQuery', 'clientCommand', 'clientReceipt', 'clientReadDraft', 'clientSaveDraft', 'clientPlanList']
    window.ddpDesktop = Object.fromEntries(methods.map(name => [name, (input: unknown) => call(name, input)])) as never
    // The real host serves ddp://app/** from its protocol handler; here the same-origin
    // routes stand in for it (fetch for the API client, XHR for pdf.js).
    const local = (url: string) => (url.startsWith('ddp://app/') ? url.slice('ddp://app'.length) : url)
    const realFetch = window.fetch.bind(window)
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input)
      if (!url.startsWith('ddp://app/')) return realFetch(input, init)
      return realFetch(input instanceof Request ? new Request(local(url), input) : local(url), init)
    }
    const open = XMLHttpRequest.prototype.open as (...args: unknown[]) => void
    XMLHttpRequest.prototype.open = function (this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]) {
      return open.call(this, method, local(String(url)), ...rest)
    } as typeof XMLHttpRequest.prototype.open
  })
  return { calls, drafts, sources }
}

test('模型页仅查询状态，显式点击才发下载命令，结果未知时不自动重试', async ({ page }) => {
  const catalog = { runtime: { status: 'stopped' }, items: [{
    manifest: { id: 'qwen3-1.7b-q8_0', name: 'Qwen3 1.7B Q8_0', kind: 'model', bytes: 1834425344,
      license: 'Apache-2.0', runtime_id: 'llama-cpu', runtime_ids: ['llama-cpu'] },
    status: 'not_installed',
  }] }
  let lose = false
  const fixture = await desktopShell(page, { handle: (name, input) => {
    if (name === 'clientQuery' && input.name === 'models.list') return { ok: true, value: catalog }
    if (name === 'clientCommand') return lose ? { ok: false, error: { code: 'connection_failed' } } : { ok: true, value: {} }
    return undefined
  } })
  const errors = watchErrors(page)
  await page.goto('/#/models')
  await expect(page.getByRole('heading', { name: 'Qwen3 1.7B Q8_0' })).toBeVisible()
  await expect(page.locator('.model-row')).toContainText('Apache-2.0')
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(0)
  expect(fixture.calls.filter(call => call.name === 'clientQuery').map(call => call.input.name)).toEqual(['models.list'])

  lose = true
  await page.getByRole('button', { name: '下载并校验', exact: true }).click()
  // AppShell 的 profile 横幅与本页的错误各占一个 alert：只断言本页这一个。
  await expect(page.locator('.models').getByRole('alert')).toHaveText('连接暂不可用，已保留草稿。')
  const installs = fixture.calls.filter(call => call.name === 'clientCommand')
  expect(installs).toHaveLength(1)
  expect(installs[0]!.input).toMatchObject({ name: 'models.install', payload: { model_id: 'qwen3-1.7b-q8_0' } })
  // An unknown result keeps its operation key: the button stays disabled, nothing resends.
  await expect(page.getByRole('button', { name: '下载并校验', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '刷新状态', exact: true }).click()
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(1)
  expect(realErrors(errors)).toEqual([])
})

test('系统密钥库不可用时连接中心只用会话凭证，密码不进网页存储与草稿', async ({ page }) => {
  const password = 'synthetic-center-password-for-test'
  const fixture = await desktopShell(page, { handle: (name) =>
    name === 'centerConnect' ? { ok: false, error: { code: 'authentication_required' } } : undefined })
  const errors = watchErrors(page)
  await page.goto('/#/sources')
  await expect(page.getByText('当前环境没有可用的持久密钥库，登录只保留在本次会话。')).toBeVisible()
  await expect(page.getByRole('checkbox', { name: '保存登录' })).toBeDisabled()
  await page.getByLabel('中心地址').fill('https://center.example/team')
  await page.getByLabel('账号').fill('alice')
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '连接', exact: true }).click()
  // AppShell 的 profile 横幅与连接表单的错误各占一个 alert：只断言表单这一个。
  await expect(page.locator('.block').filter({ hasText: '连接中心' }).getByRole('alert')).toContainText('此身份需要重新认证。')
  const connect = fixture.calls.find(call => call.name === 'centerConnect')!
  expect(connect.input).toEqual({ endpoint: 'https://center.example/team', username: 'alice', password, persist: false })
  // The password goes to the host call only: not kept in the form, web storage or drafts.
  await expect(page.getByLabel('密码')).toHaveValue('')
  const webStorage = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))
  expect(webStorage).not.toContain(password)
  expect(JSON.stringify([...fixture.drafts])).not.toContain(password)
  expect(fixture.calls.filter(call => call.name !== 'centerConnect').map(call => JSON.stringify(call.input)).join('\n'))
    .not.toContain(password)
  expect(realErrors(errors)).toEqual([])
})

test('固定证据打开真实 PDF 预览，生成文本不能触发外部资源请求', async ({ page }) => {
  const fixture = await desktopShell(page)
  const errors = watchErrors(page), external: string[] = []
  page.on('request', request => { if (request.url().includes('outside.invalid')) external.push(request.url()) })
  const pdf = readFileSync(new URL('../../../tests/fixtures/contract.pdf', import.meta.url))
  const text = 'Industrial bearing 6204'
  const bbox = [61.092, 121.384, 181.668, 132.628], pageSize = [612, 792]
  const document = {
    id: 'local-doc', resource_id: 'local-resource', source_version_id: 'local-v1',
    filename: 'contract.pdf', doc_id: 'e'.repeat(64), origin: 'local', mime: 'application/pdf',
    size_bytes: pdf.length, page_count: 2, status: 'succeeded', error: null,
    index_status: 'ready', index_error: null, compile_status: 'ready', compile_degraded: [],
    compile_fingerprint: 'f'.repeat(64), layout_version: 'ddp-layout/1', code_detection: 'heuristic',
    current_job_id: 'local-parse', created_at: '2026-01-01T00:00:00Z', uploaders: ['owner'], can_delete: true,
  }
  const answer = `工作温度见原文。![leak](https://outside.invalid/leak.png) <img src="https://outside.invalid/raw.png">`
  let asked = false
  const sourceHeaders = { 'X-DDP-Source': 'local-0' }
  await page.route(url => url.pathname === '/api/search', route => route.fulfill({ headers: sourceHeaders, json: {
    query: 'bearing', degraded: null, groups: [{ document_id: document.id, resource_id: document.resource_id,
      source_version_id: document.source_version_id, parse_revision: document.current_job_id,
      filename: document.filename, hits: [{ chunk_id: 'local-2', page_idx: 1, bbox, snippet: text, score: 1, similarity: 0.9 }] }],
  } }))
  await page.route(url => url.pathname.startsWith('/api/documents/local-doc'), route => {
    const url = new URL(route.request().url())
    // Local runtime semantics: download-url is a same-origin relative path to the original bytes.
    if (url.pathname.endsWith('/source')) return route.fulfill({ headers: sourceHeaders, contentType: 'application/pdf', body: pdf })
    if (url.pathname.endsWith('/download-url')) return route.fulfill({ headers: sourceHeaders, json: { url: '/api/documents/local-doc/source' } })
    if (url.pathname.endsWith('/conversations')) return route.fulfill({ headers: sourceHeaders, json: { id: 'conv-1', title: '工作温度' } })
    if (url.pathname === '/api/documents/local-doc') return route.fulfill({ headers: sourceHeaders, json: document })
    if (url.pathname.endsWith('/result')) return route.fulfill({ headers: sourceHeaders, json: { document_id: document.id,
      job_id: document.current_job_id, filename: document.filename, page_count: 2, markdown: text, images: [] } })
    if (url.pathname.endsWith('/pages')) return route.fulfill({ headers: sourceHeaders, json: { document_id: document.id,
      job_id: document.current_job_id, page_count: 2, pages: [{ page_idx: 0, page_size: pageSize, blocks: [] },
        { page_idx: 1, page_size: pageSize, blocks: [{ page_idx: 1, page_size: pageSize, bbox, seq: 1, type: 'text',
          text, chunk_id: 'local-2', evidence_id: 'local-evidence' }] }] } })
    return route.fallback()
  })
  await page.route(url => url.pathname.startsWith('/api/conversations'), route => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/conversations/conv-1/ask') {
      asked = true
      const frames = [['meta', { query_decision: { mode: 'answer', reason: '' }, retrieval: { chunk_ids: [], candidates: [] } }],
        ['delta', { text: answer }], ['citations', { citations: [] }], ['assertions', { assertions: [] }],
        ['done', { message_id: 'm-2', verified: false, degraded: null, confidence: 'low' }]]
      return route.fulfill({ headers: sourceHeaders, contentType: 'text/event-stream',
        body: frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('') })
    }
    if (url.pathname === '/api/conversations/conv-1/messages') return route.fulfill({ headers: sourceHeaders, json: asked ? [
      { id: 'm-1', role: 'user', content: '工作温度', citations: [], verified: false, degraded: null, created_at: '2026-01-01T00:00:00Z' },
      { id: 'm-2', role: 'assistant', content: answer, citations: [], verified: false, degraded: null, created_at: '2026-01-01T00:00:01Z' },
    ] : [] })
    return route.fulfill({ headers: sourceHeaders, json: [] })
  })

  await page.goto('/#/search')
  await page.getByPlaceholder('在可访问的资源中检索').fill('bearing')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('link', { name: /Industrial bearing 6204/ }).click()
  await expect(page.locator('.pane.source canvas')).toBeVisible()
  await expect(page.locator('.pdf-canvas .box.selected')).toBeVisible()
  await page.getByPlaceholder(/Enter 发送/).fill('工作温度')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('工作温度见原文。')).toBeVisible()
  expect(asked).toBe(true)
  expect(external).toEqual([])
  // No bridge content read: the original came through the /api proxy path, not IPC.
  expect(fixture.calls.filter(call => call.name === 'clientQuery' || call.name === 'clientCommand')).toHaveLength(0)
  expect(realErrors(errors)).toEqual([])
})

test('切换数据源整页重载，草稿按数据源隔离，旧源的迟到响应不污染当前页', async ({ page }) => {
  const fixture = await desktopShell(page, { sources: [localSource('local-0', '甲的工作区', true),
    localSource('local-1', '乙的工作区', false)] })
  const errors = watchErrors(page)
  await page.goto('/#/tasks/new')
  await page.getByLabel('问题（批准后会原文发送给接收中心）').fill('甲的私有问题')
  await expect.poll(() => {
    const draft = fixture.drafts.get('local-0:federation-plan')?.value
    return draft && typeof draft === 'object' && 'query' in draft ? draft.query : undefined
  }).toBe('甲的私有问题')

  await page.goto('/#/sources')
  await page.locator('.source-row').filter({ hasText: '乙的工作区' }).getByRole('button', { name: '切换' }).click()
  await expect(page).toHaveURL(/#\/resources$/)
  await expect(page.locator('.egress')).toBeVisible()
  expect(fixture.sources.find(item => item.active)?.sourceId).toBe('local-1')

  // A response still stamped with the previous source is dropped, not rendered.
  await page.route(url => url.pathname === '/api/resources', route => route.fulfill({
    headers: { 'X-DDP-Source': 'local-0', 'Content-Type': 'application/json' },
    body: JSON.stringify({ has_more: false, items: [{ id: 'stale', organization_id: 'local', owner_id: 'owner',
      uploader_ref: { issuer: 'env-local-0', subject: 'owner' }, display_name: '不应出现的旧源资源', publication: 'private', versions: [] }] }),
  }))
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByText(/数据源已切换，此结果已丢弃/)).toBeVisible()
  await expect(page.getByText('不应出现的旧源资源')).toHaveCount(0)

  await page.goto('/#/tasks/new')
  await expect(page.getByLabel('问题（批准后会原文发送给接收中心）')).toHaveValue('')
  await page.goto('/#/sources')
  await page.locator('.source-row').filter({ hasText: '甲的工作区' }).getByRole('button', { name: '切换' }).click()
  await expect(page).toHaveURL(/#\/resources$/)
  await page.goto('/#/tasks/new')
  await expect(page.getByLabel('问题（批准后会原文发送给接收中心）')).toHaveValue('甲的私有问题')
  expect(realErrors(errors)).toEqual([])
})

test('中心源只读：文档表、会话与授权副本的写入口禁用并写明原因，页面发不出写请求', async ({ page }) => {
  const center = { ...localSource('center-0', '研究中心', true), kind: 'center', readOnly: true }
  await desktopShell(page, { sources: [center, localSource('local-0', '甲的工作区', false)] })
  const errors = watchErrors(page), writes: string[] = []
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/') && !['GET', 'HEAD'].includes(request.method()))
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`)
  })
  const document = {
    id: 'center-doc', resource_id: 'center-resource', source_version_id: 'center-v1', filename: '中心手册.pdf',
    doc_id: 'e'.repeat(64), origin: 'web', mime: 'application/pdf', size_bytes: 2048, page_count: 1,
    status: 'succeeded', error: null, index_status: 'ready', index_error: null, compile_status: 'ready',
    compile_degraded: [], compile_fingerprint: 'f'.repeat(64), layout_version: 'ddp-layout/1',
    code_detection: 'heuristic', current_job_id: 'center-parse', created_at: '2026-01-01T00:00:00Z',
    uploaders: ['alice'], can_delete: true,
  }
  // 真宿主的每个 /api 响应都带 `X-DDP-Source: <当前源>`（fail closed 围栏）。
  const centerHeaders = { 'X-DDP-Source': 'center-0' }
  await page.route(url => url.pathname === '/api/documents', route => route.fulfill({ headers: centerHeaders, json: [document] }))
  await page.route(url => url.pathname.endsWith('/documents/stats/summary'),
    route => route.fulfill({ headers: centerHeaders, json: { documents: 1, pages: 1, askable: 1 } }))
  const label = '中心在桌面里只读；写操作请作为联邦任务发起并批准'

  await page.goto('/#/documents')
  await expect(page.getByText('中心手册.pdf')).toBeVisible()
  await page.getByRole('button', { name: '更多' }).first().click()
  const menu = page.locator('.el-dropdown-menu:visible')
  await expect(menu.getByText(label)).toBeVisible()
  await expect(menu.getByText('删除', { exact: true })).toHaveCount(0)
  await expect(menu.getByText('重建索引')).toHaveCount(0)
  await expect(page.locator('.el-table__header .el-checkbox')).toHaveCount(0)

  await page.route(url => url.pathname === '/api/resources', route => route.fulfill({ headers: centerHeaders, json: { has_more: false, items: [{
    id: 'center-resource', organization_id: 'org-1', owner_id: 'u-1', uploader_ref: { issuer: 'node-center', subject: 'alice' },
    display_name: '中心手册.pdf', publication: 'private', versions: [{ id: 'center-v1', resource_id: 'center-resource',
      version_no: 1, document_id: 'center-doc', source_digest: 'a'.repeat(64), filename: '中心手册.pdf', size_bytes: 2048,
      parse_job_id: 'center-parse', parse_status: 'succeeded', index_status: 'ready' }] }] } }))
  await page.route(url => url.pathname.endsWith('/bundle/replicas'), route => route.fulfill({ headers: centerHeaders, json: {
    replicas: [{ replica_id: 'replica-1', availability: 'available' }] } }))
  await page.goto('/#/resources')
  await page.getByRole('button', { name: '授权副本' }).first().click()
  await expect(page.getByText('replica-1')).toBeVisible()
  await expect(page.getByRole('button', { name: '撤销副本' })).toBeDisabled()

  expect(writes).toEqual([])
  expect(realErrors(errors)).toEqual([])
})
