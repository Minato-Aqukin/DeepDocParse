import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, realpath, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { createRuntimeBackend } from '../src/runtime-backends.mjs'
import { createNativeBackend, runtimeEnvironment } from '../src/runtime-native.mjs'
import { OwnedRuntimeManager } from '../src/runtime.mjs'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { HostError } from '../src/policy.mjs'

class FakeChild extends EventEmitter {
  constructor(pid = 4242, { closeOn = [] } = {}) {
    super(); this.pid = pid; this.exitCode = null; this.signalCode = null
    this.signals = []; this.closeOn = closeOn
  }
  kill(signal) {
    this.signals.push(signal)
    if (this.closeOn.includes(signal)) {
      this.signalCode = signal
      queueMicrotask(() => this.emit('close', null, signal))
    }
    return true
  }
}

function validatorChild({ output = 'validated', code = 0 } = {}) {
  const child = new EventEmitter()
  child.stdout = new EventEmitter()
  child.stdin = new EventEmitter()
  child.kill = () => true
  child.stdin.end = data => {
    child.received = data
    queueMicrotask(() => {
      if (output) child.stdout.emit('data', Buffer.from(output))
      child.emit('close', code)
    })
  }
  return child
}

async function handshakeServer(t) {
  const server = createServer((_request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ protocol_version: 'ddp-client/1',
      identity: { environment_id: 'env-1', workspace_id: 'ws-1' } }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) })
  return server.address().port
}

async function managerFixture(t, backend, options = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'ddp-runtime-backend-'))
  const workspaces = new WorkspaceHandles()
  await mkdir(path.join(root, 'workspace'))
  const selected = await workspaces.selectedByNativeDialog(path.join(root, 'workspace'))
  const runtime = new OwnedRuntimeManager({ workspaces, directory: path.join(root, 'sessions'), backend, ...options })
  t.after(async () => { await runtime.shutdown().catch(() => {}); await rm(root, { recursive: true, force: true }) })
  return { runtime, id: selected.workspaceId, root }
}

test('manager drives any backend through spawn, ready, handshake, wake and stop', async t => {
  const port = await handshakeServer(t)
  const child = new FakeChild(777, { closeOn: ['SIGTERM'] })
  const calls = { spawn: [], ready: 0, stop: [], validated: [] }
  const backend = {
    name: 'fake', isolation: 'test',
    async spawn(options) {
      calls.spawn.push(options)
      return { child, ready: async () => { calls.ready++; return { url: `http://127.0.0.1:${port}`, token: 'a'.repeat(64) } } }
    },
    async stop(value, options) {
      calls.stop.push({ child: value, options })
      if (value.exitCode === null && value.signalCode === null) { value.signalCode = 'SIGTERM'; value.emit('close') }
    },
    async validateBundle(data) { calls.validated.push(data) },
  }
  const { runtime, id, root } = await managerFixture(t, backend)
  const ready = await runtime.start(id)
  assert.equal(ready.state, 'ready')
  assert.equal(ready.identity.workspace_id, 'ws-1')
  assert.equal(runtime.activeCount(), 1)
  assert.deepEqual(Object.keys(calls.spawn[0]).sort(), ['sessionDir', 'tokenFile', 'workspace'])
  assert.equal(calls.spawn[0].workspace, await realpath(path.join(root, 'workspace')))
  assert.equal(calls.spawn[0].sessionDir, path.dirname(calls.spawn[0].tokenFile))
  assert.equal(calls.ready, 1)
  assert.equal(runtime.connection(id).handshake.protocol_version, 'ddp-client/1')
  assert.equal((await runtime.wake(id)).state, 'ready')
  assert.equal(calls.spawn.length, 1)
  const stopped = await runtime.stop(id)
  assert.equal(stopped.state, 'stopped')
  assert.equal(runtime.activeCount(), 0)
  assert.equal(calls.stop.length, 1)
  assert.equal(calls.stop[0].child, child)
  assert.deepEqual(calls.stop[0].options, { graceMs: 3000 })
  const data = { length: 4 }
  await runtime.validateBundle(data)
  assert.deepEqual(calls.validated, [data])
  await assert.rejects(runtime.validateBundle({ length: 32 * 1024 * 1024 + 1 }), /invalid_bundle/)
  assert.equal(calls.validated.length, 1)
})

const posixRuntimeReason = process.platform === 'linux' ? false
  : 'the native backend derives a POSIX child environment (PATH=/usr/bin:/bin); Windows local mode is the WSL backend covered by runtime-wsl.test.mjs'

test('native backend spawns the unchanged launcher argv and POSIX environment', { skip: posixRuntimeReason }, async () => {
  const captured = []
  const child = new FakeChild(101)
  const backend = createNativeBackend({ python: '/opt/runtime/python3', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: ['/app/site-packages', '/app/ddp'], cwd: '/app', startupMs: 50,
    spawnProcess: (command, args, options) => { captured.push({ command, args, options }); return child } })
  const handle = await backend.spawn({ workspace: '/workspace', sessionDir: '/sessions/one', tokenFile: '/sessions/one/session.json' })
  assert.equal(backend.name, 'native')
  assert.equal(typeof backend.isolation, 'string')
  assert.equal(handle.child, child)
  assert.equal(captured.length, 1)
  assert.equal(captured[0].command, '/opt/runtime/python3')
  assert.deepEqual(captured[0].args, ['-S', '-P', '/app/src/runtime-launcher.py', '--workspace', '/workspace',
    'serve', '--port', '0', '--token-file', '/sessions/one/session.json'])
  assert.equal(captured[0].options.shell, false)
  assert.equal(captured[0].options.detached, false)
  assert.equal(captured[0].options.stdio, 'ignore')
  assert.equal(captured[0].options.cwd, '/app')
  assert.equal(captured[0].options.env.PATH, '/usr/bin:/bin')
  assert.equal(captured[0].options.env.PYTHONPATH, '/app/site-packages:/app/ddp')
  assert.equal(captured[0].options.env.PYTHONSAFEPATH, '1')
  assert.equal(captured[0].options.env.PYTHONNOUSERSITE, '1')
  assert.equal(captured[0].options.env.PYTHONDONTWRITEBYTECODE, '1')
  assert.equal(captured[0].options.env.SERVICE_TOKEN, undefined)
  assert.equal(captured[0].options.env.HTTPS_PROXY, undefined)
})

test('runtimeEnvironment keeps only system variables for a future win32 child', () => {
  const source = { PATH: 'C:\\Windows\\System32', SystemRoot: 'C:\\Windows', SystemDrive: 'C:',
    TEMP: 'C:\\Temp', USERPROFILE: 'C:\\Users\\dev', HOME: '/home/dev', LANG: 'zh_CN.UTF-8', SERVICE_TOKEN: 'secret' }
  const windows = runtimeEnvironment(['C:\\site-packages'], 'win32', source)
  assert.equal(windows.PATH, 'C:\\Windows\\System32')
  assert.equal(windows.SystemRoot, 'C:\\Windows')
  assert.equal(windows.SystemDrive, 'C:')
  assert.equal(windows.TEMP, 'C:\\Temp')
  assert.equal(windows.USERPROFILE, 'C:\\Users\\dev')
  assert.equal(windows.HOME, undefined)
  assert.equal(windows.LANG, undefined)
  assert.equal(windows.SERVICE_TOKEN, undefined)
  assert.equal(windows.PYTHONPATH, 'C:\\site-packages')
  const posix = runtimeEnvironment(['/site-packages'], 'linux', source)
  assert.equal(posix.PATH, '/usr/bin:/bin')
  assert.equal(posix.SystemRoot, undefined)
  assert.equal(posix.HOME, '/home/dev')
  assert.equal(posix.LANG, 'zh_CN.UTF-8')
})

test('native ready resolves from a valid token file and rejects on timeout', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'ddp-native-ready-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const child = new FakeChild(2024)
  const backend = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: [], cwd: root, startupMs: 400, spawnProcess: () => child })
  const tokenFile = path.join(root, 'session.json')
  const handle = await backend.spawn({ workspace: root, sessionDir: root, tokenFile })
  await writeFile(tokenFile, JSON.stringify({ url: 'http://127.0.0.1:15001', pid: 2024, token: 'a'.repeat(64) }), { mode: 0o600 })
  assert.deepEqual(await handle.ready(), { url: 'http://127.0.0.1:15001', token: 'a'.repeat(64) })

  const timeoutFile = path.join(root, 'missing.json')
  const timeout = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: [], cwd: root, startupMs: 120, spawnProcess: () => new FakeChild(2025) })
  const pending = await timeout.spawn({ workspace: root, sessionDir: root, tokenFile: timeoutFile })
  const started = Date.now()
  await assert.rejects(pending.ready(), /runtime_start_failed/)
  assert.ok(Date.now() - started < 5000)
})

test('native stop is SIGTERM, one grace period, then SIGKILL', async () => {
  const backend = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: [], cwd: '/tmp', shutdownMs: 20, spawnProcess: () => assert.fail('stop must not spawn') })
  const stubborn = new FakeChild(1, { closeOn: ['SIGKILL'] })
  await backend.stop(stubborn, { graceMs: 20 })
  assert.deepEqual(stubborn.signals, ['SIGTERM', 'SIGKILL'])
  const graceful = new FakeChild(2, { closeOn: ['SIGTERM'] })
  await backend.stop(graceful, { graceMs: 200 })
  assert.deepEqual(graceful.signals, ['SIGTERM'])
  const exited = new FakeChild(3); exited.signalCode = 'SIGTERM'
  await backend.stop(exited, { graceMs: 20 })
  assert.deepEqual(exited.signals, [])
})

test('stop while starting cancels a pending ready wait and never publishes ready', async t => {
  const child = new FakeChild(9, { closeOn: ['SIGTERM'] })
  let readyStarted = false
  const backend = {
    name: 'fake', isolation: 'test',
    async spawn() {
      return { child, ready: () => new Promise((_resolve, reject) => {
        readyStarted = true; child.once('close', () => reject(new HostError('runtime_exited')))
      }) }
    },
    async stop(value) { value.kill('SIGTERM') },
    async validateBundle() {},
  }
  const { runtime, id } = await managerFixture(t, backend, { startupMs: 60000 })
  const starting = runtime.start(id)
  while (!readyStarted) await delay(5)
  const started = Date.now()
  const stopping = runtime.stop(id)
  await assert.rejects(starting, /runtime_stopped/)
  await stopping
  assert.ok(Date.now() - started < 1000)
  assert.equal(runtime.status(id).state, 'stopped')
})

test('WSL selection fails closed and leaves non-runtime manager calls unaffected', async t => {
  await assert.rejects(createRuntimeBackend({ kind: 'wsl' }),
    error => error instanceof HostError && error.code === 'wsl_backend_unavailable')
  await assert.rejects(createRuntimeBackend({ kind: 'pypy' }), /unknown_runtime_backend/)
  const native = await createRuntimeBackend({ kind: 'native', python: '/opt/python',
    launcher: '/app/src/runtime-launcher.py', pythonPaths: [], cwd: '/tmp', spawnProcess: () => new FakeChild() })
  assert.equal(native.name, 'native')
  const unavailable = createRuntimeBackend({ kind: 'wsl' })
  unavailable.catch(() => {})
  const { runtime, id } = await managerFixture(t, unavailable)
  assert.equal(runtime.activeCount(), 0)
  assert.equal(runtime.status(id).state, 'stopped')
  await assert.rejects(runtime.start(id), /wsl_backend_unavailable/)
  assert.equal(runtime.status(id).state, 'failed')
  assert.equal(runtime.status(id).reason, 'wsl_backend_unavailable')
  assert.equal(runtime.activeCount(), 0)
  assert.throws(() => runtime.connection(id), /runtime_unavailable/)
})

test('native validateBundle speaks the unchanged runtime-files.py stdin/stdout protocol', async () => {
  const children = []
  const backend = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: ['/p'], cwd: '/work',
    spawnProcess: (command, args, options) => { const child = validatorChild(); children.push({ command, args, options, child }); return child } })
  const data = Buffer.from('ddp-bundle-bytes')
  await backend.validateBundle(data)
  assert.deepEqual(children[0].args, ['-S', '-P', path.join('/app/src', 'runtime-files.py')])
  assert.equal(children[0].command, '/opt/python')
  assert.deepEqual(children[0].options.stdio, ['pipe', 'pipe', 'ignore'])
  assert.equal(children[0].options.env.PYTHONSAFEPATH, '1')
  assert.deepEqual(children[0].child.received, data)
  const rejected = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: [], cwd: '/work', spawnProcess: () => validatorChild({ code: 1 }) })
  await assert.rejects(rejected.validateBundle(Buffer.from('x')), /invalid_bundle/)
  const garbage = createNativeBackend({ python: '/opt/python', launcher: '/app/src/runtime-launcher.py',
    pythonPaths: [], cwd: '/work', spawnProcess: () => validatorChild({ output: 'nope' }) })
  await assert.rejects(garbage.validateBundle(Buffer.from('x')), /invalid_bundle/)
})

test('validateBundle keeps the size cap and closing gate in the manager', async t => {
  const calls = []
  const backend = { name: 'fake', isolation: 'test', async spawn() { assert.fail('must not spawn') },
    async stop() {}, async validateBundle(data) { calls.push(data) } }
  const { runtime } = await managerFixture(t, backend)
  await assert.rejects(runtime.validateBundle({ length: 32 * 1024 * 1024 + 1 }), /invalid_bundle/)
  await runtime.shutdown()
  await assert.rejects(runtime.validateBundle({ length: 1 }), /invalid_bundle/)
  assert.equal(calls.length, 0)
})
