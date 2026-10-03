import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readdir, rm, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { OwnedRuntimeManager } from '../src/runtime.mjs'
import { ClientHost, clientFailure } from '../src/client-host.mjs'
import { clientArguments } from '../src/client-policy.mjs'
import { nativeRuntimeSkipReason } from './helpers/platform.mjs'

test('renderer command IPC is local model management only; content writes cannot bypass the /api proxy', () => {
  const command = (name, payload) => clientArguments('clientCommand', { connectionId: 'known', name, payload, idempotencyKey: 'operation-0001' })
  assert.equal(command('models.install', { model_id: 'qwen3-1.7b' }).name, 'models.install')
  assert.equal(command('models.start', { model_id: 'qwen3-1.7b', runtime_id: 'llama-cpp-cpu' }).name, 'models.start')
  assert.equal(command('models.stop', {}).name, 'models.stop')
  for (const [name, payload] of [['models.start', { model_id: '../x' }], ['models.start', { model_id: 'm', runtime_id: 'Bad Id' }],
    ['models.stop', { model_id: 'm' }], ['models.install', { model_id: 'm', endpoint: 'https://other' }]])
    assert.throws(() => command(name, payload), /invalid_arguments/, name)
  const body = { title: 'Manual', sources: [{ resource_id: 'resource-1', source_version_id: 'version-1' }], execution_policy: 'local_only', allow_remote: false }
  for (const [name, payload] of [['wiki.create', { body }], ['answer.generate', { query: 'q', version_ids: null, execution_policy: 'local_only', allow_remote: false }],
    ['version.delete', { version_id: 'v1' }], ['resource.delete', { resource_id: 'r1' }], ['task.cancel', { task_id: 't1' }]])
    assert.throws(() => command(name, payload), /unsupported_operation/, name)
  for (const name of ['wiki.list', 'corpus.search', 'evidence.get', 'task.page', 'resource.page'])
    assert.throws(() => clientArguments('clientQuery', { connectionId: 'known', name, payload: {} }), /unsupported_operation|invalid_arguments/, name)
  assert.equal(clientArguments('clientQuery', { connectionId: 'known', name: 'models.list', payload: {} }).name, 'models.list')
  assert.throws(() => clientArguments('clientQuery', { connectionId: 'known', name: 'models.list', payload: { endpoint: 'x' } }), /invalid_arguments/)
})
const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')

async function fixture(t, { native = true, socketControlPort } = {}) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-client-host-test-'))
  const workspaces = new WorkspaceHandles()
  let runtime = null
  const socketAudit = socketControlPort === undefined ? null : {
    file: path.join(temporary, 'runtime-sockets.jsonl'), pid: null,
  }
  if (native) {
    const [version] = (await readdir(path.join(repository, '.venv/lib'))).filter(name => /^python\d+\.\d+$/.test(name))
    runtime = new OwnedRuntimeManager({ workspaces, directory: path.join(temporary, 'sessions'),
      python: path.join(repository, '.venv/bin/python'), launcher: path.join(repository, 'apps/desktop/src/runtime-launcher.py'),
      cwd: repository, pythonPaths: ['ddp_local', 'ddp_core', 'ddp_contracts'].map(name => path.join(repository, 'python', name))
        .concat(path.join(repository, '.venv/lib', version, 'site-packages')),
      ...(socketAudit ? { spawnProcess: (command, args, options) => {
        const launcher = args[2]
        const child = spawn(command, [...args.slice(0, 2),
          path.join(repository, 'apps/desktop/test/helpers/runtime-network-audit.py'), ...args.slice(3)], {
          ...options, env: { ...options.env, DDP_TEST_REAL_RUNTIME_LAUNCHER: launcher,
            DDP_TEST_SOCKET_AUDIT_FILE: socketAudit.file, DDP_TEST_SOCKET_CONTROL_PORT: String(socketControlPort) },
        })
        socketAudit.pid = child.pid
        return child
      } } : {}) })
  }
  const stored = new Map()
  const options = { workspaces, runtime: runtime ?? {}, directory: path.join(temporary, 'client'),
    credentials: { withCredential: () => assert.fail('local runtime must not use remote credentials'),
      set: async input => { stored.set(input.environmentId + '/' + input.profileId, input.secret); return {} },
      clear: async pair => { stored.delete(pair.environmentId + '/' + pair.profileId); return {} } } }
  const clients = await new ClientHost(options).initialize()
  t.after(async () => {
    try { await clients.close() } finally {
      if (runtime) await runtime.shutdown()
      await rm(temporary, { recursive: true, force: true })
    }
  })
  await mkdir(path.join(temporary, 'workspace'))
  const selected = await workspaces.selectedByNativeDialog(path.join(temporary, 'workspace'))
  return { clients, options, runtime, selected, temporary, stored, socketAudit }
}
async function current(clients, id) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const view = clients.list().find(entry => entry.connectionId === id)
    if (view.view.transport === 'ready' && view.view.snapshot === 'current') return view
    if (view.view.transport === 'blocked') assert.fail(JSON.stringify(view))
    await delay(50)
  }
  assert.fail('client did not become current')
}

test('real shared client persists scoped projection/draft and the source registry owns activation',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { clients, runtime, selected, options } = await fixture(t)
  const first = await clients.connectLocal(selected)
  await current(clients, first.connectionId)
  // No renderer view subscriptions exist anymore: activation state comes from sourceList.
  clients.selectWorkspace = async () => selected
  const opened = await clients.workspaceOpen()
  assert.equal(opened.sourceId, first.connectionId)
  assert.equal((await clients.sourceList()).find(entry => entry.sourceId === first.connectionId).active, true)
  await clients.saveDraft({ connectionId: first.connectionId, key: 'query', expectedRevision: 0, value: { text: 'my draft' } })
  await assert.rejects(clients.saveDraft({ connectionId: first.connectionId, key: 'query', expectedRevision: 0, value: 'stale' }), /draft_conflict/)
  assert.equal(runtime.status(selected.workspaceId).state, 'ready')
  await clients.sourceRemove({ sourceId: first.connectionId })
  assert.equal((await clients.sourceList()).find(entry => entry.sourceId === first.connectionId), undefined)
  await assert.rejects(clients.query({ connectionId: first.connectionId, name: 'models.list', payload: {} }), /unknown_connection/)
  await clients.close()
  const restarted = await new ClientHost(options).initialize()
  try {
    assert.equal(restarted.list().length, 0, 'removal deletes the registration; restart must not resurrect it')
    assert.throws(() => restarted.readDraft({ connectionId: first.connectionId, key: 'query' }), /unknown_connection/)
  } finally { await restarted.close() }
})

test('a persisted WSL local connection rehydrates as a WSL workspace without client_configuration_invalid', async t => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-client-host-wsl-'))
  t.after(() => rm(temporary, { recursive: true, force: true }))
  const workspaces = new WorkspaceHandles()
  const directory = '~/.deepdocparse/workspaces/default'
  const workspace = workspaces.selectedWsl({ directory })
  // A WSL handle is virtual: no host path exists, and the handshake is faked so the
  // regression stays independent of the Linux-only native runtime.
  const url = 'http://127.0.0.1:1'
  const runtime = { start: async () => ({ state: 'ready' }),
    connection: () => ({ url, token: 'a'.repeat(64), handshake: { protocol_version: 'ddp-client/1',
      identity: { environment_id: 'wsl-env', workspace_id: 'wsl-workspace', authority_node_id: 'wsl-node' },
      profile: { issuer: 'wsl-node', subject: 'user-alice' } } }) }
  const options = { workspaces, runtime, directory: path.join(temporary, 'client'),
    credentials: { withCredential: () => assert.fail('local runtime must not use remote credentials') } }
  const clients = await new ClientHost(options).initialize()
  try {
    assert.equal((await clients.connectLocal({ workspaceId: workspace.workspaceId })).workspaceId, workspace.workspaceId)
  } finally { await clients.close() }
  const persisted = JSON.parse(await readFile(path.join(options.directory, 'connections.json'), 'utf8'))
  assert.deepEqual(persisted.map(entry => [entry.directory, entry.workspaceKind]), [[directory, 'wsl']])

  const restarted = await new ClientHost(options).initialize()
  try {
    const restored = restarted.list().find(entry => entry.workspaceId === workspace.workspaceId)
    assert.ok(restored, 'the persisted WSL workspace must be registered')
    assert.notEqual(restored.view.reason, 'workspace_unavailable')
  } finally { await restarted.close() }
})

test('legacy local entries without a workspace kind keep the absolute native directory gate', async t => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-client-host-legacy-'))
  t.after(() => rm(temporary, { recursive: true, force: true }))
  const directory = path.join(temporary, 'client')
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const configuration = path.join(directory, 'connections.json')
  const entry = { kind: 'local', label: 'Legacy', directory: 'relative/workspace',
    environment: { environmentId: 'legacy-env', workspaceId: 'legacy-workspace',
      authorityNodeId: 'legacy-node', endpoint: 'http://127.0.0.1:9' },
    profile: { profileId: 'legacy-profile', issuer: 'legacy-node', subject: 'user-alice' } }
  await writeFile(configuration, JSON.stringify([entry]), { mode: 0o600 })
  const options = { workspaces: new WorkspaceHandles(), runtime: {}, credentials: {}, directory }
  await assert.rejects(new ClientHost(options).initialize(), /client_configuration_invalid/)
  entry.directory = temporary
  await writeFile(configuration, JSON.stringify([entry]), { mode: 0o600 })
  const clients = await new ClientHost(options).initialize()
  try {
    assert.ok(clients.list()[0].workspaceId, 'a legacy absolute native directory must still be registered')
  } finally { await clients.close() }
})

test('fixed client schema rejects scope/path/URL injection and writes with implicit remote permission', () => {
  for (const [method, input] of [
    ['clientQuery', { connectionId: 'known', name: 'fetch', payload: { url: 'http://example.com' } }],
    ['clientQuery', { connectionId: 'known', name: 'corpus.search', payload: { query: 'x' } }],
    ['clientQuery', { connectionId: 'known', name: 'evidence.get', payload: { evidence_id: 'e1' } }],
    ['clientQuery', { connectionId: 'known', name: 'wiki.list', payload: {} }],
    ['clientQuery', { connectionId: 'known', name: 'task.page', payload: { snapshot_id: 's', cursor: 'c' } }],
    ['clientReadDraft', { connectionId: 'known', key: 'draft', scope: 'other' }],
    ['clientCommand', { connectionId: 'known', name: 'answer.generate', idempotencyKey: '12345678',
      payload: { query: 'x', version_ids: [], execution_policy: 'remote_allowed', allow_remote: true } }],
  ]) assert.throws(() => clientArguments(method, input))
  assert.deepEqual(clientFailure(new Error('private-token or URL')), { ok: false, error: { code: 'host_operation_failed' } })
})


test('native upload goes through the proxied content chain: session → parts → finalize → parse → source bytes', { skip: nativeRuntimeSkipReason() }, async t => {
  // Security intent of the deleted importFile/readOriginal/exportBundle IPC, kept via the
  // new surface: bytes enter only through the host /api proxy with the process token,
  // and the renderer-facing equivalents are same-origin proxy routes.
  const { clients, runtime, selected, temporary } = await fixture(t)
  const source = path.join(temporary, '技术手册.pdf')
  const bytes = await readFile(path.join(repository, 'tests/fixtures/sample.pdf'))
  await writeFile(source, bytes)
  clients.selectWorkspace = async () => selected
  const opened = await clients.workspaceOpen()
  assert.ok(opened)
  const readBY = async (method, path, body) => {
    const proxied = await clients.apiProxy({ method, path, headers: { 'Content-Type': 'application/pdf' }, body })
    assert.equal(proxied.headers['X-DDP-Source'], opened.sourceId)
    return JSON.parse(await new Response(proxied.body).text())
  }
  const session = await clients.apiProxy({ method: 'POST', path: '/api/uploads',
    headers: { 'Content-Type': 'application/json' },
    body: Buffer.from(JSON.stringify({ filename: '技术手册.pdf', size: bytes.length, mime: 'application/pdf' })) })
    .then(response => new Response(response.body).json())
  assert.ok(session.id)
  const put = await clients.apiProxy({ method: 'PUT', path: `/api/uploads/${session.id}/parts/1`,
    headers: { 'Content-Type': 'application/pdf' }, body: Buffer.from(bytes) })
  assert.equal(put.status, 200)
  const finalized = await readBY('POST', `/api/uploads/${session.id}/finalize`,
    Buffer.from(JSON.stringify({ engine: 'borndigital', options: {} })))
  assert.ok(finalized.resource_id && finalized.version_id, JSON.stringify(finalized))
  let document = null
  for (let attempt = 0; attempt < 120; attempt++) {
    const got = await readBY('GET', `/api/documents/${finalized.version_id}`)
    if (got.status === 'succeeded') { document = got; break }
    await delay(100)
  }
  assert.equal(document?.status, 'succeeded', JSON.stringify(document))
  const pdf = await clients.apiProxy({ method: 'GET', path: `/api/documents/${finalized.version_id}/source`, headers: {} })
  assert.equal(pdf.status, 200)
  assert.deepEqual(Buffer.from(await new Response(pdf.body).arrayBuffer()), bytes)
  // A second finalize with the same session is idempotent, not a second version.
  const again = await readBY('POST', `/api/uploads/${session.id}/finalize`,
    Buffer.from(JSON.stringify({ engine: 'borndigital', options: {} })))
  assert.equal(again.version_id, finalized.version_id)
})

test('missing local model rejects desktop answers and Wiki without remote fetches or credential use',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const control = createServer(socket => socket.end())
  await new Promise(resolve => control.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => control.close(resolve)))
  const socketControlPort = control.address().port
  const { clients, runtime, selected, socketAudit } = await fixture(t, { socketControlPort })
  const fetched = [], credentialUse = []
  const actualFetch = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    fetched.push({ url: typeof input === 'string' ? input : input.url, method: init?.method ?? 'GET' })
    return actualFetch(input, init)
  }
  t.after(() => { globalThis.fetch = actualFetch })
  clients.credentials.withCredential = async pair => {
    credentialUse.push(pair)
    assert.fail('a missing local model must not request a remote secret')
  }
  clients.selectWorkspace = async () => selected
  const opened = await clients.workspaceOpen()
  const localOrigin = runtime.connection(selected.workspaceId).url
  const proxy = async (method, route, body, headers = {}) => {
    const response = await clients.apiProxy({ sourceId: opened.sourceId, method, path: route,
      headers: { 'Content-Type': 'application/json', ...headers },
      ...(body === undefined ? {} : { body: Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body)) }) })
    assert.equal(response.headers['X-DDP-Source'], opened.sourceId)
    return new Response(response.body, { status: response.status, headers: response.headers })
  }
  const models = await clients.query({ connectionId: opened.sourceId, name: 'models.list', payload: {} })
  assert.equal(models.items.find(item => item.manifest.id === 'qwen3-1.7b-q8_0').status, 'not_installed')
  await assert.rejects(clients.command({ connectionId: opened.sourceId, name: 'models.start',
    payload: { model_id: 'qwen3-1.7b-q8_0' }, idempotencyKey: 'missing-model-start' }),
  { code: 'model_not_installed' })

  const bytes = await readFile(path.join(repository, 'tests/fixtures/sample.pdf'))
  const upload = await (await proxy('POST', '/api/uploads',
    { filename: 'sample.pdf', size: bytes.length, mime: 'application/pdf' })).json()
  assert.equal((await proxy('PUT', `/api/uploads/${upload.id}/parts/1`, bytes,
    { 'Content-Type': 'application/pdf' })).status, 200)
  const finalized = await (await proxy('POST', `/api/uploads/${upload.id}/finalize`,
    { engine: 'borndigital', options: {} })).json()
  let document
  for (let attempt = 0; attempt < 120; attempt++) {
    document = await (await proxy('GET', `/api/documents/${finalized.version_id}`)).json()
    if (document.status === 'succeeded') break
    assert.notEqual(document.status, 'failed', JSON.stringify(document))
    await delay(100)
  }
  assert.equal(document.status, 'succeeded')
  const search = await clients.apiProxy({ method: 'GET', path: '/api/search',
    query: 'q=contract&doc=' + finalized.version_id, headers: {} })
  const hits = await new Response(search.body).json()
  assert.ok(hits.groups.some(group => group.hits.length > 0),
    'the question must reach model generation, not the no-evidence answer path')
  const conversation = await (await proxy('POST',
    `/api/documents/${finalized.version_id}/conversations`)).json()
  const asked = await proxy('POST', `/api/conversations/${conversation.id}/ask`, { question: 'contract' })
  assert.equal(asked.status, 200)
  const events = (await asked.text()).trim().split('\n\n').map(block => {
    const [event, data] = block.split('\n')
    return [event.slice(7), JSON.parse(data.slice(6))]
  })
  assert.deepEqual(events.map(([name]) => name), ['error'])
  assert.equal(events[0][1].code, 'model_unavailable')
  assert.match(events[0][1].message, /local instruction model/)
  assert.deepEqual(await (await proxy('GET', `/api/conversations/${conversation.id}/messages`)).json(), [])
  const wiki = await proxy('POST', '/api/wikis',
    { title: 'Contract', sources: [{ resource_id: finalized.resource_id,
      source_version_id: finalized.version_id }] }, { 'Idempotency-Key': 'missing-model-wiki' })
  assert.equal(wiki.status, 503)
  assert.equal((await wiki.json()).error.code, 'model_unavailable')
  assert.deepEqual(await (await proxy('GET', '/api/wikis')).json(), [])
  assert.ok(fetched.some(call => call.url.endsWith(`/api/conversations/${conversation.id}/ask`)))
  assert.ok(fetched.some(call => call.url.endsWith('/api/wikis') && call.method === 'POST'))
  assert.deepEqual(fetched.filter(call => new URL(call.url).origin !== localOrigin), [],
    'all host HTTP calls stay on the owned native runtime; no center/model fallback')
  assert.deepEqual(credentialUse, [], 'no remote secret is read')
  const sockets = (await readFile(socketAudit.file, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
  assert.ok(sockets.some(row => row.event === 'socket.getaddrinfo' &&
    row.host === '127.0.0.1' && row.port === socketControlPort && row.pid === socketAudit.pid),
  'DNS observer must be active in the owned runtime process, not a separate probe')
  assert.ok(sockets.some(row => row.event === 'socket.connect' &&
    row.host === '127.0.0.1' && row.port === socketControlPort && row.pid === socketAudit.pid),
  'socket observer must see the real positive-control connection in the runtime process')
  assert.deepEqual(sockets.filter(row => !row.loopback_or_unix), [],
    'the owned Python runtime must not attempt remote DNS or socket connections when its model is missing')
})

test('lost upload finalize response is reconciled by re-reading the session, never by re-sending bytes', { skip: nativeRuntimeSkipReason() }, async t => {
  const { clients, selected } = await fixture(t)
  clients.selectWorkspace = async () => selected
  const opened = await clients.workspaceOpen()
  const bytes = await readFile(path.join(repository, 'tests/fixtures/sample.pdf'))
  const created = await new Response((await clients.apiProxy({ method: 'POST', path: '/api/uploads',
    headers: { 'Content-Type': 'application/json' },
    body: Buffer.from(JSON.stringify({ filename: 'sample.pdf', size: bytes.length, mime: 'application/pdf' })) })).body).json()
  await clients.apiProxy({ method: 'PUT', path: `/api/uploads/${created.id}/parts/1`,
    headers: { 'Content-Type': 'application/pdf' }, body: Buffer.from(bytes) })
  const finalized = await new Response((await clients.apiProxy({ method: 'POST', path: `/api/uploads/${created.id}/finalize`,
    headers: { 'Content-Type': 'application/json' }, body: Buffer.from(JSON.stringify({ engine: 'borndigital', options: {} })) })).body).json()
  assert.ok(finalized.version_id)
  // Re-reading the finalized session returns the same version: no byte replay happened.
  const reread = await new Response((await clients.apiProxy({ method: 'GET', path: `/api/uploads/${created.id}`, headers: {} })).body).json()
  assert.equal(reread.version_id, finalized.version_id)
  assert.equal(reread.status, 'ready')
})

test('model operations accept registry IDs only, without arbitrary engine arguments or endpoints', () => {
  for (const name of ['models.install', 'models.start']) {
    const request = { connectionId: 'known', name, payload: { model_id: 'qwen3-1.7b-q8_0' }, idempotencyKey: 'model-operation-1' }
    assert.equal(clientArguments('clientCommand', request).name, name)
    assert.throws(() => clientArguments('clientCommand', { ...request, payload: { model_id: '../outside' } }))
    assert.throws(() => clientArguments('clientCommand', { ...request, payload: { model_id: 'model', endpoint: 'http://other' } }))
  }
  assert.equal(clientArguments('clientCommand', { connectionId: 'known', name: 'models.stop', payload: {}, idempotencyKey: 'model-operation-2' }).name, 'models.stop')
})

test('centerConnect proves the node before storing any credential or registering the source', async t => {
  const { clients, stored } = await fixture(t, { native: false })
  let credentialsWritten = 0
  const realSet = clients.credentials.set
  clients.credentials.set = async input => { credentialsWritten++; return realSet(input) }
  const actualFetch = globalThis.fetch, requested = []
  globalThis.fetch = async input => { requested.push(new URL(typeof input === 'string' ? input : input.url).pathname); return new Response(JSON.stringify({ authority_node_id: 'node-unproven',
    public_key: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', proof: {} }),
  { headers: { 'Content-Type': 'application/json' } }) }
  try {
    await assert.rejects(clients.centerConnect({ endpoint: 'https://example.invalid/team',
      username: 'alice', password: 'secret', persist: true }), /identity_mismatch/)
    assert.equal(credentialsWritten, 0, 'no JWT stored before the node proof verifies')
    assert.deepEqual(requested, ['/api/v1/federation/node'], 'the password is never sent to an unproven node')
    assert.equal((await clients.sourceList()).length, 0, 'no source registered on proof failure')
    assert.equal(stored?.size ?? 0, 0)
  } finally { globalThis.fetch = actualFetch }
})


test('centerConnect validates endpoint shape before any network call', async t => {
  const { clients } = await fixture(t, { native: false })
  let fetched = 0
  const actualFetch = globalThis.fetch
  globalThis.fetch = async () => { fetched++; return new Response('{}', { headers: { 'Content-Type': 'application/json' } }) }
  try {
    for (const endpoint of ['http://example.invalid/team', 'https://secret@example.invalid/team',
      'https://example.invalid/team?redirect=other', 'https://example.invalid/team#other']) {
      await assert.rejects(clients.centerConnect({ endpoint, username: 'alice',
        password: 'secret', persist: false }), /invalid_endpoint/)
    }
    assert.equal(fetched, 0, 'invalid endpoints never reach the network')
  } finally { globalThis.fetch = actualFetch }
})
