import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, mkdtemp, writeFile, readFile, readdir, mkdir, symlink, rename, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { CHANNELS, authorizeSender, validate, uiLocation, allowRequest, safeFailure } from '../src/policy.mjs'
import { CredentialBroker } from '../src/credentials.mjs'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { handshake, readConnection, runtimeEnvironment } from '../src/runtime.mjs'
import { staticUI } from '../src/static-ui.mjs'

async function temporary(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-host-test-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  return directory
}
function storage(backend = 'gnome_libsecret') {
  const key = randomBytes(32)
  return {
    getSelectedStorageBackend: () => backend, isEncryptionAvailable: () => true,
    encryptString(text) {
      const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv)
      const data = Buffer.concat([cipher.update(text, 'utf8'), cipher.final()])
      return Buffer.concat([iv, cipher.getAuthTag(), data])
    },
    decryptString(data) {
      const cipher = createDecipheriv('aes-256-gcm', key, data.subarray(0, 12))
      cipher.setAuthTag(data.subarray(12, 28))
      return Buffer.concat([cipher.update(data.subarray(28)), cipher.final()]).toString('utf8')
    },
  }
}
const pair = { environmentId: 'local:one', profileId: 'person:alice' }

test('only the trusted main frame can call a fixed, schema-validated IPC operation', () => {
  const expected = uiLocation(), frame = { url: 'ddp://app/index.html#/workspace' }
  const contents = { mainFrame: frame, isDestroyed: () => false }
  authorizeSender({ sender: contents, senderFrame: frame }, contents, expected)
  for (const event of [
    { sender: {}, senderFrame: frame }, { sender: contents, senderFrame: { url: frame.url } },
    { sender: contents, senderFrame: null },
  ]) assert.throws(() => authorizeSender(event, contents, expected), /untrusted_sender/)
  for (const url of ['https://evil.test', 'ddp://evil/index.html', 'ddp://app/foreign.html']) {
    frame.url = url
    assert.throws(() => authorizeSender({ sender: contents, senderFrame: frame }, contents, expected), /untrusted_sender/)
  }
  assert.throws(() => validate('startLocal', { workspaceId: 'a'.repeat(36), path: '/etc' }), /invalid_arguments/)
  assert.throws(() => validate('shell', { command: 'id' }), /unknown_operation/)
  assert.throws(() => validate('setCredential', { ...pair, secret: 'key', persist: 'true' }), /invalid_credential/)
  assert.deepEqual(safeFailure(new Error('token=secret')), { ok: false, error: { code: 'host_operation_failed' } })
  assert.equal(Object.hasOwn(CHANNELS, 'getCredential'), false)
})

test('development URL and resource requests stay within the selected UI origin', () => {
  for (const url of ['https://example.com', 'http://localhost:5173', 'http://127.0.0.1:5173/evil',
    'http://user:secret@127.0.0.1:5173', 'http://127.0.0.1:5173/?x=1']) assert.throws(() => uiLocation(url))
  const expected = uiLocation('http://127.0.0.1:5173')
  assert.equal(allowRequest('ws://127.0.0.1:5173/vite', expected), true)
  assert.equal(allowRequest('http://127.0.0.1:8080/api', expected), false)
  assert.equal(allowRequest('file:///etc/passwd', expected), false)
  assert.equal(allowRequest('https://fonts.googleapis.com/css', uiLocation()), false)
})

test('basic_text and unavailable backends are session-only and never encrypt or persist a secret', async t => {
  for (const backend of ['basic_text', 'unknown', 'unavailable']) {
    const directory = path.join(await temporary(t), 'credentials')
    const safeStorage = storage(backend)
    safeStorage.encryptString = () => assert.fail('must not use weak encryption')
    // Linux secret-service semantics must not depend on the host platform.
    const broker = new CredentialBroker({ directory, safeStorage, platform: 'linux' })
    const status = await broker.set({ ...pair, secret: 'private-key', persist: true })
    assert.equal(status.mode, 'session'); assert.equal(status.persistentAvailable, false)
    assert.deepEqual(await readdir(directory), [])
    assert.equal(await broker.withCredential(pair, secret => secret === 'private-key'), true)
    broker.clearSession()
    assert.equal((await broker.status(pair)).present, false)
  }
})

test('credential policy platform never changes the filesystem security gate', async t => {
  const directory = path.join(await temporary(t), 'credentials')
  await mkdir(directory, { recursive: true })
  if (process.platform !== 'win32') await chmod(directory, 0o755)
  // Policy says Linux (session-only secret service), filesystem says win32:
  // the win32 gate is advisory and must accept a 0o755 directory even though a
  // POSIX gate would reject it. Re-coupling the two would fail this test.
  const broker = new CredentialBroker({ directory, safeStorage: storage('basic_text'),
    platform: 'linux', directoryPlatform: 'win32' })
  const status = await broker.set({ ...pair, secret: 'private-key', persist: true })
  assert.equal(status.mode, 'session')
  assert.equal(status.persistentAvailable, false)
})

test('ciphertext is scoped to environment and profile; session replacement removes stale persistence', async t => {
  const directory = path.join(await temporary(t), 'credentials'), safeStorage = storage()
  let broker = new CredentialBroker({ directory, safeStorage })
  await broker.set({ ...pair, secret: 'unique-private-key', persist: true })
  const [file] = await readdir(directory)
  assert.equal((await readFile(path.join(directory, file), 'utf8')).includes('unique-private-key'), false)
  broker = new CredentialBroker({ directory, safeStorage })
  assert.equal(await broker.withCredential(pair, value => value), 'unique-private-key')
  assert.equal((await broker.status({ ...pair, environmentId: 'local:two' })).present, false)
  assert.equal((await broker.status({ ...pair, profileId: 'person:bob' })).present, false)
  await broker.set({ ...pair, secret: 'session-new-key', persist: false })
  broker.clearSession()
  assert.deepEqual(await readdir(directory), [])
  assert.equal((await broker.status(pair)).present, false)
})

test('copied encrypted files cannot change the credential binding', async t => {
  const directory = path.join(await temporary(t), 'credentials'), safeStorage = storage()
  const broker = new CredentialBroker({ directory, safeStorage })
  await broker.set({ ...pair, secret: 'alice-secret', persist: true })
  const [first] = await readdir(directory)
  const other = { ...pair, profileId: 'person:bob' }
  await broker.set({ ...other, secret: 'bob-secret', persist: true })
  const second = (await readdir(directory)).find(file => file !== first)
  await writeFile(path.join(directory, second), await readFile(path.join(directory, first)), { mode: 0o600 })
  broker.clearSession()
  await assert.rejects(broker.withCredential(other, () => assert.fail()), /credential_unavailable/)
})

test('workspace handles reject forged IDs and replacement by a symlink', async t => {
  const root = await temporary(t), directory = path.join(root, 'workspace')
  await mkdir(directory)
  const handles = new WorkspaceHandles(), selected = await handles.selectedByNativeDialog(directory)
  assert.deepEqual(Object.keys(selected).sort(), ['name', 'workspaceId'])
  await assert.rejects(handles.directory('forged'), /unknown_workspace/)
  await rename(directory, directory + '-old'); await symlink(directory + '-old', directory)
  await assert.rejects(handles.directory(selected.workspaceId), /workspace_changed/)
  await assert.rejects(handles.selectedByNativeDialog(directory), /invalid_workspace/)
})

test('runtime token file rejects stale child PID, insecure modes, remote URLs and symlinks', async t => {
  const root = await temporary(t), file = path.join(root, 'token.json')
  const valid = { url: 'http://127.0.0.1:15001', pid: 1234, token: 'a'.repeat(64) }
  await writeFile(file, JSON.stringify(valid), { mode: 0o600 })
  assert.equal((await readConnection(file, 1234)).url, valid.url)
  await assert.rejects(readConnection(file, 4321), /unsafe_runtime_token/)
  await writeFile(file, JSON.stringify({ ...valid, url: 'http://example.com:15001' }))
  await assert.rejects(readConnection(file, 1234), /unsafe_runtime_token/)
  await symlink(file, path.join(root, 'linked'))
  await assert.rejects(readConnection(path.join(root, 'linked'), 1234))
  const env = runtimeEnvironment(['/trusted'])
  assert.equal(env.PYTHONNOUSERSITE, '1')
  assert.equal(env.SERVICE_TOKEN, undefined); assert.equal(env.HTTPS_PROXY, undefined)
})

test('packaged static protocol does not expose files outside bundled UI', async t => {
  const root = await temporary(t), ui = path.join(root, 'ui')
  await mkdir(ui); await writeFile(path.join(ui, 'index.html'), '<main>workbench</main>')
  await writeFile(path.join(root, 'private'), 'secret'); await symlink(path.join(root, 'private'), path.join(ui, 'linked'))
  const serve = staticUI(ui, uiLocation())
  const response = await serve(new Request('ddp://app/index.html'))
  assert.equal(response.status, 200)
  assert.match(response.headers.get('content-security-policy'), /frame-src 'none'/)
  for (const url of ['ddp://app/linked', 'ddp://app/%2e%2e%2fprivate', 'ddp://foreign/index.html']) {
    const denied = await serve(new Request(url))
    assert.notEqual(denied.status, 200); assert.equal(await denied.text(), '')
  }
})

test('owned runtime handshake rejects an unknown protocol version or incomplete identity', async t => {
  // The gap the boundaries suite closes here: runtime.test.mjs only exercises
  // the happy path. An unknown version or a missing identity must be rejected
  // as runtime_incompatible before the host trusts the session.
  let status = 200, body = {}
  const server = createServer((_req, res) => {
    res.statusCode = status; res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(body))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) })
  const connection = { url: `http://127.0.0.1:${server.address().port}`, token: 't'.repeat(32) }
  const valid = { protocol_version: 'ddp-client/1', identity: { environment_id: 'env-1', workspace_id: 'ws-1' } }

  for (const bad of [
    { ...valid, protocol_version: 'ddp-client/99' },
    { ...valid, protocol_version: 'ddp-client/2' },
    { ...valid, protocol_version: undefined },
    { ...valid, identity: { environment_id: 'env-1' } },
    { ...valid, identity: {} },
    { ...valid, identity: null },
  ]) {
    body = bad
    await assert.rejects(handshake(connection), /runtime_incompatible/)
  }
  status = 401; body = valid
  await assert.rejects(handshake(connection), /runtime_unavailable/)
  status = 200; body = valid
  assert.equal((await handshake(connection)).protocol_version, 'ddp-client/1')
})
