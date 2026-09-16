import { expect, test, type Page } from '@playwright/test'
import { realErrors, watchErrors } from './console-guard'

test('无桌面宿主的未登录浏览器只能看到配对入口，不请求或展示任何工作区资料', async ({ page }) => {
  const errors = watchErrors(page), requests: string[] = []
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request.url()) })
  await page.goto('/#/workspaces')
  await expect(page.getByText('此浏览器页面尚未配对执行环境。', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '生成回答', exact: true })).toHaveCount(0)
  expect(requests).toEqual([])
  await page.getByRole('link', { name: '打开本站资源库' }).click()
  await expect(page).toHaveURL(/#\/login\?redirect=/)
  expect(realErrors(errors)).toEqual([])
})

async function desktopFixture(page: Page, imported = false, options: { center?: boolean; windows?: boolean; wiki?: boolean; plans?: boolean } = {}) {
  const drafts = new Map<string, { revision: number; value: unknown }>()
  const calls: { name: string; input: Record<string, unknown> }[] = []
  const subscriptions = new Map<string, string>()
  // A real PDF with calculated xref offsets; malformed legacy fixture offsets
  // make PDF.js repair it and hide defects behind console allow-lists.
  const stream = 'BT /F1 16 Tf 50 760 Td (Controller operating temperature: 40 C.) Tj ET'
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`]
  let source = '%PDF-1.4\n'; const offsets = [0]
  objects.forEach((object, index) => { offsets.push(Buffer.byteLength(source)); source += `${index + 1} 0 obj\n${object}\nendobj\n` })
  const xref = Buffer.byteLength(source)
  source += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => String(offset).padStart(10, '0') + ' 00000 n \n').join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  const pdf = Array.from(Buffer.from(source))
  const summaries = ['甲', '乙'].map((label, i) => ({
    connectionId: `connection-${i}`, kind: options.center ? 'remote' : 'local', label: `${label}的工作区`, workspaceId: `handle-${i}`,
    environment: { environmentId: `environment-${i}`, workspaceId: `workspace-${i}`, authorityNodeId: `node-${i}` },
    profile: { profileId: `profile-${i}`, issuer: `node-${i}`, subject: `owner-${i}` }, revision: 0,
    view: { transport: 'ready', snapshot: 'current', reason: null, projection: { cursor: 'cursor-0', sequence: 0,
      state: { resources: [{ id: options.center ? `resource-${i}` : `version-${i}`, resource_id: `resource-${i}`, version_id: `version-${i}`, filename: `${label}的技术手册.pdf`, state: 'ready',
        ...(options.plans ? { source_digest: 'a'.repeat(64), size_bytes: 2048 } : {}) }], tasks: [], capabilities: { generation: { available: false } },
        ...(options.windows ? { snapshot_id: `snapshot-${i}`, cache_complete: false, windows: { resources: { visible_total: 2, items_loaded: 1, has_more: true, next_cursor: 'next-1' } } } : {}) } } },
  }))
  // A paired, identity-proven center that local plans can name as their only recipient.
  const centerSummary = { connectionId: 'connection-center', kind: 'remote', label: '研究中心', workspaceId: null,
    environment: { environmentId: 'node-center', workspaceId: 'org-1', authorityNodeId: 'node-center' },
    profile: { profileId: 'profile-center', issuer: 'node-center', subject: 'user-alice' }, revision: 0,
    view: { transport: 'ready', snapshot: 'current', reason: null, projection: { cursor: 'c-0', sequence: 0, state: { resources: [], tasks: [] } } } }
  if (options.plans) summaries.push(centerSummary as never)
  // Local runtime plan ledger double: the same refusals the host relies on (approval per
  // phase bound to the scope digest, verified-only confirmation). Real rules: ddp_local tests.
  type Plan = Record<string, any>
  const planLedger = { plans: new Map<string, Plan>(), federation: new Map<string, Plan>(), receipts: new Map<string, () => unknown>(),
    tamper: false, loseDispatch: false, cancelApproval: false }
  const planDetail = (id: string) => {
    const federation = planLedger.federation.get(id) ?? null, delivery = federation?.delivery
    const expected = delivery?.verified ? delivery.result_manifest_digest : null
    return { plan: planLedger.plans.get(id), federation, verification: { state: expected ? (planLedger.tamper ? 'failed' : 'passed') : 'unavailable',
      expected, actual: expected ? (planLedger.tamper ? 'sha256:' + 'e'.repeat(64) : expected) : null } }
  }
  let loseReply = false
  let wikiCurrent = 'revision-1'
  const wikiRevisions = new Map<string, Record<string, unknown>>([['revision-1', {
    id: 'revision-1', title: '控制器说明', pages: [{ page_key: 'controller', title: '控制器',
      generated_sections: [{ heading: '工作条件', sentences: [{ id: 'sentence-1', text: '工作温度为40°C。', evidence_ids: ['evidence-0'] }] }],
      human_paragraphs: [{ id: 'human-1', text: '现场复核第一版。' }] }], relations: [], dependency_manifest: [], stale: false,
  }]])
  const wikiDocument = (id = wikiCurrent) => ({ wiki: { id: 'wiki-1', title: '控制器说明', current_revision_id: wikiCurrent, published_revision_id: null }, revision: wikiRevisions.get(id) })
  await page.exposeBinding('desktopTestCall', async (_source, { name, input = {} }) => {
    calls.push({ name, input })
    const summary = summaries.find(item => item.connectionId === input.connectionId)!
    const ok = (value: unknown) => ({ ok: true, value })
    if (name === 'clientList') return ok(summaries)
    if (name === 'hostStatus') return ok({ secrets: { backend: 'basic_text', persistentAvailable: false }, lifecycle: 'close_stops_owned_local_tasks' })
    if (name === 'clientSubscribe') { subscriptions.set(input.subscriptionId, input.connectionId); return ok(summary) }
    if (name === 'clientUnsubscribe') { subscriptions.delete(input.subscriptionId); return ok(null) }
    const draftKey = input.key && input.key !== 'workspace' ? input.connectionId + ':' + input.key : input.connectionId
    if (name === 'clientReadDraft') return ok(drafts.get(draftKey) ?? null)
    if (name === 'clientSaveDraft') {
      const previous = drafts.get(draftKey)
      if ((previous?.revision ?? 0) !== input.expectedRevision) return { ok: false, error: { code: 'revision_conflict' } }
      drafts.set(draftKey, { revision: input.expectedRevision + 1, value: input.value }); return ok({ revision: input.expectedRevision + 1 })
    }
    if (name === 'clientWake' || name === 'clientDisconnect') return ok(summary)
    if (name === 'setCredential') return ok({ stored: 'session' })
    if (name === 'clientPairRemote') return ok(options.plans ? centerSummary : { ...summaries[0], kind: 'remote', label: input.label })
    if (name.startsWith('clientPlan')) {
      const fail = (code: string) => ({ ok: false, error: { code } })
      const plan = planLedger.plans.get(input.planId)
      if (name === 'clientPlanPropose') {
        if (planLedger.receipts.has(input.idempotencyKey)) return ok(planLedger.receipts.get(input.idempotencyKey)!())
        const id = 'plan-' + (planLedger.plans.size + 1), bytes = Buffer.byteLength(input.query)
        const payload = { recipient_node_id: 'node-center', payload_kind: 'query_text', size_bytes: bytes, digest: 'sha256:' + 'b'.repeat(64), transport_ref: 'center' }
        planLedger.plans.set(id, { plan_id: id, scope_digest: 'sha256:' + 'd'.repeat(64), planning_state: 'ready', revoked: false, consents: {},
          scope: { task_spec: { query: input.query }, retention: input.retention, output_locations: ['local:workspace-0'],
            input_manifest: input.inputs.map((item: Plan) => ({ ref: item.ref, digest: item.digest, size_bytes: item.sizeBytes })),
            payload_bindings: [{ payload_id: 'exploration-query', phase: 'exploration', ...payload }, { payload_id: 'execution-query', phase: 'execution', edge_id: 'edge-query', ...payload }],
            transport_bindings: [{ transport_ref: 'center', recipient_node_id: 'node-center', environment_id: 'node-center', workspace_id: 'org-1',
              profile_id: 'profile-center', issuer: 'node-center', subject: 'user-alice', endpoint: 'https://center.example/team' }],
            plan: { plan_id: id, plan_digest: 'sha256:' + 'c'.repeat(64), valid_until: '2030-01-01T00:00:00Z',
              budget: { max_requests: 4, max_bytes: bytes * 4 }, data_edges: [{ edge_id: 'edge-query', from_node_id: 'local-env-0', to_node_id: 'node-center', payload_kind: 'query_text', retention: input.retention }] },
            exploration: { budget: { max_probe_requests: 2, max_egress_bytes: bytes * 2 } } } })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.plans.get(id))
        return ok(planLedger.plans.get(id))
      }
      if (name === 'clientPlanList') return ok({ visible_total: planLedger.plans.size, items: [...planLedger.plans.values()].map(item => ({
        plan_id: item.plan_id, planning_state: item.planning_state,
        federation: planLedger.federation.has(item.plan_id) ? { state: planLedger.federation.get(item.plan_id)!.state, delivery_state: planLedger.federation.get(item.plan_id)!.delivery?.state ?? null } : null })) })
      if (!plan) return fail('not_found')
      const federation = planLedger.federation.get(plan.plan_id)
      if (name === 'clientPlanGet') return ok(planDetail(plan.plan_id))
      if (name === 'clientPlanApprove') {
        if (input.userConfirmed !== true || input.scopeDigest !== plan.scope_digest) return fail('plan_changed')
        if (planLedger.cancelApproval) { planLedger.cancelApproval = false; return fail('approval_cancelled') }
        plan.consents[input.phase] = { consent_id: 'consent-' + input.phase }
        plan.planning_state = plan.consents.execution ? 'approved' : 'exploring'
        return ok(plan)
      }
      if (name === 'clientPlanDispatch') {
        if (plan.revoked || !plan.consents[input.phase]) return fail('approved_plan_required')
        planLedger.federation.set(plan.plan_id, { ...(federation ?? {}), plan_id: plan.plan_id, root_task_id: 'root-1', center_plan_digest: plan.scope.plan.plan_digest,
          state: input.phase === 'exploration' ? 'planned' : 'submitted', reconcile: { at: 1789000000 }, delivery: null })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.federation.get(plan.plan_id))
        if (planLedger.loseDispatch) { planLedger.loseDispatch = false; return fail('outcome_unknown') }
        return ok(planLedger.federation.get(plan.plan_id))
      }
      if (name === 'clientPlanReconcile') {
        if (federation?.state === 'submitted') planLedger.federation.set(plan.plan_id, { ...federation, state: 'succeeded', delivery: { id: 'delivery-1', state: 'pending' } })
        return ok(planDetail(plan.plan_id))
      }
      if (name === 'clientPlanFetchDelivery') {
        planLedger.federation.set(plan.plan_id, { ...federation, delivery: { ...federation!.delivery, verified: true, result_manifest_digest: 'sha256:' + 'f'.repeat(64),
          result: { schema: 'ddp-answer/1', answer: '控制器工作温度为 40°C。' } } })
        return ok(planDetail(plan.plan_id))
      }
      if (name === 'clientPlanConfirmDelivery') {
        const detail = planDetail(plan.plan_id)
        if (detail.verification.state !== 'passed' || input.resultManifestDigest !== federation?.delivery?.result_manifest_digest) return fail('delivery_unverified')
        planLedger.federation.set(plan.plan_id, { ...federation, delivery: { ...federation!.delivery, state: 'confirmed' } })
        return ok(planLedger.federation.get(plan.plan_id))
      }
    }
    if (name === 'clientQuery') {
      if (input.name === 'wiki.list') return ok({ items: options.wiki ? [{ wiki: wikiDocument().wiki, revision: { id: wikiCurrent, page_count: 1 } }] : [], visible_total: options.wiki ? 1 : 0, has_more: false, next_cursor: null })
      if (input.name === 'wiki.get') return ok(wikiDocument(input.payload.revision_id))
      if (input.name === 'wiki.revisions') return ok({ items: [...wikiRevisions.keys()].reverse().map(id => ({ id, created_at: '2026-09-13' })), visible_total: wikiRevisions.size, has_more: false, next_cursor: null })
      if (input.name === 'models.list') return ok({ catalog_revision: 'fixture', runtime: { status: 'stopped' }, items: [{ manifest: {
        id: 'qwen3-1.7b-q8_0', name: 'Qwen CPU', kind: 'model', bytes: 1834426016, license: 'Apache-2.0', device: 'cpu',
      }, status: 'not_installed', downloaded_bytes: 0 }] })
      if (input.name === 'resource.page') return ok({ snapshot_id: 'snapshot-0', sequence: 0, kind: 'resources',
        items: [{ id: 'resource-next', version_id: 'version-next', filename: '下一页资料.pdf', state: 'ready' }],
        next_cursor: null, has_more: false, visible_total: 2, items_loaded: 1, page_index: 1 })
      if (input.name === 'corpus.search') return ok({ hits: [{ evidence_id: 'evidence-0', text: '控制器的工作温度为 40°C。' }] })
      return ok({ ...(imported ? { version_id: 'version-0' } : {}), evidence: { evidence_id: 'evidence-0',
        source_version_id: imported ? 'origin-version-42' : 'version-0', origin_node_id: 'origin-node', source_digest: 'd'.repeat(64),
        locator: { physical_page_index: 0, seq: 0, bbox: [30, 30, 250, 80], page_size: { width: 595, height: 842 } } } })
    }
    if (name === 'clientReadOriginal') return ok(pdf)
    if (name === 'clientCommand') {
      if (loseReply) return { ok: false, error: { code: 'outcome_unknown' } }
      if (input.name === 'wiki.edit') {
        if (input.payload.body.base_revision_id !== wikiCurrent) return { ok: false, error: { code: 'revision_conflict' } }
        const previous = structuredClone(wikiRevisions.get(wikiCurrent)!) as { id: string; pages: { human_paragraphs: unknown[] }[] }
        wikiCurrent = 'revision-' + (wikiRevisions.size + 1); previous.id = wikiCurrent
        previous.pages[0]!.human_paragraphs = input.payload.body.paragraphs; wikiRevisions.set(wikiCurrent, previous)
        return ok({ ...wikiDocument(), task_id: 'wiki-task' })
      }
      return ok({ answer: '<img src="https://outside.invalid/private-question"> 控制器工作温度为40°C。[1]', evidence: [] })
    }
    if (name === 'clientReceipt') return ok(planLedger.receipts.get(input.idempotencyKey)?.() ?? null)
    throw new Error(`Unexpected fixture operation ${name}`)
  })
  await page.addInitScript(() => {
    const listeners = new Set<(event: unknown) => void>()
    const call = (name: string, input?: unknown) => (window as unknown as { desktopTestCall: (value: unknown) => Promise<unknown> }).desktopTestCall({ name, input })
    const methods = ['clientList', 'hostStatus', 'clientSubscribe', 'clientUnsubscribe', 'clientReadDraft',
      'clientSaveDraft', 'clientWake', 'clientDisconnect', 'clientQuery', 'clientCommand', 'clientReceipt', 'clientReadOriginal', 'setCredential', 'clientPairRemote',
      'clientPlanPropose', 'clientPlanList', 'clientPlanGet', 'clientPlanApprove', 'clientPlanRevoke', 'clientPlanDispatch',
      'clientPlanReconcile', 'clientPlanFetchDelivery', 'clientPlanConfirmDelivery']
    window.ddpDesktop = Object.fromEntries(methods.map(name => [name, (input: unknown) => call(name, input)])) as never
    window.ddpDesktop!.onClientView = listener => { listeners.add(listener as never); return () => listeners.delete(listener as never) }
    ;(window as unknown as { emitDesktopView: (event: unknown) => void }).emitDesktopView = event => listeners.forEach(listener => listener(event))
  })
  return { drafts, calls, summaries, subscriptions, planLedger, loseNextReply: () => { loseReply = true } }
}

test('桌面根路径进入业务工作台，身份切换恢复各自草稿且旧订阅不能污染当前状态', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/')
  await expect(page).toHaveURL(/#\/workspaces$/)
  await expect(page.getByRole('button', { name: '甲的技术手册.pdf' })).toBeVisible()
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await page.getByLabel('问题或检索词').fill('甲的私有问题')
  await expect.poll(() => fixture.drafts.get('connection-0')?.value).toMatchObject({ question: '甲的私有问题' })
  const [oldSubscription] = [...fixture.subscriptions.keys()]
  await page.getByRole('button', { name: '乙的工作区' }).click()
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await expect(page.getByLabel('问题或检索词')).toHaveValue('')
  await page.getByLabel('问题或检索词').fill('乙的私有问题')
  await page.evaluate(event => (window as unknown as { emitDesktopView: (value: unknown) => void }).emitDesktopView(event), {
    subscriptionId: oldSubscription, connectionId: 'connection-0', revision: 999,
    view: { transport: 'ready', snapshot: 'current', reason: null, projection: { cursor: 'poison', sequence: 999, state: { resources: [{ filename: '不应出现的旧身份资源' }] } } },
  })
  await expect(page.getByText('不应出现的旧身份资源')).toHaveCount(0)
  await page.getByRole('button', { name: '甲的工作区' }).click()
  await expect(page.getByLabel('问题或检索词')).toHaveValue('甲的私有问题')
  expect(fixture.subscriptions.size).toBe(1)
  expect(realErrors(errors)).toEqual([])
})

test('提交回执不明时保留操作键，断线恢复不重复生成，输入仍可编辑', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  fixture.loseNextReply(); await page.goto('/#/workspaces')
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await page.getByLabel('问题或检索词').fill('查阅工作温度')
  await page.getByRole('button', { name: '生成回答', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('提交结果未确认')
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(1)
  await page.getByRole('button', { name: '查询回执', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('尚未找到回执')
  const [subscriptionId] = [...fixture.subscriptions.keys()]
  await page.evaluate(event => (window as unknown as { emitDesktopView: (value: unknown) => void }).emitDesktopView(event), {
    subscriptionId, connectionId: 'connection-0', revision: 1, view: { ...fixture.summaries[0]!.view, transport: 'disconnected', snapshot: 'stale' },
  })
  await page.getByLabel('问题或检索词').fill('断线后继续编辑')
  await page.getByRole('button', { name: '重新连接', exact: true }).click()
  await page.reload()
  await expect(page.getByLabel('问题或检索词')).toHaveValue('断线后继续编辑')
  await expect(page.getByRole('button', { name: '查询回执', exact: true })).toBeVisible()
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(1)
  expect(realErrors(errors)).toEqual([])
})

test('固定证据打开真实 PDF 预览，生成文本不能触发外部资源请求', async ({ page }, info) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page), external: string[] = []
  page.on('request', request => { if (request.url().includes('outside.invalid')) external.push(request.url()) })
  await page.goto('/#/workspaces')
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await page.getByLabel('问题或检索词').fill('工作温度')
  await page.getByRole('button', { name: '检索证据', exact: true }).click()
  await page.getByRole('button', { name: '查看原始出处', exact: true }).click()
  await expect(page.locator('.source-panel canvas')).toBeVisible()
  await expect(page.locator('.source-panel .source-id')).toHaveText('version-0')
  await page.getByRole('button', { name: '生成回答', exact: true }).click()
  await expect(page.locator('.generated-text')).toContainText('<img src=')
  expect(external).toEqual([])
  await page.screenshot({ path: info.outputPath('environment-workspace.png'), fullPage: true, animations: 'disabled' })
  expect(fixture.calls.some(call => call.name === 'clientReadOriginal' && call.input.versionId === 'version-0')).toBe(true)
  expect(realErrors(errors)).toEqual([])
})

test('导入资料包的证据保留原始身份，但原件读取使用当前工作区的副本版本', async ({ page }) => {
  const fixture = await desktopFixture(page, true), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await page.getByLabel('问题或检索词').fill('工作温度')
  await page.getByRole('button', { name: '检索证据', exact: true }).click()
  await page.getByRole('button', { name: '查看原始出处', exact: true }).click()
  await expect(page.locator('.source-panel canvas')).toBeVisible()
  await expect(page.locator('.source-provenance')).toContainText('origin-version-42')
  await expect(page.locator('.source-provenance')).toContainText('origin-node')
  expect(fixture.calls.filter(call => call.name === 'clientReadOriginal').map(call => call.input.versionId)).toEqual(['version-0'])
  expect(realErrors(errors)).toEqual([])
})

test('模型页仅查询状态，显式下载记录稳定操作键，未知回执不会因刷新再次下载', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  expect(fixture.calls.filter(call => call.input.name === 'models.install')).toHaveLength(0)
  await page.getByRole('button', { name: '本地模型', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Qwen CPU' })).toBeVisible()
  await expect(page.locator('.model-row')).toContainText('Apache-2.0')
  await expect(page.locator('.model-row')).toContainText('1749.4 MiB')
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(0)
  fixture.loseNextReply()
  await page.getByRole('button', { name: '下载并校验', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('提交结果未确认')
  const download = fixture.calls.find(call => call.input.name === 'models.install')!
  expect(download.input.payload).toEqual({ model_id: 'qwen3-1.7b-q8_0' })
  expect(fixture.drafts.get('connection-0')?.value).toMatchObject({ pendingKey: download.input.idempotencyKey })
  await page.reload()
  await expect(page.getByRole('button', { name: '查询回执', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '下载并校验', exact: true })).toBeDisabled()
  expect(fixture.calls.filter(call => call.input.name === 'models.install')).toHaveLength(1)
  expect(realErrors(errors)).toEqual([])
})

test('中心分页替换有界窗口，原文和检索都使用固定版本，外发确认不沿用到新问题', async ({ page }) => {
  const fixture = await desktopFixture(page, false, { center: true, windows: true }), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  await page.getByRole('button', { name: '下一页资源', exact: true }).click()
  await expect(page.getByRole('button', { name: '下一页资料.pdf' })).toBeVisible()
  await expect(page.getByRole('button', { name: '甲的技术手册.pdf' })).toHaveCount(0)
  expect(fixture.calls.find(call => call.input.name === 'resource.page')?.input.payload).toEqual({ snapshot_id: 'snapshot-0', cursor: 'next-1' })
  await page.getByRole('button', { name: '下一页资料.pdf' }).click()
  await expect(page.locator('.source-panel canvas')).toBeVisible()
  expect(fixture.calls.find(call => call.name === 'clientReadOriginal')?.input.versionId).toBe('version-next')
  await page.getByRole('button', { name: '问答', exact: true }).click()
  await page.getByLabel('问题或检索词').fill('中心问题')
  await expect(page.getByRole('button', { name: '检索证据', exact: true })).toBeDisabled()
  expect(fixture.calls.filter(call => call.input.name === 'corpus.search')).toHaveLength(0)
  await page.getByRole('checkbox', { name: /允许将当前检索词/ }).check()
  await page.getByRole('button', { name: '检索证据', exact: true }).click()
  await expect(page.getByRole('button', { name: '查看原始出处', exact: true })).toBeVisible()
  expect(fixture.calls.find(call => call.input.name === 'corpus.search')?.input.payload).toEqual({ query: '中心问题', version_ids: ['version-next'] })
  await page.getByLabel('问题或检索词').fill('另一个问题')
  await expect(page.getByRole('checkbox', { name: /允许将当前检索词/ })).not.toBeChecked()
  await expect(page.getByRole('button', { name: '检索证据', exact: true })).toBeDisabled()
  expect(realErrors(errors)).toEqual([])
})

test('系统密钥库不可用时中心配对只用会话凭证，秘密不进入工作区草稿', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  await page.getByRole('button', { name: '配对中心…', exact: true }).click()
  await page.getByLabel('名称', { exact: true }).fill('研究中心')
  await page.getByLabel('HTTPS 地址', { exact: true }).fill('https://center.example/team')
  await page.getByLabel('节点身份', { exact: true }).fill('node-' + 'a'.repeat(48))
  await page.getByLabel('工作区编号', { exact: true }).fill('organization-1')
  await page.getByLabel('用户编号', { exact: true }).fill('subject-1')
  await page.getByLabel('API 凭证', { exact: true }).fill('synthetic-private-token-for-test')
  await expect(page.getByRole('checkbox', { name: '保存在系统密钥库' })).toBeDisabled()
  await page.getByRole('button', { name: '验证并连接', exact: true }).click()
  await expect(page.getByRole('heading', { name: '配对中心', exact: true })).toHaveCount(0)
  expect(fixture.calls.find(call => call.name === 'setCredential')?.input.persist).toBe(false)
  expect(fixture.calls.find(call => call.name === 'clientPairRemote')?.input.environment).toMatchObject({ endpoint: 'https://center.example/team' })
  expect(JSON.stringify([...fixture.drafts])).not.toContain('synthetic-private-token-for-test')
  // 网页存储（T21）：秘密只许进宿主的会话凭证通道，不许落进渲染进程能读到的存储。
  const webStorage = await page.evaluate(() => JSON.stringify([
    Object.entries(localStorage), Object.entries(sessionStorage)]))
  expect(webStorage).not.toContain('synthetic-private-token-for-test')
  expect(realErrors(errors)).toEqual([])
})

test('Wiki 编辑草稿跨刷新保留，保存基于固定修订，历史查看不会覆盖人工内容', async ({ page }, info) => {
  const fixture = await desktopFixture(page, false, { wiki: true }), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  await page.getByRole('button', { name: 'Wiki', exact: true }).click()
  await page.getByRole('button', { name: '控制器说明 1 页' }).click()
  await expect(page.getByLabel('人工编辑草稿', { exact: true })).toHaveValue('现场复核第一版。')
  await page.getByLabel('人工编辑草稿', { exact: true }).fill('保留到重启后的人工补充。')
  await page.reload()
  await expect(page.getByLabel('人工编辑草稿', { exact: true })).toHaveValue('保留到重启后的人工补充。')
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(0)
  await page.getByRole('button', { name: '保存人工编辑为新修订', exact: true }).click()
  await expect(page.locator('.human-paragraphs')).toContainText('保留到重启后的人工补充。')
  expect(fixture.calls.find(call => call.input.name === 'wiki.edit')?.input.payload).toEqual({ wiki_id: 'wiki-1', page_key: 'controller', body: { base_revision_id: 'revision-1', paragraphs: [{ id: 'human-1', text: '保留到重启后的人工补充。' }] } })
  await page.screenshot({ path: info.outputPath('local-wiki-editor.png'), fullPage: true, animations: 'disabled' })
  await page.getByLabel('历史修订', { exact: true }).selectOption('revision-1')
  await expect(page.locator('.human-paragraphs')).toContainText('现场复核第一版。')
  await expect(page.getByText('正在查看历史修订；', { exact: false })).toBeVisible()
  await page.getByLabel('人工编辑草稿', { exact: true }).fill('不能直接覆盖新版')
  await expect(page.getByRole('button', { name: '保存人工编辑为新修订', exact: true })).toBeDisabled()
  expect(fixture.calls.filter(call => call.name === 'clientCommand')).toHaveLength(1)
  expect(realErrors(errors)).toEqual([])
})

const PLAN_SECRET = 'synthetic-center-token-for-plan-e2e'
async function proposePlan(page: Page) {
  await page.getByRole('button', { name: '远端计划', exact: true }).click()
  await page.getByLabel('接收方（已配对中心）').selectOption('connection-center')
  await page.getByLabel('检索词（批准后会原文发送给该中心）').fill('控制器工作温度是多少？')
  await page.getByRole('checkbox', { name: /甲的技术手册\.pdf/ }).check()
  await page.getByRole('button', { name: '生成待审阅计划', exact: true }).click()
  const review = page.getByRole('article', { name: '计划审阅' })
  await expect(review).toBeVisible()
  return review
}

test('远端计划主路径：审阅实际外发内容，分阶段批准后派发、对账、本地重算交付摘要再确认，凭证不进网页存储', async ({ page }, info) => {
  const fixture = await desktopFixture(page, false, { plans: true }), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  // Pair the center first so a real secret exists in this session to look for later.
  await page.getByRole('button', { name: '配对中心…', exact: true }).click()
  await page.getByLabel('名称', { exact: true }).fill('研究中心')
  await page.getByLabel('HTTPS 地址', { exact: true }).fill('https://center.example/team')
  await page.getByLabel('节点身份', { exact: true }).fill('node-center')
  await page.getByLabel('工作区编号', { exact: true }).fill('org-1')
  await page.getByLabel('用户编号', { exact: true }).fill('user-alice')
  await page.getByLabel('API 凭证', { exact: true }).fill(PLAN_SECRET)
  await page.getByRole('button', { name: '验证并连接', exact: true }).click()
  await page.getByRole('button', { name: /甲的工作区/ }).click()

  const review = await proposePlan(page)
  const proposal = fixture.calls.find(call => call.name === 'clientPlanPropose')!.input
  expect(proposal).toEqual({ connectionId: 'connection-0', centerConnectionId: 'connection-center', query: '控制器工作温度是多少？',
    inputs: [{ ref: 'version-0', digest: 'sha256:' + 'a'.repeat(64), sizeBytes: 2048 }], retention: 'temporary', validMinutes: 120,
    idempotencyKey: expect.stringMatching(/^[A-Za-z0-9_-]{8,128}$/) })
  // The review shows what would actually leave this machine, to whom, and under which limits.
  const outgoing = review.getByRole('table').first()
  await expect(outgoing.getByRole('row')).toHaveCount(3)
  await expect(outgoing).toContainText('探索'); await expect(outgoing).toContainText('执行')
  await expect(outgoing).toContainText('检索词原文'); await expect(outgoing).toContainText('node-center')
  await expect(outgoing).toContainText(String(Buffer.byteLength('控制器工作温度是多少？')))
  await expect(review).toContainText('检索词原文：控制器工作温度是多少？')
  await expect(review).toContainText('https://center.example/team')
  await expect(review).toContainText('local-env-0 → node-center')
  await expect(review).toContainText('version-0')
  await expect(review).toContainText('临时（任务结束即可清理）')
  await expect(review).toContainText('local:workspace-0')
  await expect(review).toContainText('2030-01-01T00:00:00Z')
  await expect(review.locator('.budget')).toContainText('4')
  await expect(review.getByRole('button', { name: '派发探索', exact: true })).toBeDisabled()
  await expect(review.getByRole('button', { name: '批准执行…', exact: true })).toBeDisabled()

  await review.getByRole('button', { name: '批准探索…', exact: true }).click()
  await expect(review).toContainText('已批准探索')
  expect(fixture.calls.find(call => call.name === 'clientPlanApprove')!.input).toMatchObject({ planId: 'plan-1', phase: 'exploration',
    scopeDigest: 'sha256:' + 'd'.repeat(64), userConfirmed: true })
  await review.getByRole('button', { name: '派发探索', exact: true }).click()
  await expect(review).toContainText('中心计划已就绪')
  await expect(review.getByRole('button', { name: '派发执行', exact: true })).toBeDisabled()
  await review.getByRole('button', { name: '批准执行…', exact: true }).click()
  await review.getByRole('button', { name: '派发执行', exact: true }).click()
  await expect(review).toContainText('中心执行中')
  await review.getByRole('button', { name: '对账', exact: true }).click()
  await expect(review).toContainText('中心已完成')
  await expect(review).toContainText('待取回确认')
  await expect(review.getByRole('button', { name: '确认交付', exact: true })).toBeDisabled()
  await review.getByRole('button', { name: '取回交付并校验', exact: true }).click()
  await expect(review).toContainText('本地重算摘要一致')
  await expect(review).toContainText('中心生成内容 · 待复核')
  await review.getByRole('button', { name: '确认交付', exact: true }).click()
  await expect(review).toContainText('已确认交付')
  await expect(page.getByRole('button', { name: /plan-1/ })).toContainText('已确认交付')
  await page.screenshot({ path: info.outputPath('federation-plan-review.png'), fullPage: true, animations: 'disabled' })

  expect(fixture.calls.filter(call => call.name === 'clientPlanConfirmDelivery')).toHaveLength(1)
  // The renderer never names an endpoint or a credential for any plan operation.
  for (const call of fixture.calls.filter(call => call.name.startsWith('clientPlan')))
    expect(Object.keys(call.input).some(key => ['endpoint', 'credential', 'secret', 'url', 'path'].includes(key))).toBe(false)
  expect(JSON.stringify(fixture.calls.filter(call => call.name !== 'setCredential'))).not.toContain(PLAN_SECRET)
  expect(JSON.stringify([...fixture.drafts])).not.toContain(PLAN_SECRET)
  const webStorage = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))
  expect(webStorage).not.toContain(PLAN_SECRET)
  expect(realErrors(errors)).toEqual([])
})

test('远端计划失败路径：取消批准不授权，回执未知保留编号并在刷新后从本机镜像恢复，摘要不一致不许确认', async ({ page }) => {
  const fixture = await desktopFixture(page, false, { plans: true }), errors = watchErrors(page)
  await page.goto('/#/workspaces')
  let review = await proposePlan(page)
  fixture.planLedger.cancelApproval = true
  await review.getByRole('button', { name: '批准探索…', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('已取消批准，没有授予任何外发许可。')
  await expect(review.getByRole('button', { name: '派发探索', exact: true })).toBeDisabled()
  await expect(page.getByText('有一项计划操作结果未知', { exact: false })).toHaveCount(0)

  await review.getByRole('button', { name: '批准探索…', exact: true }).click()
  fixture.planLedger.loseDispatch = true
  await review.getByRole('button', { name: '派发探索', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('提交结果未确认')
  await expect(page.getByText('有一项计划操作结果未知', { exact: false })).toBeVisible()
  await expect(review.getByRole('button', { name: '批准执行…', exact: true })).toBeDisabled()
  const dispatches = fixture.calls.filter(call => call.name === 'clientPlanDispatch')
  expect(dispatches).toHaveLength(1)
  const unknownKey = dispatches[0]!.input.idempotencyKey
  expect(fixture.drafts.get('connection-0:federation-plan')?.value).toMatchObject({ planKey: unknownKey, selectedPlan: 'plan-1' })

  // Restart of the renderer: the accepted task is recovered from the local mirror, not re-sent.
  await page.reload()
  review = page.getByRole('article', { name: '计划审阅' })
  await expect(review).toBeVisible()
  await expect(page.getByRole('button', { name: /plan-1/ })).toContainText('中心计划已就绪')
  await expect(review).toContainText('root-1')
  await expect(page.getByText('有一项计划操作结果未知', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '查询回执', exact: true }).click()
  await expect(page.getByText('有一项计划操作结果未知', { exact: false })).toHaveCount(0)
  expect(fixture.calls.filter(call => call.name === 'clientPlanDispatch')).toHaveLength(1)
  expect(fixture.calls.find(call => call.name === 'clientReceipt')?.input.idempotencyKey).toBe(unknownKey)

  await review.getByRole('button', { name: '批准执行…', exact: true }).click()
  await review.getByRole('button', { name: '派发执行', exact: true }).click()
  await review.getByRole('button', { name: '对账', exact: true }).click()
  fixture.planLedger.tamper = true
  await review.getByRole('button', { name: '取回交付并校验', exact: true }).click()
  await expect(review).toContainText('本地重算摘要不一致')
  await expect(review).toContainText('sha256:' + 'e'.repeat(64))
  await expect(review.getByRole('button', { name: '确认交付', exact: true })).toBeDisabled()
  await expect(review.getByText('中心生成内容 · 待复核')).toHaveCount(0)
  expect(fixture.calls.filter(call => call.name === 'clientPlanConfirmDelivery')).toHaveLength(0)
  expect(realErrors(errors)).toEqual([])
})
