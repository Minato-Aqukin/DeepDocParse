import { spawn } from 'node:child_process'
import { mkdtemp, readFile, writeFile, mkdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import os from 'node:os'
import assert from 'node:assert/strict'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(import.meta.url)
const electron = require('electron')
const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-desktop-smoke-'))
const env = { ...process.env, DDP_DESKTOP_SMOKE: '1', DDP_DESKTOP_SMOKE_DIRECTORY: directory }
delete env.ELECTRON_RUN_AS_NODE
if (!env.WAYLAND_DISPLAY && !env.DISPLAY) throw new Error('No display selected; set the actual Wayland/X11 session environment')
const flags = env.WAYLAND_DISPLAY ? ['--ozone-platform=wayland'] : ['--ozone-platform=x11']
const child = spawn(electron, [...flags, root], { env, stdio: ['ignore', 'pipe', 'pipe'] })
// Keep diagnostics private. They can contain paths; no token is printed by the host/runtime.
const diagnostics = []
child.stdout.on('data', data => diagnostics.push(data))
child.stderr.on('data', data => diagnostics.push(data))
const timeout = setTimeout(() => child.kill('SIGTERM'), 45000)
const exit = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve) })
clearTimeout(timeout)
await writeFile(path.join(directory, 'electron.log'), Buffer.concat(diagnostics), { mode: 0o600 })
if (exit !== 0) throw new Error(`Desktop smoke failed; private diagnostics: ${directory}`)
const report = JSON.parse(await readFile(path.join(directory, 'report.json'), 'utf8'))
assert.equal(report.renderer.nodeRequire, 'undefined')
assert.equal(report.renderer.nodeProcess, 'undefined')
assert.equal(report.renderer.host.ok, true)
assert.equal(report.renderer.rejected.ok, false)
assert.ok(report.renderer.rendered > 0)
assert.deepEqual(report.webPreferences, { sandbox: true, contextIsolation: true, nodeIntegration: false })
assert.equal(report.ready.state, 'ready')
assert.equal(report.suspended.state, 'stopped')
assert.equal(report.resumed.state, 'ready')
assert.deepEqual(report.ready.identity, report.resumed.identity)
assert.equal(report.stopped.state, 'stopped')
assert.equal(report.sharedClient.pdfRendered, true)
assert.ok(report.sharedClient.fileResults.originalBytes > 0)
assert.equal(report.sharedClient.fileResults.exported.value.saved, true)
assert.equal(report.renderer.methods.includes('getCredential'), false)
const artifacts = path.join(root, 'artifacts')
await mkdir(artifacts, { recursive: true })
await writeFile(path.join(artifacts, 'smoke-report.json'), JSON.stringify(report, null, 2))
await writeFile(path.join(artifacts, 'desktop.png'), await readFile(path.join(directory, 'desktop.png')))
process.stdout.write(JSON.stringify({ passed: true, report: path.join(artifacts, 'smoke-report.json'),
  screenshot: path.join(artifacts, 'desktop.png'), electron: report.renderer.host.value.electron,
  secretBackend: report.renderer.host.value.secrets, display: report.display }) + '\n')
