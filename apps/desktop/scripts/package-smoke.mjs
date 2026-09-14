// Run the packaged shared client and runtime from /tmp, with no checkout on Python's path.
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { pathToFileURL } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'

if (process.argv.length !== 4) throw new Error('Usage: package-smoke.mjs DIRECTORY PDF')
const [directory, pdf] = process.argv.slice(2).map(value => path.resolve(value))
const load = name => import(pathToFileURL(path.join(directory, 'resources/app/src', name + '.mjs')))
const { OwnedRuntimeManager } = await load('runtime')
const { WorkspaceHandles } = await load('workspaces')
const { ClientHost } = await load('client-host')
const temporary = await mkdtemp(path.join(os.tmpdir(), 'ddp-packaged-smoke-'))
const workspaces = new WorkspaceHandles()
await mkdir(path.join(temporary, 'workspace'))
const { workspaceId } = await workspaces.selectedByNativeDialog(path.join(temporary, 'workspace'))
const runtime = new OwnedRuntimeManager({ workspaces, directory: path.join(temporary, 'sessions'),
  python: '/usr/bin/python3', launcher: path.join(directory, 'resources/app/src/runtime-launcher.py'),
  pythonPaths: [path.join(directory, 'resources/runtime/site-packages')], cwd: temporary,
})
const clients = await new ClientHost({ runtime, workspaces, directory: path.join(temporary, 'client'),
  credentials: { withCredential: () => assert.fail('local must not request remote secrets') },
  selectInput: async () => pdf, selectOutput: async () => path.join(temporary, 'export.ddp.zip'),
}).initialize()
try {
  const connection = await clients.connectLocal({ workspaceId })
  const view = () => clients.list().find(item => item.connectionId === connection.connectionId).view
  for (let i = 0; i < 100 && (view().transport !== 'ready' || view().snapshot !== 'current'); i++) await delay(100)
  assert.equal(view().snapshot, 'current')
  const task = await clients.importFile({ connectionId: connection.connectionId, kind: 'pdf', idempotencyKey: 'packaged-smoke-0001' })
  let status
  for (let attempt = 0; attempt < 120; attempt++) {
    status = view().projection.state.tasks.find(item => item.id === task.id)
    if (['succeeded', 'failed', 'cancelled'].includes(status?.status)) break
    await delay(250)
  }
  assert.equal(status?.status, 'succeeded', JSON.stringify(status))
  const search = await clients.query({ connectionId: connection.connectionId, name: 'corpus.search', payload: { query: 'contract' } })
  assert.ok(search.hits.length > 0)
  const evidence = await clients.query({ connectionId: connection.connectionId, name: 'evidence.get', payload: { evidence_id: search.hits[0].evidence_id } })
  assert.ok(evidence.evidence.locator.bbox)
  assert.deepEqual(Buffer.from(await clients.readOriginal({ connectionId: connection.connectionId, versionId: task.version_id })), await readFile(pdf))
  assert.ok(await clients.receipt({ connectionId: connection.connectionId, idempotencyKey: 'packaged-smoke-0001' }))
  assert.equal((await clients.exportBundle({ connectionId: connection.connectionId, versionId: task.version_id })).saved, true)
  const archive = await readFile(path.join(temporary, 'export.ddp.zip'))
  await runtime.validateBundle(archive)
  await clients.saveDraft({ connectionId: connection.connectionId, key: 'query', expectedRevision: 0, value: 'packaged' })
  await clients.disconnect({ connectionId: connection.connectionId })
  assert.equal(runtime.status(workspaceId).state, 'ready')
  await clients.close(); await runtime.shutdown()
  console.log(JSON.stringify({ passed: true, packagedPython: '/usr/bin/python3', packagedClientRuntime: true,
    checkoutOnPythonPath: false, parse: status.status, hits: search.hits.length,
    evidenceBBox: evidence.evidence.locator.bbox, bundleBytes: archive.length,
    stopped: runtime.status(workspaceId).state, sqliteDraftAndReceipt: true }))
} finally { await clients.close(); await runtime.shutdown(); await rm(temporary, { recursive: true, force: true }) }
