// Windows packaged-app smoke: launch an already-packaged executable with the
// host's smoke instrumentation and assert the report it writes.
//
// The instrumentation in src/main.mjs is environment-driven plus one argv flag:
//
//     const smoke = process.env.DDP_DESKTOP_SMOKE === '1'
//       && (!app.isPackaged || process.argv.includes('--smoke'))
//     if (smoke && process.env.DDP_DESKTOP_SMOKE_DIRECTORY) app.setPath('userData', ...)
//
// A packaged exe (win-unpacked/deepdocparse.exe, DeepDocParse-*-setup.exe,
// DeepDocParse-*-portable.exe) therefore enters the smoke path when launched
// with `--smoke` and the two environment variables below; this script always
// passes the flag. The CI step still runs continue-on-error for the first
// iteration because a runner session may not be able to open an Electron
// window, but the report assertions are real.
//
// Usage:
//   node scripts/smoke-windows.mjs [TARGET] [--timeout[=]ms] [--report[=]path] [--host]
//
// TARGET  packaged directory (win-unpacked / the assembled stage) or an .exe
//         path (portable/setup, or win-unpacked/deepdocparse.exe).
//         Default: dist/desktop/windows/win-unpacked/deepdocparse.exe, else
//         the single dist/desktop/windows/DeepDocParse-*-portable.exe.
// --host  run the checkout app with the npm-installed Electron instead of a
//         packaged exe (the Windows analogue of scripts/smoke.mjs; needs
//         apps/desktop/node_modules/electron with its binary downloaded).
//
// A temp directory is used as --user-data-dir / DDP_DESKTOP_SMOKE_DIRECTORY.
// The main.mjs report (report.json) is copied next to --report as
// smoke-report.json; diagnostics go to smoke-windows-electron.log. Exit code 0
// means every assertion passed. Dependency-free: node stdlib plus the host's
// own src/policy.mjs (the single list of WSL host-capability codes).

import { spawn, spawnSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, writeFile, copyFile } from 'node:fs/promises'
import { existsSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import path from 'node:path'
import os from 'node:os'
import { WSL_HOST_UNAVAILABLE_CODES } from '../src/policy.mjs'
import assert from 'node:assert/strict'

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repository = path.resolve(desktop, '../..')

export function parseArguments(argv) {
  const result = { target: null, host: false, timeoutMs: 120000,
    report: path.join(desktop, 'artifacts', 'smoke-report.json') }
  const valued = new Set(['--timeout', '--report'])
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--host') result.host = true
    else if (argument === '--help' || argument === '-h') {
      process.stdout.write('Usage: node scripts/smoke-windows.mjs [TARGET] [--host] [--timeout[=]ms] [--report[=]path]\n')
      process.exit(0)
    } else if (argument.startsWith('--timeout=')) {
      result.timeoutMs = Number(argument.slice('--timeout='.length))
    } else if (argument.startsWith('--report=')) {
      result.report = path.resolve(argument.slice('--report='.length))
    } else if (valued.has(argument)) {
      const value = argv[index + 1]
      if (value === undefined || value.startsWith('--')) {
        throw new Error(`${argument} needs a value`)
      }
      index += 1
      if (argument === '--timeout') result.timeoutMs = Number(value)
      else result.report = path.resolve(value)
    } else if (argument.startsWith('--')) {
      throw new Error(`unknown option: ${argument}`)
    } else if (result.target === null) {
      result.target = path.resolve(argument)
    } else {
      throw new Error('at most one TARGET is accepted')
    }
  }
  if (!Number.isFinite(result.timeoutMs) || result.timeoutMs <= 0) {
    throw new Error('--timeout must be a positive number of milliseconds')
  }
  return result
}

function resolvePackagedTarget(target) {
  if (target !== null) {
    if (!existsSync(target)) throw new Error(`target not found: ${target}`)
    if (target.toLowerCase().endsWith('.exe')) return target
    const binary = path.join(target, 'deepdocparse.exe')
    if (existsSync(binary)) return binary
    throw new Error(`no deepdocparse.exe in ${target}`)
  }
  const windows = path.join(repository, 'dist/desktop/windows')
  const unpacked = path.join(windows, 'win-unpacked', 'deepdocparse.exe')
  if (existsSync(unpacked)) return unpacked
  if (existsSync(windows)) {
    const portables = readdirSync(windows).filter(name => /^DeepDocParse-.*-portable\.exe$/.test(name))
    if (portables.length === 1) return path.join(windows, portables[0])
    if (portables.length > 1) throw new Error(`multiple portable exes under ${windows}; pass TARGET explicitly`)
  }
  throw new Error(`no packaged Windows app found under ${windows}; pass TARGET explicitly`)
}

function resolveHostElectron() {
  const require = createRequire(import.meta.url)
  try {
    return require('electron')
  } catch (error) {
    throw new Error(`--host needs apps/desktop/node_modules/electron with its binary ` +
      `(ELECTRON_SKIP_BINARY_DOWNLOAD leaves none): ${error.message}`)
  }
}

function killTree(child) {
  if (!child.pid) return
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/pid', String(child.pid), '/t', '/f'], { stdio: 'ignore' })
  } else {
    child.kill('SIGTERM')
  }
}

export function assertReport(report) {
  assert.equal(report.renderer.nodeRequire, 'undefined')
  assert.equal(report.renderer.nodeProcess, 'undefined')
  assert.equal(report.renderer.host.ok, true)
  assert.equal(report.renderer.rejected.ok, false)
  assert.ok(report.renderer.rendered > 0, 'renderer rendered no UI')
  assert.deepEqual(report.webPreferences,
    { sandbox: true, contextIsolation: true, nodeIntegration: false })
  assert.equal(report.renderer.methods.includes('getCredential'), false)
  if (report.localRuntime?.state === 'ready') {
    assert.equal(report.ready.state, 'ready')
    assert.equal(report.suspended.state, 'stopped')
    assert.equal(report.resumed.state, 'ready')
    assert.deepEqual(report.ready.identity, report.resumed.identity)
    assert.equal(report.stopped.state, 'stopped')
    assert.equal(report.sharedClient.pdfRendered, true)
    assert.ok(report.sharedClient.fileResults.originalBytes > 0)
    assert.equal(report.sharedClient.fileResults.exported.value.saved, true)
    return
  }
  // Tier A: a Windows runner without a WSL2 distribution reports the honest
  // capability reason instead of pretending local mode works. Only host
  // capability codes qualify: a `wsl_runtime_*` / `wsl_backend_unavailable`
  // reason is a broken package and must fail, not pass as "no WSL".
  assert.equal(report.localRuntime?.state, 'unavailable')
  assert.ok(WSL_HOST_UNAVAILABLE_CODES.has(report.localRuntime.reason),
    `unexpected local runtime reason: ${report.localRuntime.reason}`)
  assert.equal(report.sharedClient, null)
  assert.equal(report.ready ?? null, null, 'an unavailable run cannot carry a ready runtime')
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  const executable = options.host ? resolveHostElectron() : resolvePackagedTarget(options.target)
  const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-desktop-smoke-'))
  const env = { ...process.env, DDP_DESKTOP_SMOKE: '1', DDP_DESKTOP_SMOKE_DIRECTORY: directory }
  delete env.ELECTRON_RUN_AS_NODE
  // A packaged app ignores DDP_DESKTOP_SMOKE (see header), so keep the profile
  // out of %APPDATA% with Chromium's own switch as well. Unknown `--smoke` is
  // ignored by Chromium and is the flag main.mjs can start honouring.
  const arguments_ = options.host
    ? [desktop]
    : [`--user-data-dir=${directory}`, '--smoke']
  const child = spawn(executable, arguments_, { env, stdio: ['ignore', 'pipe', 'pipe'] })
  const diagnostics = []
  child.stdout.on('data', data => diagnostics.push(data))
  child.stderr.on('data', data => diagnostics.push(data))
  let closed = false
  let exitCode = null
  let spawnError = null
  child.once('error', error => { spawnError = error })
  const completion = new Promise(resolve => child.once('close', code => {
    closed = true
    exitCode = code
    resolve(code)
  }))
  const reportFile = path.join(directory, 'report.json')
  const failureFile = path.join(directory, 'failure.json')
  const readReport = async () => {
    try {
      return JSON.parse(await readFile(reportFile, 'utf8'))
    } catch {
      return null // absent, or observed mid-write: the loop retries
    }
  }
  const deadline = Date.now() + options.timeoutMs
  let report = null
  while (!closed && !spawnError && report === null && Date.now() < deadline) {
    report = await readReport()
    if (report === null) await delay(250)
  }
  if (!closed && !spawnError) {
    if (report !== null) await Promise.race([completion, delay(5000)]) // let it quit on its own
    if (!closed) killTree(child)
    await Promise.race([completion, delay(15000)])
  }
  const log = Buffer.concat(diagnostics)
  const artifacts = path.dirname(options.report)
  const logPath = path.join(artifacts, 'smoke-windows-electron.log')
  await mkdir(artifacts, { recursive: true })
  await writeFile(logPath, log)
  if (spawnError) throw new Error(`could not launch ${executable}: ${spawnError.message}; log: ${logPath}`)
  report = report ?? await readReport()
  if (report !== null) {
    await copyFile(reportFile, options.report)
    if (existsSync(path.join(directory, 'desktop.png'))) {
      await copyFile(path.join(directory, 'desktop.png'), path.join(artifacts, 'desktop.png'))
    }
    assertReport(report)
    process.stdout.write(JSON.stringify({ passed: true, report: options.report, log: logPath,
      executable, exitCode, display: report.display ?? null,
      localRuntime: report.localRuntime ?? null,
      electron: report.renderer.host.value.electron, secrets: report.renderer.host.value.secrets }) + '\n')
    return
  }
  if (existsSync(failureFile)) {
    const failure = JSON.parse(await readFile(failureFile, 'utf8'))
    await copyFile(failureFile, path.join(artifacts, 'smoke-windows-failure.json'))
    throw new Error(`desktop smoke reported a host failure (exit ${exitCode}): ` +
      `${JSON.stringify(failure)}; log: ${logPath}`)
  }
  throw new Error(`desktop smoke wrote no report.json (exit ${exitCode ?? `timeout ${options.timeoutMs}ms`}); ` +
    `the packaged app must receive --smoke plus DDP_DESKTOP_SMOKE=1 and ` +
    `DDP_DESKTOP_SMOKE_DIRECTORY (see the script header); log: ${logPath}`)
}

const invokedDirectly = process.argv[1] !== undefined
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
if (invokedDirectly) await main()
