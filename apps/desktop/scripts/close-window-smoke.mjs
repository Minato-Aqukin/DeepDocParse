import { spawn, execFileSync } from 'node:child_process'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import path from 'node:path'
import os from 'node:os'
import assert from 'node:assert/strict'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const electron = createRequire(import.meta.url)('electron')
const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE
delete env.DDP_DESKTOP_SMOKE
if (!env.WAYLAND_DISPLAY || !env.NIRI_SOCKET) {
  throw new Error('Select an actual niri/Wayland session with WAYLAND_DISPLAY and NIRI_SOCKET')
}
const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-close-window-smoke-'))
const profile = path.join(directory, 'profile')
const child = spawn(electron, [root, `--user-data-dir=${profile}`, '--ozone-platform=wayland'],
  { env, stdio: ['ignore', 'pipe', 'pipe'] })
const diagnostics = []
let exit = null, launchError = null
child.stdout.on('data', data => diagnostics.push(data))
child.stderr.on('data', data => diagnostics.push(data))
child.once('error', error => { launchError = error })
child.once('exit', (code, signal) => { exit = { code, signal } })
const niri = (...args) => execFileSync('niri', ['msg', ...args], { env, encoding: 'utf8', timeout: 2000 })
const windows = () => JSON.parse(niri('--json', 'windows')).filter(window => window.pid === child.pid)
let window
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (launchError) throw launchError
    assert.equal(exit, null, 'Electron exited before opening its only window')
    window = windows()[0]
    if (window) break
    await delay(100)
  }
  assert.ok(window, 'Electron must open its only window')
  niri('action', 'close-window', '--id', String(window.id))
  for (let attempt = 0; attempt < 50 && !exit; attempt++) await delay(100)
  assert.deepEqual(exit, { code: 0, signal: null },
    'Closing the only window must exit Electron, not leave a windowless main process')
  assert.equal(windows().length, 0)
  process.stdout.write(JSON.stringify({ passed: true, pid: child.pid, profile, windowId: window.id, exit }) + '\n')
} catch (error) {
  process.stderr.write(`Close-window smoke failed; private diagnostics: ${directory}\n`)
  throw error
} finally {
  if (!exit && !launchError) child.kill('SIGKILL')
  await writeFile(path.join(directory, 'electron.log'), Buffer.concat(diagnostics), { mode: 0o600 })
  await writeFile(path.join(directory, 'close-report.json'), JSON.stringify({ pid: child.pid, profile,
    windowId: window?.id ?? null, exit }, null, 2), { mode: 0o600 })
}
