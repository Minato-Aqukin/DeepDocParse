import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readdir, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { OwnedRuntimeManager } from '../src/runtime.mjs'
import { nativeRuntimeSkipReason } from './helpers/platform.mjs'

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
async function manager(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'ddp-host-runtime-'))
  const workspaces = new WorkspaceHandles()
  await mkdir(path.join(root, 'workspace'))
  const selected = await workspaces.selectedByNativeDialog(path.join(root, 'workspace'))
  const [version] = (await readdir(path.join(repository, '.venv/lib'))).filter(name => /^python\d+\.\d+$/.test(name))
  const children = []
  const runtime = new OwnedRuntimeManager({ workspaces, directory: path.join(root, 'sessions'),
    python: path.join(repository, '.venv/bin/python'), launcher: path.join(repository, 'apps/desktop/src/runtime-launcher.py'),
    cwd: repository, pythonPaths: ['ddp_local', 'ddp_core', 'ddp_contracts'].map(name => path.join(repository, 'python', name))
      .concat(path.join(repository, '.venv/lib', version, 'site-packages')),
    spawnProcess: (...args) => { const child = spawn(...args); children.push(child); return child },
  })
  t.after(async () => { await runtime.shutdown(); await rm(root, { recursive: true, force: true }) })
  return { runtime, id: selected.workspaceId, children, root }
}

test('real owned runtime starts once, retains workspace identity across suspend, and stops only its own child',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { runtime, id, children, root } = await manager(t)
  const external = spawn('/usr/bin/python3', ['-c', 'import time; time.sleep(60)'], { stdio: 'ignore' })
  t.after(async () => { if (external.exitCode === null) { const closed = once(external, 'close'); external.kill(); await closed } })
  const [ready, duplicate] = await Promise.all([runtime.start(id), runtime.start(id)])
  assert.equal(ready.state, 'ready'); assert.equal(duplicate.state, 'ready'); assert.equal(children.length, 1)
  const connection = runtime.connection(id)
  assert.equal(connection.handshake.protocol_version, 'ddp-client/1')
  assert.equal(JSON.stringify(ready).includes(connection.token), false)
  await runtime.suspend()
  assert.equal(runtime.status(id).state, 'stopped'); assert.ok(children[0].exitCode !== null || children[0].signalCode !== null)
  assert.equal(external.exitCode, null)
  await runtime.resume()
  assert.equal(runtime.status(id).state, 'ready'); assert.equal(children.length, 2)
  assert.deepEqual(runtime.status(id).identity, ready.identity)
  assert.equal(runtime.status(id).generation, 2)
  await runtime.shutdown()
  assert.equal(runtime.status(id).reason, 'host_quit'); assert.equal(external.exitCode, null)
  assert.deepEqual(await readdir(path.join(root, 'sessions')), [])
  await assert.rejects(runtime.start(id), /host_closing/)
})

test('stop while starting cannot leave an orphan runtime or publish stale ready state',
  { skip: nativeRuntimeSkipReason() }, async t => {
  const { runtime, id, children } = await manager(t)
  const starting = runtime.start(id)
  const stopping = runtime.stop(id)
  await assert.rejects(starting, /runtime_stopped/)
  await stopping
  assert.equal(runtime.status(id).state, 'stopped')
  assert.equal(children.length, 0)
  assert.throws(() => runtime.connection(id), /runtime_unavailable/)
})
