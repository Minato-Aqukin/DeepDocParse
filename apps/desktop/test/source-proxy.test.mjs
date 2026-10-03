import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash, generateKeyPairSync, sign } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { ClientHost } from '../src/client-host.mjs'
import { SOURCE_CHANNELS, sourceArguments, normalizeEndpoint, normalizeOrigin } from '../src/source-policy.mjs'
import { staticUI } from '../src/static-ui.mjs'
import { contentSecurityPolicy, uiLocation } from '../src/policy.mjs'

// HostProxy wave 1: source registry + /api proxy. Fakes: a loopback "local"
// runtime (process token) and a center double with a real Ed25519 node proof.
// Consumer-visible behavior only: tokens never reach the renderer, center
// non-GET never touches the network, 401 marks signed_out, X-DDP-Source is
// present, active source persists, password never persisted, cancel → null.

const TOKEN = 't'.repeat(64)
const SECRET = 'synthetic-center-jwt-for-source-tests'
const LOCAL = { environment_id: 'local-env-1', workspace_id: 'workspace-1', authority_node_id: 'local-env-1' }
const LOCAL_PROFILE = { issuer: 'local-env-1', subject: 'workspace:workspace-1' }
const keys = generateKeyPairSync('ed25519')
const keyBytes = Buffer.from(keys.publicKey.export({ format: 'jwk' }).x, 'base64url')
const NODE = 'node-' + createHash('sha256').update(keyBytes).digest('hex').slice(0, 48)
const CENTER = 'https://center.test/team'

function centerDouble(t, { loginStatus = 200, handshakeStatus = 200, endpoint = CENTER,
  resources = [], documents = {} } = {}) {
  const seen = { posts: 0, gets: 0, logins: 0, handshakes: 0, paths: [], offline: false }
  const realFetch = globalThis.fetch
  const origin = new URL(endpoint).origin, prefix = new URL(endpoint).pathname.replace(/\/$/, '')
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url)
    if (url.origin !== origin) return realFetch(input, init)
    const json = (body, status = 200) => new Response(JSON.stringify(body),
      { status, headers: { 'Content-Type': 'application/json' } })
    const route = url.pathname.startsWith(prefix + '/') ? url.pathname.slice(prefix.length) : url.pathname
    if (seen.offline) return json({ error: { code: 'unavailable' } }, 503)
    if (route === '/api/auth/login') {
      seen.logins++
      if (loginStatus !== 200) return json({ error: { code: 'invalid_credentials' } }, loginStatus)
      return json({ access_token: SECRET, token_type: 'bearer', expires_in: 604800 })
    }
    if (init.method && init.method !== 'GET' && init.method !== 'HEAD') {
      seen.posts++
      return json({ error: { code: 'must_not_happen' } }, 500)
    }
    seen.gets++
    seen.paths.push(url.pathname + url.search)
    if (route === '/api/v1/federation/node') {
      const issued = Date.now()
      const proof = { schema: 'ddp-node-proof/1', nonce: url.searchParams.get('challenge'), node_id: NODE,
        endpoint, issued_at: new Date(issued).toISOString(),
        expires_at: new Date(issued + 60000).toISOString() }
      proof.signature = sign(null, Buffer.from(JSON.stringify([proof.schema, proof.nonce, proof.node_id,
        proof.endpoint, proof.issued_at, proof.expires_at])), keys.privateKey).toString('base64url')
      return json({ authority_node_id: NODE, public_key: keyBytes.toString('base64'), proof })
    }
    if (route === '/api/v1/client/handshake') {
      seen.handshakes++
      if (handshakeStatus !== 200) return json({ error: { code: 'x' } }, handshakeStatus)
      return json({ protocol_version: 'ddp-client/1',
        identity: { environment_id: NODE, authority_node_id: NODE, workspace_id: 'org-1' },
        profile: { issuer: NODE, subject: 'user-alice' },
        capabilities: ['client.snapshot', 'client.events', 'client.receipt'] })
    }
    if (route === '/api/v1/client/snapshot') {
      return json({ cursor: 'center-0', sequence: 0, state: { resources } })
    }
    if (route === '/api/v1/client/events') {
      const projection = { cursor: 'center-0', sequence: 0, state: { resources } }
      return json({ events: [{ ...projection, previous_sequence: 0 }] })
    }
    if (route === '/api/resources') return json({ items: resources, total: resources.length })
    if (documents[route]) return json(documents[route])
    if (route === '/api/documents/doc-broken/download-url') {
      return new Response('{"url":"https://center.test/files/signed?X-Amz-Signature=presigned-secret"',
        { headers: { 'Content-Type': 'application/json' } })
    }
    if (route === '/api/documents/doc-1/download-url') {
      // Encoded like the real control-api (Go encoding/json writes '&' as \u0026).
      const url = 'https://center.test/files/signed?X-Amz-Algorithm=AWS4\\u0026X-Amz-Signature=presigned-secret'
      return new Response(`{"url":"${url}","expires_in":60}`, { headers: { 'Content-Type': 'application/json', 'Content-Length': '999' } })
    }
    return json({ error: { code: 'not_found' } }, 404)
  }
  t.after(() => { globalThis.fetch = realFetch })
  return seen
}

async function localDouble(t, { capabilities, resources = [], documents = {} } = {}) {
  const seen = { authorization: [], paths: [], forwarded: [] }
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://local'), chunks = []
    for await (const chunk of req) chunks.push(chunk)
    seen.authorization.push(req.headers.authorization)
    seen.forwarded.push([req.headers.cookie, req.headers.origin, req.headers.referer])
    seen.paths.push(`${req.method} ${url.pathname}`)
    const send = (status, value, type = 'application/json') => {
      res.statusCode = status
      res.setHeader('Content-Type', type)
      res.end(typeof value === 'string' ? value : JSON.stringify(value))
    }
    if (req.headers.authorization !== 'Bearer ' + TOKEN) return send(401, { error: { code: 'unauthorized' } })
    if (url.pathname === '/api/v1/capabilities') {
      return send(200, { workspace_id: 'workspace-1', environment_id: 'local-env-1', kind: 'local',
        ...(capabilities ? { content_features: capabilities } : {}) })
    }
    if (url.pathname === '/api/v1/client/handshake') return send(200, { protocol_version: 'ddp-client/1',
      identity: LOCAL, profile: LOCAL_PROFILE,
      capabilities: ['client.snapshot', 'client.events', 'client.receipt'] })
    if (url.pathname === '/api/v1/client/snapshot') {
      return send(200, { cursor: 'local-0', sequence: 0, state: { resources, tasks: [] } })
    }
    if (url.pathname === '/api/v1/client/events') {
      const projection = { cursor: 'local-0', sequence: 0, state: { resources, tasks: [] } }
      return send(200, { events: [{ ...projection, previous_sequence: 0 }] })
    }
    if (url.pathname === '/api/ask' && req.method === 'POST') {
      res.statusCode = 200
      res.setHeader('Content-Type', 'text/event-stream')
      res.end('event: delta\ndata: {"text":"42"}\n\nevent: done\ndata: {}\n\n')
      return
    }
    if (url.pathname === '/api/resources') return send(200, { items: [{ id: 'r1' }], total: 1 })
    if (documents[url.pathname]) return send(200, documents[url.pathname])
    return send(404, { error: { code: 'not_found' } })
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) })
  const endpoint = `http://127.0.0.1:${server.address().port}`
  return { seen, object: { start: async () => ({ state: 'ready' }),
    connection: () => ({ url: endpoint, token: TOKEN,
      handshake: { protocol_version: 'ddp-client/1', identity: LOCAL, profile: LOCAL_PROFILE } }) } }
}

async function hosts(t, { capabilities, loopbackCenters, resources, documents } = {}) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-source-test-'))
  const workspaces = new WorkspaceHandles()
  const runtime = await localDouble(t, { capabilities, resources, documents })
  const stored = new Map()
  const options = { workspaces, runtime: runtime.object, directory: path.join(temporary, 'client'), loopbackCenters,
    credentials: {
      async withCredential(pair, operation) {
        const secret = stored.get(pair.environmentId + '/' + pair.profileId)
        if (!secret) throw Object.assign(new Error('credential_required'), { code: 'credential_required' })
        return operation(secret)
      },
      async set(input) { stored.set(input.environmentId + '/' + input.profileId, input.secret); return {} },
      async clear(pair) { stored.delete(pair.environmentId + '/' + pair.profileId); return {} },
    } }
  const clients = await new ClientHost(options).initialize()
  // One hook, close before delete (same as client-host.test.mjs): Windows cannot remove a
  // directory while client.sqlite is open, and a throwing after-hook skips the later ones,
  // leaving the host and runtime double open so the test process never exits.
  t.after(async () => {
    try { await clients.close() } catch {} finally {
      await rm(temporary, { recursive: true, force: true })
    }
  })
  return { clients, options, runtime, temporary, stored }
}

async function connectLocalSource(t, clients, options, directory = path.join(options.directory, '..', 'workspace')) {
  await import('node:fs/promises').then(fs => fs.mkdir(directory, { recursive: true }))
  const workspaces = options.workspaces
  const selected = await workspaces.selectedByNativeDialog(directory)
  clients.selectWorkspace = async () => selected
  const opened = await clients.workspaceOpen()
  assert.ok(opened)
  for (let attempt = 0; attempt < 200; attempt++) {
    const view = clients.list().find(item => item.connectionId === opened.sourceId)?.view
    if (view?.transport === 'ready' && view.snapshot === 'current') break
    await delay(20)
    if (attempt === 199) assert.fail('local source did not become ready')
  }
  return opened
}

test('local proxy forwards any method with the process token and never the renderer Authorization', async t => {
  const { clients, options, runtime } = await hosts(t)
  const opened = await connectLocalSource(t, clients, options)
  const proxied = await clients.apiProxy({ method: 'POST', path: '/api/ask',
    headers: { Authorization: 'Bearer renderer-forgery', 'Content-Type': 'application/json', Cookie: 'a=b',
      Origin: 'ddp://app', Referer: 'ddp://app/index.html' },
    body: Buffer.from('{"q":"hi"}') })
  assert.equal(proxied.status, 200)
  assert.equal(proxied.headers['X-DDP-Source'], opened.sourceId)
  assert.ok(runtime.seen.authorization.every(value => value === 'Bearer ' + TOKEN))
  assert.ok(runtime.seen.authorization.length > 0)
  assert.ok(!runtime.seen.paths.some(line => line.includes('renderer-forgery')))
  // Renderer Cookie/Origin/Referer never reach the runtime.
  assert.ok(runtime.seen.forwarded.every(values => values.every(value => value === undefined)), JSON.stringify(runtime.seen.forwarded))
  const text = await new Response(proxied.body).text()
  assert.match(text, /event: delta/)
})

test('local proxy never reaches the runtime private protocol: plan approval stays behind the native dialog', async t => {
  const { clients, options, runtime } = await hosts(t)
  await connectLocalSource(t, clients, options)
  const before = runtime.seen.paths.length
  for (const [method, path] of [['POST', '/api/v1/plans/plan-1/approve'], ['POST', '/api/%761/plans/plan-1/approve'],
    ['GET', '/api/v1/plans'], ['POST', '/api/v1/models/m/install'], ['GET', '/api/v1/client/snapshot']]) {
    await assert.rejects(clients.apiProxy({ method, path, headers: {},
      body: method === 'POST' ? Buffer.from(JSON.stringify({ phase: 'execution', user_confirmed: true })) : undefined }),
    error => ['not_supported_locally', 'invalid_arguments'].includes(error.code), method + ' ' + path)
  }
  assert.equal(runtime.seen.paths.length, before, 'blocked requests must not reach the runtime')
  const capabilities = await clients.apiProxy({ method: 'GET', path: '/api/v1/capabilities', headers: {} })
  assert.equal(capabilities.status, 200)
  await new Response(capabilities.body).arrayBuffer()
})

test('center non-GET is rejected with zero network I/O', async t => {
  const { clients } = await hosts(t)
  const seen = centerDouble(t)
  const connected = await clients.centerConnect({ endpoint: CENTER,
    username: 'alice', password: 's3cret', persist: false }, { packaged: false })
  assert.equal(connected.kind, 'center')
  const before = seen.posts
  await assert.rejects(clients.apiProxy({ sourceId: connected.sourceId, method: 'POST',
    path: '/api/documents/doc-1/conversations', headers: {}, body: Buffer.from('{}') }),
  { code: 'approved_plan_required' })
  assert.equal(seen.posts, before)
  await assert.rejects(clients.apiProxy({ sourceId: connected.sourceId, method: 'DELETE',
    path: '/api/resources/r1', headers: {} }), { code: 'approved_plan_required' })
  assert.equal(seen.posts, before)
})

test('center 401 marks the source signed_out with the contract code', async t => {
  const { clients, stored } = await hosts(t)
  centerDouble(t)
  const connected = await clients.centerConnect({ endpoint: CENTER,
    username: 'alice', password: 's3cret', persist: true }, { packaged: false })
  // Expire the JWT: keep a valid broker entry but make the center answer 401.
  const realFetch = globalThis.fetch
  globalThis.fetch = async (input, init = {}) =>
    new Response('{}', { status: 401, headers: { 'Content-Type': 'application/json' } })
  try {
    await assert.rejects(clients.apiProxy({ sourceId: connected.sourceId, method: 'GET',
      path: '/api/resources', headers: {} }), { code: 'source_signed_out' })
  } finally { globalThis.fetch = realFetch }
  assert.ok(stored.size > 0, 'a stored JWT existed before the 401')
  const listed = await clients.sourceList()
  const entry = listed.find(item => item.sourceId === connected.sourceId)
  assert.equal(entry.state, 'signed_out')
  assert.equal(entry.reason, 'source_signed_out')
})

test('every proxied response carries X-DDP-Source; stale source requests are discarded', async t => {
  const { clients, options } = await hosts(t)
  const opened = await connectLocalSource(t, clients, options)
  const proxied = await clients.apiProxy({ method: 'GET', path: '/api/resources', headers: {} })
  assert.equal(proxied.headers['X-DDP-Source'], opened.sourceId)
  // A request naming a source that is no longer active is discarded.
  clients.selectWorkspace = async () => null
  await assert.rejects(clients.apiProxy({ sourceId: opened.sourceId + '-other', method: 'GET',
    path: '/api/resources', headers: {} }), error => ['source_changed', 'unknown_connection'].includes(error.code))
})

test('equal local and center names and document paths keep selection, cached projections and drafts isolated', async t => {
  const localResource = { id: 'r1', display_name: 'Report.pdf', publication: 'private' }
  const centerResource = { id: 'r1', display_name: 'Report.pdf', publication: 'published' }
  const route = '/api/documents/doc-1'
  const localDocument = { id: 'doc-1', filename: 'Report.pdf', resource_id: 'r1', status: 'succeeded', size_bytes: 111 }
  const centerDocument = { id: 'doc-1', filename: 'Report.pdf', resource_id: 'r1', status: 'succeeded', size_bytes: 222 }
  const { clients, options, runtime } = await hosts(t,
    { resources: [localResource], documents: { [route]: localDocument } })
  const directory = path.join(options.directory, '..', 'center.test')
  // Equal source labels and identical directory/endpoint path text are presentation,
  // never identity. Document and resource IDs deliberately collide as well.
  const endpoint = 'https://center.test' + directory
  const seen = centerDouble(t, { endpoint, resources: [centerResource], documents: { [route]: centerDocument } })
  const local = await connectLocalSource(t, clients, options, directory)
  const center = await clients.centerConnect({ endpoint, username: 'alice', password: 'secret', persist: true },
    { packaged: false })
  assert.equal(local.label, center.label)
  assert.notEqual(local.sourceId, center.sourceId)
  assert.notEqual(local.environment.environmentId, center.environment.environmentId)
  assert.deepEqual((await clients.sourceList()).map(item => [item.sourceId, item.active]),
    [[local.sourceId, false], [center.sourceId, true]])
  await clients.saveDraft({ connectionId: local.sourceId, key: 'document:doc-1', expectedRevision: 0,
    value: { question: 'Private notes' } })
  await clients.saveDraft({ connectionId: center.sourceId, key: 'document:doc-1', expectedRevision: 0,
    value: { question: 'Published notes' } })
  const readDocument = async (host, source, expected) => {
    const response = await host.apiProxy({ sourceId: source.sourceId, method: 'GET', path: route, headers: {} })
    assert.equal(response.headers['X-DDP-Source'], source.sourceId)
    assert.deepEqual(await new Response(response.body).json(), expected)
  }
  await readDocument(clients, center, centerDocument)
  const remoteReads = seen.paths.filter(p => p === directory + route).length
  await clients.sourceActivate({ sourceId: local.sourceId })
  await readDocument(clients, local, localDocument)
  assert.equal(seen.paths.filter(p => p === directory + route).length, remoteReads,
    'opening a local document must not query the identically named center document')
  const localReads = runtime.seen.paths.filter(p => p === 'GET ' + route).length
  await assert.rejects(clients.apiProxy({ sourceId: center.sourceId, method: 'GET', path: route, headers: {} }),
    { code: 'source_changed' })
  await clients.sourceActivate({ sourceId: center.sourceId })
  await readDocument(clients, center, centerDocument)
  assert.equal(runtime.seen.paths.filter(p => p === 'GET ' + route).length, localReads)
  await clients.close()
  const restarted = await new ClientHost(options).initialize()
  try {
    const restored = restarted.list()
    assert.equal(restored.length, 2)
    assert.deepEqual(restored.find(item => item.connectionId === local.sourceId).view.projection.state.resources,
      [localResource])
    assert.deepEqual(restored.find(item => item.connectionId === center.sourceId).view.projection.state.resources,
      [centerResource])
    assert.deepEqual(await restarted.readDraft({ connectionId: local.sourceId, key: 'document:doc-1' }),
      { revision: 1, value: { question: 'Private notes' } })
    assert.deepEqual(await restarted.readDraft({ connectionId: center.sourceId, key: 'document:doc-1' }),
      { revision: 1, value: { question: 'Published notes' } })
    assert.equal(restarted.activeSourceId(), center.sourceId)
    await restarted.restoreActiveSource()
    await readDocument(restarted, center, centerDocument)
    await restarted.sourceActivate({ sourceId: local.sourceId })
    await readDocument(restarted, local, localDocument)
    assert.deepEqual((await restarted.sourceList()).map(item => [item.sourceId, item.active]),
      [[local.sourceId, true], [center.sourceId, false]])
  } finally { await restarted.close() }
})

test('active source persists across restart; no source → 503 no_active_source', async t => {
  const { clients, options } = await hosts(t)
  await assert.rejects(clients.apiProxy({ method: 'GET', path: '/api/resources', headers: {} }),
    { code: 'no_active_source' })
  const opened = await connectLocalSource(t, clients, options)
  await clients.close()
  const restarted = await new ClientHost(options).initialize()
  try {
    const listed = await restarted.sourceList()
    assert.equal(listed.find(item => item.sourceId === opened.sourceId)?.active, true)
    // Restored but not yet reconnected: the page must not treat it as ready.
    assert.notEqual(listed.find(item => item.sourceId === opened.sourceId)?.state, 'ready')
    const states = []
    const unsubscribe = restarted.onSourceChange(summaries =>
      states.push(summaries.find(item => item.sourceId === opened.sourceId)?.state))
    await restarted.restoreActiveSource()
    unsubscribe()
    assert.equal(states[0], 'connecting')
    for (let attempt = 0; attempt < 200; attempt++) {
      const now = (await restarted.sourceList()).find(item => item.sourceId === opened.sourceId)
      if (now.state === 'ready') break
      if (attempt === 199) assert.fail('restored source did not become ready')
      await delay(20)
    }
    const proxied = await restarted.apiProxy({ method: 'GET', path: '/api/resources', headers: {} })
    assert.equal(proxied.status, 200)
    await new Response(proxied.body).arrayBuffer()
  } finally { await restarted.close() }
})

test('centerConnect derives identity from the handshake and never persists the password', async t => {
  const { clients, stored } = await hosts(t)
  const seen = centerDouble(t)
  const connected = await clients.centerConnect({ endpoint: CENTER + '/',
    username: 'alice', password: 's3cret-password', persist: true }, { packaged: false })
  assert.equal(connected.environment.environmentId, NODE)
  assert.equal(connected.environment.authorityNodeId, NODE)
  assert.equal(connected.profile.subject, 'user-alice')
  assert.equal(connected.kind, 'center')
  assert.equal(connected.readOnly, true)
  assert.deepEqual(connected.features, ['resources', 'documents', 'search', 'wiki', 'federation_tasks'])
  assert.equal(connected.label, 'center.test')
  assert.equal(seen.logins, 1)
  assert.ok(seen.handshakes >= 1, 'login handshake runs at least once; the shared provider may re-handshake')
  // Only the JWT is stored; the password appears nowhere on disk or in memory.
  const secrets = [...stored.values()].join('\n')
  assert.ok(secrets.includes(SECRET))
  assert.ok(!secrets.includes('s3cret-password'))
  const listed = await clients.sourceList()
  assert.ok(JSON.stringify(listed).includes(SECRET) === false)
  assert.equal(connected.state, 'ready')
})

test('sourceReconnect wakes a retry-exhausted non-active center without switching the workspace', async t => {
  const { clients, options } = await hosts(t)
  const seen = centerDouble(t)
  const center = await clients.centerConnect({ endpoint: CENTER,
    username: 'alice', password: 's3cret', persist: false }, { packaged: false })
  const local = await connectLocalSource(t, clients, options)
  seen.offline = true
  const deadline = Date.now() + 25000
  while (clients.list().find(item => item.connectionId === center.sourceId)?.view.transport !== 'blocked') {
    assert.ok(Date.now() < deadline, 'center did not exhaust its bounded retry budget')
    await delay(25)
  }
  assert.equal((await clients.sourceList()).find(item => item.sourceId === center.sourceId).state, 'unavailable')
  seen.offline = false
  const updates = []
  const unsubscribe = clients.onSourceChange(sources => updates.push(sources))
  const reconnected = await clients.sourceReconnect({ sourceId: center.sourceId })
  unsubscribe()
  assert.equal(reconnected.state, 'ready')
  assert.equal(reconnected.active, false)
  assert.equal(clients.activeSourceId(), local.sourceId)
  assert.equal(updates[0].find(item => item.sourceId === center.sourceId).state, 'connecting')
  assert.equal(updates.at(-1).find(item => item.sourceId === center.sourceId).state, 'ready')
  assert.ok(updates.every(sources => sources.find(item => item.sourceId === local.sourceId).active))
  const proxied = await clients.apiProxy({ method: 'GET', path: '/api/resources', headers: {} })
  assert.equal(proxied.headers['X-DDP-Source'], local.sourceId)
  assert.deepEqual(JSON.parse(await new Response(proxied.body).text()), { items: [{ id: 'r1' }], total: 1 })
})

test('sourceReconnect rejects a local source before runtime work and leaves the active source unchanged', async t => {
  const { clients, options, runtime } = await hosts(t)
  const local = await connectLocalSource(t, clients, options)
  const before = await clients.sourceList()
  const starts = []
  runtime.object.start = async workspaceId => { starts.push(workspaceId); return { state: 'ready' } }
  await assert.rejects(clients.sourceReconnect({ sourceId: local.sourceId }), { code: 'invalid_arguments' })
  assert.deepEqual(starts, [], 'remote-only reconnect must not start a local runtime')
  assert.equal(clients.activeSourceId(), local.sourceId)
  assert.deepEqual(await clients.sourceList(), before)
})

test('a dev loopback http center becomes ready only when the host allows loopback centers', async t => {
  // Unpackaged builds accept http://127.0.0.1 centers (source policy); the shared
  // provider must then accept the same endpoint, or the source is stuck unavailable.
  const endpoint = 'http://127.0.0.1:45999/team'
  for (const loopbackCenters of [true, false]) {
    const { clients } = await hosts(t, { loopbackCenters })
    centerDouble(t, { endpoint })
    const connected = await clients.centerConnect({ endpoint, username: 'alice',
      password: 's3cret-password', persist: false }, { packaged: false })
    assert.equal(connected.state, loopbackCenters ? 'ready' : 'unavailable', String(loopbackCenters))
  }
})

test('workspaceOpen cancel → null; sourceRemove never deletes workspace data', async t => {
  const { clients, options, temporary } = await hosts(t)
  clients.selectWorkspace = async () => null
  assert.equal(await clients.workspaceOpen(), null)
  const opened = await connectLocalSource(t, clients, options)
  assert.equal(opened.kind, 'local')
  assert.equal(opened.readOnly, false)
  const workspaceDir = path.join(options.directory, '..', 'workspace')
  assert.equal((await import('node:fs/promises').then(fs => fs.stat(workspaceDir))).isDirectory(), true)
  assert.equal(await clients.sourceRemove({ sourceId: opened.sourceId }), null)
  assert.equal((await import('node:fs/promises').then(fs => fs.stat(workspaceDir))).isDirectory(), true)
  assert.equal((await clients.sourceList()).length, 0)
})

test('sourceReconnect is a fixed schema-validated source operation', () => {
  assert.equal(SOURCE_CHANNELS.sourceReconnect, 'ddp:source-reconnect')
  assert.deepEqual(sourceArguments('sourceReconnect', { sourceId: 'connection-1' }), { sourceId: 'connection-1' })
  for (const input of [undefined, {}, { sourceId: '../x' }, { sourceId: '' }, { sourceId: 1 },
    { sourceId: 'connection-1', endpoint: CENTER }, { sourceId: 'connection-1', shell: 'id' }]) {
    assert.throws(() => sourceArguments('sourceReconnect', input), { code: 'invalid_arguments' })
  }
})

test('source policy rejects unknown fields, loopback in packaged builds, and bad input', () => {
  assert.throws(() => sourceArguments('sourceActivate', { sourceId: 'x', endpoint: CENTER }), /invalid_arguments/)
  assert.throws(() => sourceArguments('centerConnect',
    { endpoint: CENTER, username: 'a', password: 'b', persist: true, token: 'x' }, { packaged: false }),
  /invalid_arguments/)
  assert.throws(() => normalizeEndpoint('http://127.0.0.1:9/team', { packaged: true }), /invalid_endpoint/)
  assert.equal(normalizeEndpoint('http://127.0.0.1:9/team', { packaged: false }), 'http://127.0.0.1:9/team')
  // The shared provider only admits 127.0.0.1/[::1] for http; localhost would pass here and then stick.
  assert.throws(() => normalizeEndpoint('http://localhost:9/team', { packaged: false }), /invalid_endpoint/)
  // Storage origins follow the same loopback rule (dev only, no `localhost`).
  assert.equal(normalizeOrigin('http://[::1]:9000', { packaged: false }), 'http://[::1]:9000')
  assert.throws(() => normalizeOrigin('http://localhost:9000', { packaged: false }), /invalid_endpoint/)
  assert.throws(() => normalizeOrigin('http://127.0.0.1:9000', { packaged: true }), /invalid_endpoint/)
  assert.throws(() => normalizeEndpoint('https://user:pass@center.test/team'), /invalid_endpoint/)
  assert.throws(() => sourceArguments('sourceRemove', { sourceId: '../x' }), /invalid_arguments/)
})

test('_object hides the presigned URL: rewrite + fetch with no Authorization', async t => {
  const { clients } = await hosts(t)
  centerDouble(t)
  const connected = await clients.centerConnect({ endpoint: CENTER,
    username: 'alice', password: 's3cret', persist: false }, { packaged: false })
  const serve = staticUI('/nonexistent-ui-root', uiLocation(), { clients: () => clients })
  const proxied = await serve(new Request('ddp://app/api/documents/doc-1/download-url'))
  assert.equal(proxied.status, 200)
  assert.equal(proxied.headers.get('X-DDP-Source'), connected.sourceId)
  assert.equal(proxied.headers.get('Content-Length'), null, 'upstream length no longer matches the rewritten body')
  const text = await proxied.text()
  assert.ok(!text.includes('presigned-secret') && !text.includes('X-Amz'), 'presigned URL must not reach the renderer: ' + text)
  const match = JSON.parse(text).url.match(/^ddp:\/\/app\/_object\/([A-Za-z0-9_.-]+)$/)
  assert.ok(match, 'center object URL rewritten to exactly one _object id, got: ' + text)
  // Fetch the opaque id through the host protocol handler: no Authorization sent.
  let authorization = 'unset'
  const realFetch = globalThis.fetch
  globalThis.fetch = async (input, init = {}) => {
    authorization = init.headers?.Authorization ?? init.headers?.authorization ?? null
    return new Response('%PDF-bytes', { headers: { 'Content-Type': 'application/pdf' } })
  }
  try {
    const response = await serve(new Request('ddp://app/_object/' + match[1]))
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('X-DDP-Source'), connected.sourceId)
    assert.equal(await response.text(), '%PDF-bytes')
    assert.equal(authorization, null)
  } finally { globalThis.fetch = realFetch }
})
test('unparsable center JSON fails closed instead of passing raw object URLs through', async t => {
  const { clients } = await hosts(t)
  centerDouble(t)
  await clients.centerConnect({ endpoint: CENTER, username: 'alice', password: 's3cret', persist: false }, { packaged: false })
  const serve = staticUI('/nonexistent-ui-root', uiLocation(), { clients: () => clients })
  const proxied = await serve(new Request('ddp://app/api/documents/doc-broken/download-url'))
  const text = await proxied.text()
  assert.equal(proxied.status, 400)
  assert.equal(JSON.parse(text).error.code, 'protocol_incompatible')
  assert.ok(!text.includes('presigned-secret'), text)
})
test('packaged CSP connect-src allows ddp://app; static files still served', async t => {
  const packaged = contentSecurityPolicy(uiLocation())
  assert.match(packaged, /connect-src[^;]*ddp:\/\/app/)
  const root = await mkdtemp(path.join(os.tmpdir(), 'ddp-static-test-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const fs = await import('node:fs/promises')
  await fs.writeFile(path.join(root, 'index.html'), '<main>app</main>')
  const serve = staticUI(root, uiLocation())
  const response = await serve(new Request('ddp://app/index.html'))
  assert.equal(response.status, 200)
})

test('source change events reach renderer listeners', async t => {
  const { clients, options } = await hosts(t)
  const seen = []
  const unsubscribe = clients.onSourceChange(summaries => seen.push(summaries))
  const opened = await connectLocalSource(t, clients, options)
  assert.ok(seen.length > 0)
  assert.ok(seen.at(-1).some(item => item.sourceId === opened.sourceId && item.active))
  unsubscribe()
})

test('clientQuery on a center connection is refused: centers read only through the /api proxy', async t => {
  const { clients } = await hosts(t)
  const seen = centerDouble(t)
  void seen
  const connected = await clients.centerConnect({ endpoint: CENTER,
    username: 'alice', password: 's3cret', persist: false }, { packaged: false })
  await assert.rejects(clients.query({ connectionId: connected.sourceId,
    name: 'resource.page', payload: { snapshot_id: 's', cursor: 'c' } }),
  { code: 'approved_plan_required' })
  await assert.rejects(clients.query({ connectionId: connected.sourceId,
    name: 'models.list', payload: {} }), { code: 'approved_plan_required' })
})
