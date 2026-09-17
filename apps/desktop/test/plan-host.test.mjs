import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { createHash, generateKeyPairSync, sign } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { ClientHost, clientFailure } from '../src/client-host.mjs'
import { CLIENT_CHANNELS, clientArguments } from '../src/client-policy.mjs'

// Host tests for the remote plan flow. The local runtime is a loopback HTTP double of
// ddp_local's plan routes, written with the same refusal rules the host relies on
// (consent per phase, reviewed endpoint, digest-bound approval, verified-only ack).
// The real ledger behind those routes is tested in python/ddp_local/tests/test_plan_desktop.py.
// The center is a fetch double with a real Ed25519 node proof, because pairing proves
// the node before any credential is used.

const SECRET = 'synthetic-center-credential-for-host-tests'
const TOKEN = 't'.repeat(64)
const LOCAL = { environment_id: 'local-env-1', workspace_id: 'workspace-1', authority_node_id: 'local-env-1' }
const LOCAL_PROFILE = { issuer: 'local-env-1', subject: 'workspace:workspace-1' }
const keys = generateKeyPairSync('ed25519')
const keyBytes = Buffer.from(keys.publicKey.export({ format: 'jwk' }).x, 'base64url')
const NODE = 'node-' + createHash('sha256').update(keyBytes).digest('hex').slice(0, 48)
const CENTER = 'https://center.test/team'
const GOOD = Buffer.from('{"answer":"40 C","schema":"ddp-answer/1","score":1.0}')
const DIGEST = 'sha256:' + createHash('sha256').update(GOOD).digest('hex')
const QUERY = '控制器工作温度是多少？'

function centerFetch(realFetch) {
  return async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url)
    if (url.origin !== 'https://center.test') return realFetch(input, init)
    const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    const route = url.pathname.replace(/^\/team/, '')
    if (route === '/api/v1/federation/node') {
      const issued = Date.now()
      const proof = { schema: 'ddp-node-proof/1', nonce: url.searchParams.get('challenge'), node_id: NODE, endpoint: CENTER,
        issued_at: new Date(issued).toISOString(), expires_at: new Date(issued + 60000).toISOString() }
      proof.signature = sign(null, Buffer.from(JSON.stringify([proof.schema, proof.nonce, proof.node_id, proof.endpoint,
        proof.issued_at, proof.expires_at])), keys.privateKey).toString('base64url')
      return json({ authority_node_id: NODE, public_key: keyBytes.toString('base64'), proof })
    }
    const projection = { cursor: 'center-0', sequence: 0, state: { resources: [] } }
    if (route === '/api/v1/client/handshake') return json({ protocol_version: 'ddp-client/1',
      identity: { environment_id: NODE, authority_node_id: NODE, workspace_id: 'org-1' }, profile: { issuer: NODE, subject: 'user-alice' },
      capabilities: ['client.snapshot', 'client.events', 'client.receipt'] })
    if (route === '/api/v1/client/snapshot') return json(projection)
    if (route === '/api/v1/client/events') return json({ events: [{ ...projection, previous_sequence: 0 }] })
    return json({ error: { code: 'not_found' } }, 404)
  }
}

async function localRuntime(t) {
  const s = { plans: new Map(), federation: new Map(), receipts: new Map(), calls: [], dispatches: [], approvals: [],
    acks: [], reconciles: [], stored: GOOD, loseAck: false, loseDispatch: false }
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://local'), chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const text = Buffer.concat(chunks).toString('utf8'), body = text ? JSON.parse(text) : null
    const key = req.headers['idempotency-key']
    const call = { method: req.method, path: url.pathname, body, key, headers: req.headers }
    s.calls.push(call)
    const send = (status, value) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)) }
    const fail = (status, code) => send(status, { error: { code } })
    if (req.headers.authorization !== 'Bearer ' + TOKEN) return fail(401, 'unauthorized')
    const projection = { cursor: 'local-0', sequence: 0, state: { resources: [], tasks: [] } }
    if (url.pathname === '/api/v1/client/handshake') return send(200, { protocol_version: 'ddp-client/1', identity: LOCAL,
      profile: LOCAL_PROFILE, capabilities: ['client.snapshot', 'client.events', 'client.receipt', 'plan.propose'] })
    if (url.pathname === '/api/v1/client/snapshot') return send(200, projection)
    if (url.pathname === '/api/v1/client/events') return send(200, { events: [{ ...projection, previous_sequence: 0 }] })
    if (url.pathname.startsWith('/api/v1/client/receipts/')) {
      const found = s.receipts.get(decodeURIComponent(url.pathname.split('/').pop()))
      return found ? send(200, found()) : fail(404, 'not_found')
    }
    if (url.pathname === '/api/v1/plans/propose') {
      if (s.receipts.has(key)) return send(201, s.receipts.get(key)())
      const planId = 'plan-' + (s.plans.size + 1), bytes = Buffer.byteLength(body.query)
      const query = { recipient_node_id: body.center.recipient_node_id, payload_kind: 'query_text', size_bytes: bytes,
        digest: 'sha256:' + createHash('sha256').update(body.query).digest('hex'), transport_ref: 'center' }
      const plan = { plan_id: planId, planning_state: 'ready', revoked: false, consents: {},
        scope_digest: 'sha256:' + createHash('sha256').update(text).digest('hex'),
        scope: { task_spec: { query: body.query }, input_manifest: body.inputs, retention: body.retention,
          output_locations: ['local:workspace-1'], transport_bindings: [{ transport_ref: 'center', ...body.center }],
          payload_bindings: [{ payload_id: 'exploration-query', phase: 'exploration', ...query },
            { payload_id: 'execution-query', phase: 'execution', edge_id: 'edge-query', ...query }],
          plan: { plan_id: planId, valid_until: '2030-01-01T00:00:00Z', data_edges: [] } } }
      s.plans.set(planId, plan); s.receipts.set(key, () => s.plans.get(planId))
      return send(201, plan)
    }
    if (url.pathname === '/api/v1/plans') return send(200, { visible_total: s.plans.size, items: [...s.plans.values()].map(plan => ({
      plan_id: plan.plan_id, planning_state: plan.planning_state, federation: s.federation.get(plan.plan_id)
        ? { state: s.federation.get(plan.plan_id).state, delivery_state: s.federation.get(plan.plan_id).delivery?.state ?? null } : null })) })
    const match = url.pathname.match(/^\/api\/v1\/plans\/([^/]+)(\/.*)?$/)
    const plan = match && s.plans.get(decodeURIComponent(match[1])), route = match?.[2] ?? ''
    if (!plan) return fail(404, 'not_found')
    const state = () => s.federation.get(plan.plan_id)
    const reviewed = center => center?.endpoint === plan.scope.transport_bindings[0].endpoint
    if (req.method === 'GET' && route === '') return send(200, plan)
    if (route === '/approve') {
      if (plan.revoked) return fail(400, 'consent_revoked')
      if (body.user_confirmed !== true || body.confirmed_scope_digest !== plan.scope_digest) return fail(409, 'plan_changed')
      s.approvals.push(call)
      plan.consents[body.phase] = { consent_id: 'consent-' + body.phase }
      plan.planning_state = plan.consents.execution ? 'approved' : 'exploring'
      s.receipts.set(key, () => plan)
      return send(200, plan)
    }
    if (route === '/revoke') { plan.revoked = true; plan.planning_state = 'invalidated'; s.receipts.set(key, () => plan); return send(200, plan) }
    if (route === '/dispatch') {
      if (plan.revoked || !plan.consents[body.phase]) return fail(400, 'consent_required')
      if (!reviewed(body.center)) return fail(400, 'policy_denied')
      s.dispatches.push(call)
      s.federation.set(plan.plan_id, { ...(state() ?? {}), plan_id: plan.plan_id, root_task_id: 'root-1', delivery: null,
        state: body.phase === 'exploration' ? 'planned' : 'submitted' })
      s.receipts.set(key, state)
      if (s.loseDispatch) { s.loseDispatch = false; req.socket.destroy(); return }
      return send(200, state())
    }
    if (route === '/federation') return state() ? send(200, state()) : fail(404, 'not_found')
    if (route === '/reconcile') {
      if (!reviewed(body.center)) return fail(400, 'policy_denied')
      s.reconciles.push(call)
      if (state().state === 'submitted') s.federation.set(plan.plan_id, { ...state(), state: 'succeeded', delivery: { id: 'delivery-1', state: 'pending' } })
      return send(200, state())
    }
    if (route === '/delivery/fetch') {
      if (!reviewed(body.center)) return fail(400, 'policy_denied')
      s.federation.set(plan.plan_id, { ...state(), delivery: { ...state().delivery, verified: true, result_manifest_digest: DIGEST } })
      return send(200, state())
    }
    if (route === '/delivery/result') {
      if (!state()?.delivery?.verified) return fail(404, 'not_found')
      res.setHeader('Content-Type', 'application/json'); return res.end(s.stored)
    }
    if (route === '/delivery/ack') {
      const delivery = state().delivery
      if (!reviewed(body.center)) return fail(400, 'policy_denied')
      if (!delivery?.verified || delivery.result_manifest_digest !== body.result_manifest_digest) return fail(409, 'plan_changed')
      s.acks.push(call)
      s.federation.set(plan.plan_id, { ...state(), delivery: { ...delivery, state: 'confirmed' } })
      s.receipts.set(key, state)
      if (s.loseAck) { s.loseAck = false; req.socket.destroy(); return }
      return send(200, state())
    }
    return fail(404, 'not_found')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) })
  const endpoint = `http://127.0.0.1:${server.address().port}`
  s.object = { start: async () => ({ state: 'ready' }),
    connection: () => ({ url: endpoint, token: TOKEN, handshake: { protocol_version: 'ddp-client/1', identity: LOCAL, profile: LOCAL_PROFILE } }) }
  return s
}

async function ready(clients, connectionId) {
  for (let attempt = 0; attempt < 200; attempt++) {
    const view = clients.list().find(item => item.connectionId === connectionId)?.view
    if (view?.transport === 'ready' && view.snapshot === 'current') return
    if (view?.transport === 'blocked') assert.fail(JSON.stringify(view))
    await delay(20)
  }
  assert.fail('connection did not become current')
}

async function setup(t) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-plan-host-'))
  const realFetch = globalThis.fetch
  globalThis.fetch = centerFetch(realFetch)
  const runtime = await localRuntime(t)
  const credentialUse = [], dialogs = []
  let answer = true
  const workspaces = new WorkspaceHandles()
  const workspace = workspaces.selectedWsl({ directory: '~/.deepdocparse/workspaces/plan-test' })
  const options = { workspaces, runtime: runtime.object, directory: path.join(temporary, 'client'),
    credentials: { withCredential: async (pair, operation) => { credentialUse.push(pair); return operation(SECRET) } },
    confirmApproval: async summary => { dialogs.push(summary); return answer } }
  const hosts = []
  const open = async () => {
    const clients = await new ClientHost(options).initialize(); hosts.push(clients)
    const local = (await clients.connectLocal({ workspaceId: workspace.workspaceId })).connectionId
    await ready(clients, local)
    return { clients, local }
  }
  t.after(async () => {
    for (const clients of hosts) await clients.close().catch(() => {})
    globalThis.fetch = realFetch
    await rm(temporary, { recursive: true, force: true })
  })
  const { clients, local } = await open()
  const center = (await clients.pairRemote({ label: '研究中心', environment: { environmentId: NODE, authorityNodeId: NODE,
    workspaceId: 'org-1', endpoint: CENTER }, profile: { profileId: 'profile-alice', issuer: NODE, subject: 'user-alice' } })).connectionId
  await ready(clients, center)
  return { clients, local, center, runtime, credentialUse, dialogs, options, temporary, open,
    answer: value => { answer = value } }
}

const propose = (clients, local, center, idempotencyKey = 'propose-0001') => clients.planPropose({ connectionId: local,
  centerConnectionId: center, query: QUERY, inputs: [{ ref: 'version-1', digest: 'sha256:' + '1'.repeat(64), sizeBytes: 1024 }],
  retention: 'temporary', validMinutes: 60, idempotencyKey })

test('plan IPC is a fixed schema: no path, URL, credential, plan body or implicit user confirmation crosses it', () => {
  const plan = { connectionId: 'connection-1', planId: 'plan-1' }
  const good = {
    clientPlanPropose: { connectionId: 'connection-1', centerConnectionId: 'connection-2', query: QUERY,
      inputs: [{ ref: 'version-1', digest: DIGEST, sizeBytes: 10 }], retention: 'temporary', validMinutes: 60, idempotencyKey: 'propose-0001' },
    clientPlanList: { connectionId: 'connection-1' }, clientPlanGet: plan, clientPlanReconcile: plan, clientPlanFetchDelivery: plan,
    clientPlanApprove: { ...plan, phase: 'exploration', scopeDigest: DIGEST, userConfirmed: true, idempotencyKey: 'approve-0001' },
    clientPlanRevoke: { ...plan, idempotencyKey: 'revoke-0001' },
    clientPlanDispatch: { ...plan, phase: 'execution', idempotencyKey: 'dispatch-0001' },
    clientPlanConfirmDelivery: { ...plan, deliveryId: 'delivery-1', resultManifestDigest: DIGEST, idempotencyKey: 'confirm-0001' },
  }
  for (const [method, input] of Object.entries(good)) {
    assert.equal(Object.hasOwn(CLIENT_CHANNELS, method), true, method)
    assert.deepEqual(clientArguments(method, structuredClone(input)), input, method)
    // Any extra field — an endpoint, a path, a credential, a TaskPlan — fails closed.
    for (const extra of [{ endpoint: CENTER }, { path: '/etc/passwd' }, { credential: SECRET }, { plan: {} }, { url: 'https://x' }])
      assert.throws(() => clientArguments(method, { ...input, ...extra }), /invalid_arguments/, method + JSON.stringify(extra))
  }
  const rejected = [
    ['clientPlanPropose', { inputs: [{ ref: '../outside', digest: DIGEST, sizeBytes: 10 }] }],
    ['clientPlanPropose', { inputs: [{ ref: 'version-1', digest: DIGEST, sizeBytes: 10, path: '/tmp/a.pdf' }] }],
    ['clientPlanPropose', { inputs: [{ ref: 'version-1', digest: 'sha256:short', sizeBytes: 10 }] }],
    ['clientPlanPropose', { inputs: [{ ref: 'version-1', digest: DIGEST, sizeBytes: 10 }, { ref: 'version-1', digest: DIGEST, sizeBytes: 10 }] }],
    ['clientPlanPropose', { centerConnectionId: 'https://center.test/team' }],
    ['clientPlanPropose', { retention: 'persistent' }], ['clientPlanPropose', { validMinutes: 100000 }],
    ['clientPlanPropose', { query: ' ' }], ['clientPlanPropose', { query: 'a b' }],
    ['clientPlanApprove', { userConfirmed: 'true' }], ['clientPlanApprove', { userConfirmed: false }],
    ['clientPlanApprove', { phase: 'publish' }], ['clientPlanApprove', { scopeDigest: 'sha256:' + 'A'.repeat(64) }],
    ['clientPlanDispatch', { planId: '../../plan-1' }], ['clientPlanDispatch', { planId: 'plan-1/../../x' }],
    ['clientPlanDispatch', { idempotencyKey: 'short' }],
    ['clientPlanConfirmDelivery', { resultManifestDigest: 'md5:abc' }], ['clientPlanConfirmDelivery', { deliveryId: 'a/b' }],
  ]
  for (const [method, change] of rejected)
    assert.throws(() => clientArguments(method, { ...good[method], ...change }), /invalid_arguments/, method + JSON.stringify(change))
  assert.throws(() => clientArguments('clientPlanApprove', (({ userConfirmed: _, ...rest }) => rest)(good.clientPlanApprove)), /invalid_arguments/)
})

test('preload and main expose exactly the fixed plan channels, each mapped to its host method', async () => {
  // Electron cannot run in this test process, so this is a structural check of the
  // two source files that wire CLIENT_CHANNELS; behaviour is covered by the tests below.
  const source = path.dirname(fileURLToPath(import.meta.url))
  const preload = await readFile(path.join(source, '../src/preload.cjs'), 'utf8')
  const main = await readFile(path.join(source, '../src/main.mjs'), 'utf8')
  for (const [method, channel] of Object.entries(CLIENT_CHANNELS)) {
    assert.match(preload, new RegExp(`${method}: (input => ipcRenderer\\.invoke\\('${channel}', input\\)|\\(\\) => ipcRenderer\\.invoke\\('${channel}'\\))`), method)
    if (method === 'clientSubscribe') continue
    assert.match(main, new RegExp(`${method}: (input => clients\\.|\\(\\) => clients\\.)`), method)
  }
  assert.doesNotMatch(preload, /invoke: |send: |ipcRenderer\.invoke\(channel/)
})

test('nothing dispatches or touches a credential before approval, and approval needs the native host dialog', async t => {
  const { clients, local, center, runtime, credentialUse, dialogs, answer } = await setup(t)
  const proposed = await propose(clients, local, center)
  // The recipient comes from host pairing metadata, not the renderer.
  assert.deepEqual(runtime.calls.find(call => call.path === '/api/v1/plans/propose').body.center, { recipient_node_id: NODE,
    environment_id: NODE, workspace_id: 'org-1', profile_id: 'profile-alice', issuer: NODE, subject: 'user-alice', endpoint: CENTER })
  const used = credentialUse.length
  await assert.rejects(clients.planDispatch({ connectionId: local, planId: proposed.plan_id, phase: 'exploration',
    idempotencyKey: 'dispatch-0001' }), { code: 'approved_plan_required' })
  assert.equal(credentialUse.length, used); assert.deepEqual(runtime.dispatches, [])

  const approve = (idempotencyKey, scopeDigest = proposed.scope_digest, phase = 'exploration') => clients.planApprove({
    connectionId: local, planId: proposed.plan_id, phase, scopeDigest, userConfirmed: true, idempotencyKey })
  await assert.rejects(approve('approve-0001', 'sha256:' + 'f'.repeat(64)), { code: 'plan_changed' })
  assert.equal(dialogs.length, 0, 'a stale digest never reaches the approval dialog')
  answer(false)
  await assert.rejects(approve('approve-0002'), { code: 'approval_cancelled' })
  assert.deepEqual(runtime.approvals, [])
  assert.deepEqual(dialogs[0], { planId: proposed.plan_id, phase: 'exploration', scopeDigest: proposed.scope_digest,
    payloads: [{ kind: 'query_text', recipient: NODE, bytes: Buffer.byteLength(QUERY), digest: proposed.scope.payload_bindings[0].digest }],
    transports: [{ recipient: NODE, endpoint: CENTER, workspace: 'org-1', subject: 'user-alice' }],
    inputs: 1, retention: 'temporary', outputLocations: ['local:workspace-1'], validUntil: '2030-01-01T00:00:00Z' })
  answer(true)
  const approved = await approve('approve-0003')
  assert.ok(approved.consents.exploration)
  assert.equal(runtime.approvals.length, 1); assert.equal(runtime.approvals[0].body.user_confirmed, true)
  // Exploration approval does not open execution.
  await assert.rejects(clients.planDispatch({ connectionId: local, planId: proposed.plan_id, phase: 'execution',
    idempotencyKey: 'dispatch-0002' }), { code: 'approved_plan_required' })
  assert.equal(credentialUse.length, used); assert.deepEqual(runtime.dispatches, [])
  // A remote connection is never a plan runtime.
  await assert.rejects(clients.planList({ connectionId: center }), /approved_plan_required/)
  assert.deepEqual(clientFailure(Object.assign(new Error(SECRET), { code: 'x' })), { ok: false, error: { code: 'host_operation_failed' } })
})

test('the center credential flows only from the broker into the owned runtime dispatch body', async t => {
  const { clients, local, center, runtime, credentialUse, options } = await setup(t)
  const proposed = await propose(clients, local, center)
  const approved = await clients.planApprove({ connectionId: local, planId: proposed.plan_id, phase: 'exploration',
    scopeDigest: proposed.scope_digest, userConfirmed: true, idempotencyKey: 'approve-0001' })
  const used = credentialUse.length
  const dispatched = await clients.planDispatch({ connectionId: local, planId: proposed.plan_id, phase: 'exploration', idempotencyKey: 'dispatch-0001' })
  assert.equal(dispatched.state, 'planned')
  assert.deepEqual(credentialUse.slice(used), [{ environmentId: NODE, profileId: 'profile-alice' }])
  assert.equal(runtime.dispatches.length, 1)
  assert.deepEqual(runtime.dispatches[0].body, { center: { endpoint: CENTER, credential: SECRET }, phase: 'exploration' })
  assert.equal(runtime.dispatches[0].key, 'dispatch-0001')
  assert.equal(runtime.dispatches[0].headers.authorization, 'Bearer ' + TOKEN, 'the local session token, not the center credential')
  for (const call of runtime.calls.filter(call => call !== runtime.dispatches[0]))
    assert.equal(JSON.stringify({ body: call.body, headers: call.headers }).includes(SECRET), false, call.path)
  const detail = await clients.planGet({ connectionId: local, planId: proposed.plan_id })
  const visible = JSON.stringify([proposed, approved, dispatched, detail, await clients.planList({ connectionId: local }), clients.list()])
  assert.equal(visible.includes(SECRET), false)
  await clients.saveDraft({ connectionId: local, key: 'federation-plan', expectedRevision: 0, value: { planKey: 'dispatch-0001' } })
  for (const file of await readdir(options.directory))
    assert.equal((await readFile(path.join(options.directory, file))).includes(SECRET), false, file)

  // A center that is not current (identity not re-proven) gets nothing.
  await clients.disconnect({ connectionId: center })
  const before = credentialUse.length
  await assert.rejects(clients.planReconcile({ connectionId: local, planId: proposed.plan_id }), { code: 'center_not_current' })
  await assert.rejects(propose(clients, local, center, 'propose-0002'), { code: 'center_not_current' })
  assert.equal(credentialUse.length, before)
  await clients.wake({ connectionId: center }); await ready(clients, center)
  // A ledger whose reviewed transport no longer names the paired endpoint gets nothing either.
  const reproven = credentialUse.length
  runtime.plans.get(proposed.plan_id).scope.transport_bindings[0].endpoint = 'https://elsewhere.test/team'
  await assert.rejects(clients.planReconcile({ connectionId: local, planId: proposed.plan_id }), { code: 'center_identity_changed' })
  assert.equal(credentialUse.length, reproven); assert.equal(runtime.reconciles.length, 0)
})

test('delivery confirmation refuses bytes that do not rehash; lost and repeated confirmations never ack twice', async t => {
  const { clients, local, center, runtime } = await setup(t)
  const proposed = await propose(clients, local, center), planId = proposed.plan_id
  for (const phase of ['exploration', 'execution']) {
    await clients.planApprove({ connectionId: local, planId, phase, scopeDigest: proposed.scope_digest, userConfirmed: true, idempotencyKey: 'approve-' + phase })
    await clients.planDispatch({ connectionId: local, planId, phase, idempotencyKey: 'dispatch-' + phase })
  }
  const reconciled = await clients.planReconcile({ connectionId: local, planId })
  assert.equal(reconciled.federation.state, 'succeeded'); assert.equal(reconciled.verification.state, 'unavailable')
  runtime.stored = Buffer.from('{"answer":"41 C","schema":"ddp-answer/1","score":1.0}')
  const fetched = await clients.planFetchDelivery({ connectionId: local, planId })
  assert.equal(fetched.federation.delivery.verified, true, 'the runtime flag alone says verified')
  assert.equal(fetched.verification.state, 'failed', 'but the host rehash of the stored bytes does not match')
  const confirm = idempotencyKey => clients.planConfirmDelivery({ connectionId: local, planId, deliveryId: 'delivery-1',
    resultManifestDigest: DIGEST, idempotencyKey })
  await assert.rejects(confirm('confirm-0001'), { code: 'delivery_unverified' })
  assert.deepEqual(runtime.acks, [])

  runtime.stored = GOOD; runtime.loseAck = true
  assert.equal((await clients.planGet({ connectionId: local, planId })).verification.state, 'passed')
  await assert.rejects(confirm('confirm-0002'))
  assert.equal(runtime.acks.length, 1)
  await assert.rejects(confirm('confirm-0002'), { code: 'outcome_unknown' })
  assert.equal(runtime.acks.length, 1, 'an unknown confirmation is reconciled, never resent under its key')
  assert.equal((await clients.receipt({ connectionId: local, idempotencyKey: 'confirm-0002' })).delivery.state, 'confirmed')
  const repeated = await confirm('confirm-0003')
  assert.equal(repeated.delivery.state, 'confirmed')
  assert.equal(runtime.acks.length, 1, 'a confirmed delivery answers repeat confirmation locally')
  assert.deepEqual(runtime.acks[0].body, { delivery_id: 'delivery-1', result_manifest_digest: DIGEST, center: { endpoint: CENTER, credential: SECRET } })
  // Nothing was cleaned up by confirming: the verified local copy still rehashes.
  assert.equal((await clients.planGet({ connectionId: local, planId })).verification.state, 'passed')
})

test('an accepted remote task survives a host restart: the unknown write stays unknown and the mirror is reconcilable', async t => {
  const { clients, local, center, runtime, open } = await setup(t)
  const proposed = await propose(clients, local, center), planId = proposed.plan_id
  await clients.planApprove({ connectionId: local, planId, phase: 'exploration', scopeDigest: proposed.scope_digest, userConfirmed: true, idempotencyKey: 'approve-0001' })
  runtime.loseDispatch = true
  await assert.rejects(clients.planDispatch({ connectionId: local, planId, phase: 'exploration', idempotencyKey: 'dispatch-0001' }))
  assert.equal(runtime.dispatches.length, 1)
  await clients.close()

  const restarted = await open()
  const summary = restarted.clients.list().find(item => item.kind === 'remote')
  assert.equal(summary.connectionId, center)
  await assert.rejects(restarted.clients.planDispatch({ connectionId: restarted.local, planId, phase: 'exploration',
    idempotencyKey: 'dispatch-0001' }), { code: 'outcome_unknown' })
  assert.equal(runtime.dispatches.length, 1, 'restart never replays the dispatch')
  const listed = await restarted.clients.planList({ connectionId: restarted.local })
  assert.deepEqual(listed.items.map(item => [item.plan_id, item.federation?.state]), [[planId, 'planned']])
  // Before the center is re-proven, reconciliation is refused rather than faked.
  await assert.rejects(restarted.clients.planReconcile({ connectionId: restarted.local, planId }), { code: 'center_not_current' })
  await restarted.clients.wake({ connectionId: center }); await ready(restarted.clients, center)
  const reconciled = await restarted.clients.planReconcile({ connectionId: restarted.local, planId })
  assert.equal(reconciled.federation.root_task_id, 'root-1'); assert.equal(runtime.reconciles.length, 1)
  assert.equal((await restarted.clients.receipt({ connectionId: restarted.local, idempotencyKey: 'dispatch-0001' })).state, 'planned')
  assert.equal(runtime.dispatches.length, 1)
})
