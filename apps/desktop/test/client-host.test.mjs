import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readdir, rm, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { OwnedRuntimeManager } from '../src/runtime.mjs'
import { ClientHost, clientFailure } from '../src/client-host.mjs'
import { clientArguments } from '../src/client-policy.mjs'
import { nativeRuntimeSkipReason } from './helpers/platform.mjs'

test('Wiki IPC accepts bounded fixed revisions and rejects path, policy and source injection',()=>{
  const command=(name,payload)=>clientArguments('clientCommand',{connectionId:'known',name,payload,idempotencyKey:'wiki-operation-1'})
  const body={title:'Manual',sources:[{resource_id:'resource-1',source_version_id:'version-1'}],execution_policy:'local_only',allow_remote:false}
  assert.equal(command('wiki.create',{body}).name,'wiki.create')
  assert.equal(command('wiki.edit',{wiki_id:'wiki-1',page_key:'page-1',body:{base_revision_id:'revision-1',paragraphs:[{id:'human-1',text:'Human edit'}]}}).name,'wiki.edit')
  for(const payload of [{body:{...body,allow_remote:true}}, {body:{...body,endpoint:'https://other'}},
    {body:{...body,sources:[{resource_id:'r',source_version_id:'../outside'}]}},
    {body:{...body,max_output_tokens:100000}}, {body:{...body,sources:[]}}])assert.throws(()=>command('wiki.create',payload))
  assert.throws(()=>command('wiki.edit',{wiki_id:'wiki-1',page_key:'../source',body:{base_revision_id:'revision-1',paragraphs:[]}}))
  assert.throws(()=>clientArguments('clientQuery',{connectionId:'known',name:'wiki.list',payload:{url:'https://other'}}))
})
const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')

async function fixture(t, { native = true } = {}) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-client-host-test-'))
  const workspaces = new WorkspaceHandles()
  let runtime = null
  if (native) {
    const [version] = (await readdir(path.join(repository, '.venv/lib'))).filter(name => /^python\d+\.\d+$/.test(name))
    runtime = new OwnedRuntimeManager({ workspaces, directory: path.join(temporary, 'sessions'),
      python: path.join(repository, '.venv/bin/python'), launcher: path.join(repository, 'apps/desktop/src/runtime-launcher.py'),
      cwd: repository, pythonPaths: ['ddp_local', 'ddp_core', 'ddp_contracts'].map(name => path.join(repository, 'python', name))
        .concat(path.join(repository, '.venv/lib', version, 'site-packages')) })
  }
  const options = { workspaces, runtime: runtime ?? {}, directory: path.join(temporary, 'client'),
    credentials: { withCredential: () => assert.fail('local runtime must not use remote credentials') } }
  const clients = await new ClientHost(options).initialize()
  t.after(async () => {
    try { await clients.close() } finally {
      if (runtime) await runtime.shutdown()
      await rm(temporary, { recursive: true, force: true })
    }
  })
  await mkdir(path.join(temporary, 'workspace'))
  const selected = await workspaces.selectedByNativeDialog(path.join(temporary, 'workspace'))
  return { clients, options, runtime, selected, temporary }
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

test('real shared client persists scoped projection/draft and subscriptions never own the process',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { clients, runtime, selected, options } = await fixture(t)
  const first = await clients.connectLocal(selected)
  await current(clients, first.connectionId)
  const messages = []
  const snapshot = clients.subscribe({ connectionId: first.connectionId, subscriptionId: 'view-one' }, value => messages.push(value))
  assert.equal(snapshot.view.projection.state.resources.length, 0)
  await clients.saveDraft({ connectionId: first.connectionId, key: 'query', expectedRevision: 0, value: { text: 'my draft' } })
  await assert.rejects(clients.saveDraft({ connectionId: first.connectionId, key: 'query', expectedRevision: 0, value: 'stale' }), /draft_conflict/)
  clients.unsubscribe({ subscriptionId: 'view-one' })
  assert.equal(runtime.status(selected.workspaceId).state, 'ready')
  await clients.disconnect({ connectionId: first.connectionId })
  assert.equal(runtime.status(selected.workspaceId).state, 'ready')
  await assert.rejects(clients.query({ connectionId: first.connectionId, name: 'models.list', payload: {} }), /connection_not_current/)
  await clients.wake({ connectionId: first.connectionId }); await current(clients, first.connectionId)
  assert.equal(runtime.status(selected.workspaceId).generation, 1)
  const models = await clients.query({ connectionId: first.connectionId, name: 'models.list', payload: {} })
  assert.ok(models)
  await clients.close()
  const restarted = await new ClientHost(options).initialize()
  try {
    const restored = restarted.list()[0]
    assert.equal(restored.connectionId, first.connectionId)
    assert.equal(restored.view.snapshot, 'stale'); assert.equal(restored.view.transport, 'disconnected')
    assert.deepEqual(await restarted.readDraft({ connectionId: first.connectionId, key: 'query' }),
      { revision: 1, value: { text: 'my draft' } })
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
    ['clientReadDraft', { connectionId: 'known', key: 'draft', scope: 'other' }],
    ['clientImportFile', { connectionId: 'known', kind: 'pdf', idempotencyKey: '12345678', path: '/etc/passwd' }],
    ['clientCommand', { connectionId: 'known', name: 'answer.generate', idempotencyKey: '12345678',
      payload: { query: 'x', version_ids: [], execution_policy: 'remote_allowed', allow_remote: true } }],
  ]) assert.throws(() => clientArguments(method, input))
  assert.deepEqual(clientFailure(new Error('private-token or URL')), { ok: false, error: { code: 'host_operation_failed' } })
})


test('native Chinese PDF import has one durable receipt, fixed source bytes and validated atomic bundle export',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { clients, runtime, selected, temporary } = await fixture(t)
  const source = path.join(temporary, '技术手册.pdf')
  const bytes = await readFile(path.join(repository, 'tests/fixtures/sample.pdf'))
  await writeFile(source, bytes)
  clients.selectInput = async () => source
  clients.selectOutput = async () => path.join(temporary, 'export.ddp.zip')
  const connection = await clients.connectLocal(selected)
  await current(clients, connection.connectionId)
  const input = { connectionId: connection.connectionId, kind: 'pdf', idempotencyKey: 'native-import-0001' }
  const task = await clients.importFile(input)
  assert.ok(task.id)
  const repeated = await clients.importFile(input)
  assert.deepEqual(repeated, task)
  assert.ok(await clients.receipt({ connectionId: connection.connectionId, idempotencyKey: input.idempotencyKey }))
  let complete
  for (let attempt = 0; attempt < 120; attempt++) {
    const view = clients.list()[0].view.projection.state
    complete = view.tasks.find(item => item.id === task.id)
    if (complete?.status === 'succeeded' || complete?.status === 'failed') break
    await delay(100)
  }
  assert.equal(complete?.status, 'succeeded', JSON.stringify(complete))
  assert.equal(clients.list()[0].view.projection.state.resources[0].filename, '技术手册.pdf')
  const original = await clients.readOriginal({ connectionId: connection.connectionId, versionId: task.version_id })
  assert.deepEqual(Buffer.from(original), bytes)
  const exported = await clients.exportBundle({ connectionId: connection.connectionId, versionId: task.version_id })
  assert.deepEqual(exported, { saved: true })
  const archive = await readFile(path.join(temporary, 'export.ddp.zip'))
  await runtime.validateBundle(archive)
  const corrupted = Buffer.from(archive); corrupted[100] ^= 1
  await assert.rejects(runtime.validateBundle(corrupted), /invalid_bundle/)
  clients.selectInput = async () => path.join(temporary, 'export.ddp.zip')
  const copied = await clients.importFile({ ...input, kind: 'bundle', idempotencyKey: 'native-import-0002' })
  assert.ok(copied)
  clients.selectInput = async () => null
  assert.equal(await clients.importFile({ ...input, idempotencyKey: 'cancelled-select' }), null)
})


test('lost native import response requires receipt lookup and never automatically replays bytes',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { clients, selected } = await fixture(t)
  const connected = await clients.connectLocal(selected)
  await current(clients, connected.connectionId)
  clients.selectInput = async () => path.join(repository, 'tests/fixtures/sample.pdf')
  const actualFetch = globalThis.fetch
  let dispatched = 0
  globalThis.fetch = async (url, options) => {
    const response = await actualFetch(url, options)
    if (String(url).endsWith('/api/v1/resources/upload')) {
      dispatched++; await response.body.cancel()
      throw new TypeError('simulated response lost after server accepted bytes')
    }
    return response
  }
  const input = { connectionId: connected.connectionId, kind: 'pdf', idempotencyKey: 'uncertain-file-0001' }
  try {
    await assert.rejects(clients.importFile(input))
    await assert.rejects(clients.importFile(input), /outcome_unknown/)
    assert.equal(dispatched, 1)
    assert.ok(await clients.receipt({ connectionId: input.connectionId, idempotencyKey: input.idempotencyKey }))
    assert.ok(await clients.importFile(input))
    assert.equal(dispatched, 1)
  } finally { globalThis.fetch = actualFetch }
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

test('remote pairing rejects an unproven node before accessing a credential or new projection', async t => {
  const { clients } = await fixture(t, { native: false })
  let credentialsRead = 0
  clients.credentials = { withCredential: async () => { credentialsRead++; return 'private-remote-token' } }
  const actualFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({ authority_node_id: 'node-unproven',
    public_key: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', proof: {} }),
  { headers: { 'Content-Type': 'application/json' } })
  try {
    const paired = await clients.pairRemote({ label: 'Unproven',
      environment: { environmentId: 'node-unproven', authorityNodeId: 'node-unproven', workspaceId: 'org-one', endpoint: 'https://example.invalid' },
      profile: { profileId: 'alice', issuer: 'node-unproven', subject: 'user-alice' } })
    for (let i = 0; i < 50 && clients.list().find(item => item.connectionId === paired.connectionId).view.transport !== 'blocked'; i++) await delay(20)
    const view = clients.list().find(item => item.connectionId === paired.connectionId).view
    assert.equal(view.transport, 'blocked'); assert.equal(view.reason, 'identity_mismatch')
    assert.equal(view.projection, null); assert.equal(credentialsRead, 0)
  } finally { globalThis.fetch = actualFetch }
})


test('window queries accept only a bounded snapshot/cursor pair in the selected connection', () => {
  for (const name of ['resource.page', 'task.page']) {
    const input = { connectionId: 'known', name, payload: { snapshot_id: 'snap.one', cursor: 'page-one' } }
    assert.equal(clientArguments('clientQuery', input).name, name)
    assert.throws(() => clientArguments('clientQuery', { ...input, payload: { ...input.payload, profile_id: 'other' } }))
    assert.throws(() => clientArguments('clientQuery', { ...input, payload: { snapshot_id: 'snap', cursor: 'x'.repeat(4097) } }))
  }
})


test('remote pairing preserves a proven HTTPS deployment prefix and refuses credentials or redirects in its URL', () => {
  const input = { label: 'Center', environment: { environmentId: 'node-known', authorityNodeId: 'node-known',
    workspaceId: 'org-one', endpoint: 'https://example.invalid/team/a&b/' },
    profile: { profileId: 'alice', issuer: 'node-known', subject: 'user-alice' } }
  assert.equal(clientArguments('clientPairRemote', input).environment.endpoint, 'https://example.invalid/team/a&b')
  for (const endpoint of ['http://example.invalid/team', 'https://secret@example.invalid/team',
    'https://example.invalid/team?redirect=other', 'https://example.invalid/team#other']) {
    assert.throws(() => clientArguments('clientPairRemote', { ...input, environment: { ...input.environment, endpoint } }))
  }
})
