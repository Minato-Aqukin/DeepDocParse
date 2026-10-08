import { expect, test, type Page } from '@playwright/test'
import { realErrors, watchErrors } from './console-guard'

test('无桌面宿主的未登录浏览器只能看到登录入口，不请求任何业务 API', async ({ page }) => {
  const errors = watchErrors(page), requests: string[] = []
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request.url()) })
  await page.goto('/#/sources')
  await expect(page.getByRole('heading', { name: '数据源' })).toHaveCount(0)
  await expect(page).toHaveURL(/#\/login\?redirect=/)
  expect(requests).toEqual([])
  expect(realErrors(errors)).toEqual([])
})

/**
 * 桌面任务桩：宿主数据源桥 + 本机计划账本。
 *
 * 桌面任务组：直接驱动 AppShell 里的 `/tasks`（本机账本）与 `/tasks/new`
 *（准备页）；计划账本夹具沿用此前工作台的 plan-ledger 形状
 *（`sourceList/sourceActivate` 是新增的 wave 1 具名方法）。
 */
interface DesktopFixtureOptions {
  center?: boolean
  plans?: boolean
}

async function desktopFixture(page: Page, options: DesktopFixtureOptions = {}) {
  const drafts = new Map<string, { revision: number; value: unknown }>()
  const calls: { name: string; input: Record<string, unknown> }[] = []
  const localSource = {
    sourceId: 'local-0', kind: 'local', label: '本机工作区', state: 'ready',
    readOnly: false, features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'],
    active: !options.center, reason: null,
    environment: { environmentId: 'local-env-0', workspaceId: 'workspace-0', authorityNodeId: 'local-env-0' },
    profile: { profileId: 'profile-local', issuer: 'local-env-0', subject: 'owner' },
  }
  const centerSource = {
    sourceId: 'center-0', kind: 'center', label: '研究中心', state: 'ready',
    readOnly: true, features: ['resources', 'documents', 'search', 'wiki', 'federation_tasks'],
    active: !!options.center, reason: null,
    environment: { environmentId: 'node-center', workspaceId: 'org-1', authorityNodeId: 'node-center' },
    profile: { profileId: 'profile-center', issuer: 'node-center', subject: 'user-alice' },
  }
  const sources = [localSource, centerSource]
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
  // 真宿主的每个 /api 响应都带 `X-DDP-Source: <当前源>`（fail closed 围栏）。
  const sourceHeaders = { 'X-DDP-Source': options.center ? 'center-0' : 'local-0' }
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/me')) {
      return route.fulfill({ headers: sourceHeaders, json: { id: 'u-1', username: 'e2e', email: 'e2e@example.com',
        role: 'admin', organization_id: 'org-1', created_at: new Date().toISOString() } })
    }
    if (path === '/api/v1/tasks') return route.fulfill({ headers: sourceHeaders, json: { items: [], next_cursor: null } })
    // Lockable inputs come from the local source's center-shaped /api/resources (the host
    // proxies ddp://app/api/** to the runtime); the renderer no longer pages projections.
    if (path === '/api/resources') {
      return route.fulfill({ headers: sourceHeaders, json: { has_more: false, items: [{
        id: 'resource-0', organization_id: 'local', owner_id: 'owner', uploader_ref: { issuer: 'local-env-0', subject: 'owner' },
        display_name: '甲的技术手册.pdf', publication: 'private', versions: [{
          id: 'version-0', resource_id: 'resource-0', version_no: 1, document_id: 'version-0',
          source_digest: 'a'.repeat(64), filename: '甲的技术手册.pdf', size_bytes: 2048,
          parse_job_id: 'job-0', parse_status: 'succeeded', index_status: 'ready' }] }] } })
    }
    return route.fulfill({ headers: sourceHeaders, json: [] })
  })
  await page.exposeBinding('desktopTestCall', async (_source, { name, input = {} }) => {
    calls.push({ name, input })
    const ok = (value: unknown) => ({ ok: true, value })
    if (name === 'sourceList') return ok(sources)
    if (name === 'sourceActivate') {
      const target = sources.find(item => item.sourceId === input.sourceId)
      if (!target || target.state !== 'ready' || target.kind !== 'local') return { ok: false, error: { code: 'source_unavailable' } }
      for (const item of sources) item.active = item.sourceId === target.sourceId
      return ok(target)
    }
    if (name === 'hostStatus') return ok({ secrets: { backend: 'basic_text', persistentAvailable: false }, lifecycle: 'close_stops_owned_local_tasks' })
    const draftKey = String(input.key ?? 'workspace') === 'workspace' ? String(input.connectionId) : String(input.connectionId) + ':' + String(input.key)
    if (name === 'clientReadDraft') return ok(drafts.get(draftKey) ?? null)
    if (name === 'clientSaveDraft') {
      const previous = drafts.get(draftKey)
      if ((previous?.revision ?? 0) !== input.expectedRevision) return { ok: false, error: { code: 'revision_conflict' } }
      drafts.set(draftKey, { revision: input.expectedRevision + 1, value: input.value }); return ok({ revision: input.expectedRevision + 1 })
    }
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
              profile_id: 'profile-center', issuer: 'node-center', subject: 'user-alice' }],
            // 形状照 `ddp_local/plan_templates.py::center_query_scope` 生成的 TaskPlan 写全：
            // 界面上的计划修订用的是与 Web 协调者同一个 PlanSummary，缺字段就会静默少画一块。
            plan: { schema: 'ddp-plan-admission/1#TaskPlan', plan_id: id, revision: 1, plan_digest: 'sha256:' + 'c'.repeat(64),
              task_spec_digest: 'sha256:' + '9'.repeat(64), root_coordinator_node_id: 'local-env-0', planning_state: 'ready',
              steps: [{ step_id: 'retrieve-1', operation: 'retrieve', executor_node_id: 'node-center', depends_on: [],
                fixed_inputs: input.inputs.map((item: Plan) => item.ref) }],
              final_result_writer: 'local-env-0', execution_consent_ref: null, valid_until: '2030-01-01T00:00:00Z',
              budget: { max_requests: 4, max_bytes: bytes * 4, max_generation_tokens: 0, max_hops: 1, deadline: '2030-01-01T00:00:00Z' },
              data_edges: [{ edge_id: 'edge-query', from_node_id: 'local-env-0', to_node_id: 'node-center', payload_kind: 'query_text',
                retention: input.retention, authorised_by: 'local:local-env-0' }] },
            exploration: { allowed_recipients: ['node-center'], allowed_payload: ['query_text'],
              budget: { max_probe_requests: 2, max_egress_bytes: bytes * 2 } } } })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.plans.get(id))
        return ok(planLedger.plans.get(id))
      }
      if (name === 'clientPlanProposeFile') {
        if (planLedger.receipts.has(input.idempotencyKey)) return ok(planLedger.receipts.get(input.idempotencyKey)!())
        if (!Array.isArray(input.inputs) || input.inputs.length !== 1) return fail('invalid_arguments')
        if (input.filename !== '甲的技术手册.pdf') return fail('input_changed')
        const id = 'plan-' + (planLedger.plans.size + 1)
        planLedger.plans.set(id, { plan_id: id, scope_digest: 'sha256:' + 'd'.repeat(64), planning_state: 'ready', revoked: false, consents: {},
          scope: { task_spec: { operation: 'corpus.parse', query: '文件任务描述' }, retention: input.retention,
            output_locations: ['local:workspace-0'],
            input_manifest: input.inputs.map((item: Plan) => ({ ref: item.ref, digest: item.digest, size_bytes: item.sizeBytes })),
            payload_bindings: [], transport_bindings: [],
            plan: { schema: 'ddp-plan-admission/1#TaskPlan', plan_id: id, revision: 1, plan_digest: 'sha256:' + 'c'.repeat(64),
              task_spec_digest: 'sha256:' + '9'.repeat(64), root_coordinator_node_id: 'local-env-0', planning_state: 'ready',
              steps: [{ step_id: 'parse-1', operation: 'corpus.parse', executor_node_id: 'node-center', depends_on: [],
                fixed_inputs: input.inputs.map((item: Plan) => item.ref) }],
              final_result_writer: 'local-env-0', execution_consent_ref: null, valid_until: '2030-01-01T00:00:00Z',
              budget: { max_requests: 4, max_bytes: 4096, max_generation_tokens: 0, max_hops: 1, deadline: '2030-01-01T00:00:00Z' },
              data_edges: [{ edge_id: 'edge-file', from_node_id: 'local-env-0', to_node_id: 'node-center', payload_kind: 'source_files',
                retention: input.retention, authorised_by: 'local:local-env-0' }] },
            exploration: { allowed_recipients: ['node-center'], allowed_payload: [], budget: { max_probe_requests: 0, max_egress_bytes: 0 } } } })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.plans.get(id))
        return ok(planLedger.plans.get(id))
      }
      if (name === 'clientPlanList') return ok({ visible_total: planLedger.plans.size, items: [...planLedger.plans.values()].map(item => ({
        plan_id: item.plan_id, planning_state: item.planning_state,
        federation: planLedger.federation.has(item.plan_id) ? { state: planLedger.federation.get(item.plan_id)!.state, delivery_state: planLedger.federation.get(item.plan_id)!.delivery?.state ?? null } : null })) })
      if (!plan) return fail('not_found')
      const federation = planLedger.federation.get(plan.plan_id)
      if (name === 'clientPlanGet') return ok(planDetail(plan.plan_id))
      if (name === 'clientPlanReviewCenter') {
        if (!federation?.plan || plan.revoked) return fail('plan_changed')
        if (planLedger.receipts.has(input.idempotencyKey)) return ok(planLedger.receipts.get(input.idempotencyKey)!())
        const id = 'plan-' + (planLedger.plans.size + 1)
        const reviewed = structuredClone(plan)
        Object.assign(reviewed, { plan_id: id, scope_digest: 'sha256:' + '2'.repeat(64), planning_state: 'ready', consents: {} })
        reviewed.scope.payload_bindings = []
        reviewed.scope.center_execution = { root_task_id: federation.root_task_id, parent_plan_id: plan.plan_id,
          transport_ref: 'center', plan: structuredClone(federation.plan) }
        planLedger.plans.set(id, reviewed)
        planLedger.federation.set(id, { ...structuredClone(federation), plan_id: id })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.plans.get(id))
        return ok(reviewed)
      }
      if (name === 'clientPlanApprove') {
        if (input.userConfirmed !== true || input.scopeDigest !== plan.scope_digest) return fail('plan_changed')
        if (input.phase === 'execution' && !plan.scope.center_execution) return fail('plan_changed')
        if (input.phase === 'exploration' && plan.scope.center_execution) return fail('plan_changed')
        if (planLedger.cancelApproval) { planLedger.cancelApproval = false; return fail('approval_cancelled') }
        plan.consents[input.phase] = { consent_id: 'consent-' + input.phase }
        plan.planning_state = plan.consents.execution ? 'approved' : 'exploring'
        return ok(plan)
      }
      if (name === 'clientPlanDispatch') {
        if (plan.revoked || !plan.consents[input.phase]) return fail('approved_plan_required')
        const centerPlan = federation?.plan ?? { ...structuredClone(plan.scope.plan),
          plan_id: 'center-plan-1', plan_digest: 'sha256:' + '3'.repeat(64), root_coordinator_node_id: 'node-center',
          steps: [{ step_id: 'source-retrieval', operation: 'retrieve', executor_node_id: 'node-source', depends_on: [], fixed_inputs: [] },
            { step_id: 'cited-generation', operation: 'answer', executor_node_id: 'node-center', depends_on: ['source-retrieval'], fixed_inputs: [] }],
          final_result_writer: 'node-center',
          data_edges: [{ edge_id: 'source-to-generator', from_node_id: 'node-source', to_node_id: 'node-center',
            payload_kind: 'evidence_excerpts', retention: 'temporary', authorised_by: 'source-policy', relay_via: [] }],
          budget: { ...plan.scope.plan.budget, max_generation_tokens: 256 } }
        planLedger.federation.set(plan.plan_id, { ...(federation ?? {}), plan: centerPlan,
          plan_id: plan.plan_id, root_task_id: 'root-1', center_plan_digest: centerPlan.plan_digest,
          state: input.phase === 'exploration' ? 'planned' : 'submitted', reconcile: { at: 1789000000 }, delivery: null })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.federation.get(plan.plan_id))
        if (planLedger.loseDispatch) { planLedger.loseDispatch = false; return fail('outcome_unknown') }
        return ok(planLedger.federation.get(plan.plan_id))
      }
      if (name === 'clientPlanResume') {
        if (!federation || plan.revoked) return fail('plan_changed')
        if (planLedger.receipts.has(input.idempotencyKey)) return ok(planLedger.receipts.get(input.idempotencyKey)!())
        planLedger.federation.set(plan.plan_id, { ...federation, state: 'planned',
          center_plan_digest: 'sha256:' + '4'.repeat(64), reconcile: { at: 1789000001 } })
        planLedger.receipts.set(input.idempotencyKey, () => planLedger.federation.get(plan.plan_id))
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
    if (name === 'clientQuery') throw new Error(`Unexpected fixture query ${input.name}`)
    if (name === 'clientReadDraft') return ok(drafts.get(draftKey) ?? null)
    if (name === 'clientSaveDraft') {
      const previous = drafts.get(draftKey)
      if ((previous?.revision ?? 0) !== input.expectedRevision) return { ok: false, error: { code: 'revision_conflict' } }
      drafts.set(draftKey, { revision: input.expectedRevision + 1, value: input.value }); return ok({ revision: input.expectedRevision + 1 })
    }
    if (name === 'clientReceipt') return ok(planLedger.receipts.get(input.idempotencyKey)?.() ?? null)
    throw new Error(`Unexpected fixture operation ${name}`)
  })
  await page.addInitScript(() => {
    const call = (name: string, input?: unknown) => (window as unknown as { desktopTestCall: (value: unknown) => Promise<unknown> }).desktopTestCall({ name, input })
    const methods = ['sourceList', 'sourceActivate', 'hostStatus', 'clientReadDraft',
      'clientSaveDraft', 'clientQuery', 'clientReceipt',
      'clientPlanPropose', 'clientPlanProposeFile', 'clientPlanList', 'clientPlanGet', 'clientPlanApprove', 'clientPlanReviewCenter', 'clientPlanRevoke', 'clientPlanCancel', 'clientPlanDispatch',
      'clientPlanResume', 'clientPlanReconcile', 'clientPlanFetchDelivery', 'clientPlanConfirmDelivery']
    window.ddpDesktop = Object.fromEntries(methods.map(name => [name, (input: unknown) => call(name, input)])) as never
    // The real host serves ddp://app/api/** through its protocol handler; in the browser
    // test the same same-origin /api routes above stand in for it.
    const realFetch = window.fetch.bind(window)
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input)
      if (!url.startsWith('ddp://app/')) return realFetch(input, init)
      const local = url.slice('ddp://app'.length)
      return realFetch(input instanceof Request ? new Request(local, input) : local, init)
    }
  })
  return { drafts, calls, sources, planLedger }
}

test('本机源空账本可准备任务：预填问题带入，禁用写入口不存在', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/tasks')
  await expect(page.getByRole('heading', { name: '联邦任务', exact: true })).toBeVisible()
  await expect(page.getByText('尚无联邦任务。')).toBeVisible()
  await expect(page.getByText('中心在桌面里只读')).toHaveCount(0)
  await page.goto('/#/tasks/new?query=' + encodeURIComponent('控制器工作温度是多少？'))
  await expect(page.getByLabel('问题（批准后会原文发送给接收中心）')).toHaveValue('控制器工作温度是多少？')
  expect(fixture.calls.filter(call => call.name === 'clientPlanPropose')).toHaveLength(0)
  expect(realErrors(errors)).toEqual([])
})

async function proposeQueryPlan(page: Page) {
  await page.getByLabel('接收方（已连接的中心）').selectOption('center-0')
  await page.getByLabel('问题（批准后会原文发送给接收中心）').fill('控制器工作温度是多少？')
  await page.getByLabel('甲的技术手册.pdf').check()
  await page.getByRole('button', { name: '生成待审阅计划', exact: true }).click()
  await expect(page.getByRole('heading', { name: '联邦任务审阅' })).toBeVisible()
}

test('联邦任务主路径：审阅实际外发内容，分阶段批准后派发、对账、本地重算交付摘要再确认', async ({ page }, info) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/tasks/new')
  await proposeQueryPlan(page)
  const detail = page.getByLabel('联邦任务详情')
  // The review shows what would actually leave this machine, to whom, and under which limits.
  const outgoing = detail.getByRole('table').first()
  await expect(outgoing.getByRole('row')).toHaveCount(3)
  await expect(outgoing).toContainText('探索'); await expect(outgoing).toContainText('执行')
  await expect(outgoing).toContainText('问题原文'); await expect(outgoing).toContainText('node-center')
  await expect(detail).toContainText('version-0')
  // 保留类别、规划态与交付态的中文都来自契约生成物；这里断言的就是契约里的那一份。
  await expect(detail).toContainText('临时数据')
  // 计划修订这一节由 Web 协调者同一个 PlanSummary 画：步骤、执行者、数据边、中继、总预算。
  const revision = detail.getByRole('region', { name: '执行计划' })
  await expect(revision).toContainText('retrieve-1')
  await expect(revision).toContainText('edge-query')
  await expect(revision).toContainText('2030-01-01T00:00:00Z')
  await expect(detail.getByRole('button', { name: '派发探索', exact: true })).toBeDisabled()
  await expect(detail.getByRole('button', { name: '批准执行…', exact: true })).toBeDisabled()

  await detail.getByRole('button', { name: '批准探索…', exact: true }).click()
  await detail.getByRole('button', { name: '派发探索', exact: true }).click()
  await expect(detail).toContainText('中心计划已就绪')
  await expect(detail.getByRole('button', { name: '派发执行', exact: true })).toBeDisabled()
  await expect(detail.getByRole('button', { name: '批准执行…', exact: true })).toBeDisabled()
  await detail.getByRole('button', { name: '载入中心计划重新审阅', exact: true }).click()
  await expect(revision).toContainText('source-retrieval')
  await expect(revision).toContainText('cited-generation')
  await expect(revision).toContainText('source-to-generator')
  await expect(revision).toContainText('sha256:' + '3'.repeat(64))
  await expect(detail.getByRole('button', { name: '批准探索…', exact: true })).toHaveCount(0)
  await expect(detail.getByRole('button', { name: '派发执行', exact: true })).toBeDisabled()
  await detail.getByRole('button', { name: '批准执行…', exact: true }).click()
  await detail.getByRole('button', { name: '派发执行', exact: true }).click()
  await expect(detail).toContainText('中心执行中')
  await detail.getByRole('button', { name: '结果未确认 · 重新核对', exact: true }).click()
  await expect(detail).toContainText('中心已完成')
  await expect(detail.getByRole('button', { name: '确认交付', exact: true })).toBeDisabled()
  await detail.getByRole('button', { name: '取回交付并校验', exact: true }).click()
  await expect(detail).toContainText('本地重算摘要一致')
  await expect(detail).toContainText('中心生成内容 · 待复核')
  await detail.getByRole('button', { name: '确认交付', exact: true }).click()
  await expect(detail.getByText('已交付', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('federation-task-review.png'), fullPage: true, animations: 'disabled' })

  expect(fixture.calls.filter(call => call.name === 'clientPlanConfirmDelivery')).toHaveLength(1)
  // The renderer never names an endpoint or a credential for any plan operation.
  for (const call of fixture.calls.filter(call => call.name.startsWith('clientPlan')))
    expect(Object.keys(call.input).some(key => ['endpoint', 'credential', 'secret', 'url', 'path'].includes(key))).toBe(false)
  const webStorage = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))
  expect(webStorage).not.toContain('sha256:' + 'd'.repeat(64))
  expect(realErrors(errors)).toEqual([])
})

test('联邦任务失败路径：取消批准不授权，回执未知保留编号，摘要不一致不许确认', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/tasks/new')
  await proposeQueryPlan(page)
  const detail = page.getByLabel('联邦任务详情')
  fixture.planLedger.cancelApproval = true
  await detail.getByRole('button', { name: '批准探索…', exact: true }).click()
  await expect(detail.getByRole('alert')).toContainText('已取消批准，没有授予任何外发许可。')
  await expect(detail.getByRole('button', { name: '派发探索', exact: true })).toBeDisabled()
  await expect(page.getByText('有一项任务操作结果未知', { exact: false })).toHaveCount(0)

  await detail.getByRole('button', { name: '批准探索…', exact: true }).click()
  fixture.planLedger.loseDispatch = true
  await detail.getByRole('button', { name: '派发探索', exact: true }).click()
  await expect(detail.getByRole('alert')).toContainText('提交结果未确认')
  await expect(page.getByText('有一项任务操作结果未知', { exact: false })).toBeVisible()
  const dispatches = fixture.calls.filter(call => call.name === 'clientPlanDispatch')
  expect(dispatches).toHaveLength(1)
  const unknownKey = dispatches[0]!.input.idempotencyKey
  // The unknown key is reconciled by receipt in place, never resent.
  await page.getByRole('button', { name: '查询回执', exact: true }).click()
  await expect(page.getByText('有一项任务操作结果未知', { exact: false })).toHaveCount(0)
  expect(fixture.calls.filter(call => call.name === 'clientPlanDispatch')).toHaveLength(1)
  expect(fixture.calls.find(call => call.name === 'clientReceipt')?.input.idempotencyKey).toBe(unknownKey)

  await detail.getByRole('button', { name: '载入中心计划重新审阅', exact: true }).click()
  await detail.getByRole('button', { name: '批准执行…', exact: true }).click()
  await detail.getByRole('button', { name: '派发执行', exact: true }).click()
  await detail.getByRole('button', { name: '结果未确认 · 重新核对', exact: true }).click()
  fixture.planLedger.tamper = true
  await detail.getByRole('button', { name: '取回交付并校验', exact: true }).click()
  await expect(detail).toContainText('本地重算摘要不一致')
  await expect(detail).toContainText('sha256:' + 'e'.repeat(64))
  await expect(detail.getByRole('button', { name: '确认交付', exact: true })).toBeDisabled()
  await expect(detail.getByText('中心生成内容 · 待复核')).toHaveCount(0)
  expect(fixture.calls.filter(call => call.name === 'clientPlanConfirmDelivery')).toHaveLength(0)
  expect(realErrors(errors)).toEqual([])
})

test('文件任务：一次锁定一个已就绪版本，文件名必须与存量一致', async ({ page }) => {
  const fixture = await desktopFixture(page), errors = watchErrors(page)
  await page.goto('/#/tasks/new')
  await page.getByLabel('任务类型').selectOption('file')
  await page.getByLabel('接收方（已连接的中心）').selectOption('center-0')
  await page.getByLabel('本地文件（锁定一个已就绪版本，文件名必须与存量一致）').selectOption('version-0')
  await page.getByRole('button', { name: '生成待审阅计划', exact: true }).click()
  await expect(page.getByRole('heading', { name: '联邦任务审阅' })).toBeVisible()
  const detail = page.getByLabel('联邦任务详情')
  await expect(detail).toContainText('解析固定 PDF（corpus.parse）')
  const propose = fixture.calls.find(call => call.name === 'clientPlanProposeFile')!
  expect(propose).toBeTruthy()
  expect(propose.input.filename).toBe('甲的技术手册.pdf')
  expect(propose.input.inputs).toEqual([{ ref: 'version-0', digest: 'sha256:' + 'a'.repeat(64), sizeBytes: 2048 }])
  expect(realErrors(errors)).toEqual([])
})

test('中心源只读：写入口禁用并说明原因，发起入口切到本机工作区且预填跟过去', async ({ page }) => {
  const fixture = await desktopFixture(page, { center: true }), errors = watchErrors(page)
  await page.goto('/#/tasks')
  await expect(page.getByText('中心在桌面里只读')).toBeVisible()
  await expect(page.getByRole('button', { name: '新建任务', exact: true })).toBeDisabled()
  await expect(page.getByText('在本机准备联邦任务')).toBeVisible()
  // 预填走 sessionStorage 暂存（按目标源），不经 URL 跨源传问题文本。
  await page.evaluate(() => sessionStorage.clear())
  await page.goto('/#/tasks/new?query=' + encodeURIComponent('中心看到的问题') + '&purpose=answer')
  await expect(page.getByText('联邦任务只在本机账本准备与批准')).toBeVisible()
  await expect(page.getByRole('button', { name: '切换到此工作区并继续', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '切换到此工作区并继续', exact: true }).click()
  await expect(page).toHaveURL(/#\/tasks\/new$/)
  await expect(page.getByLabel('问题（批准后会原文发送给接收中心）')).toHaveValue('中心看到的问题')
  expect(fixture.calls.filter(call => call.name === 'sourceActivate')).toHaveLength(1)
  expect(realErrors(errors)).toEqual([])
})
