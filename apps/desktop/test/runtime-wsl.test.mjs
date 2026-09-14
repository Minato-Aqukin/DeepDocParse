import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { createReadStream } from 'node:fs'
import { chmod, cp, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { createRuntimeBackend } from '../src/runtime-backends.mjs'
import { handshake } from '../src/runtime-native.mjs'
import { createWslBackend, decodeWslText, parseWslDistros, resolveWslExecutable, selectWslDistro } from '../src/runtime-wsl.mjs'
import { WorkspaceHandles } from '../src/workspaces.mjs'

const source = path.dirname(fileURLToPath(import.meta.url))
const repository = path.resolve(source, '../../..')
const shim = path.join(source, 'helpers/wsl-shim.sh')
const bundleBuilder = path.join(source, 'helpers/make_bundle.py')
const realDirectory = path.join(repository, 'dist/wsl')
const linuxOnly = { skip: process.platform === 'linux' ? false
  : 'the WSL2 shim and bundled Linux runtime need a POSIX host; Windows coverage is W5 smoke' }

const WSL_ROOT = '~/.deepdocparse/runtime'
const WSL_WORKSPACE = '~/.deepdocparse/workspaces/default'

const LIST = '  NAME            STATE           VERSION\r\n'
  + '* Ubuntu-22.04    Running         2\r\n'
  + '  Debian          Stopped         1\r\n'

function environment(home, overrides = {}) {
  return { ...process.env, HOME: home, WSL_SHIM_LIST: `\r\n${LIST}`, ...overrides }
}

function manifestValue(overrides = {}) {
  return { format: 1, name: 'deepdocparse-wsl-runtime', version: '0.1.0',
    platform: 'linux-x86_64', archive: 'deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz',
    size: 4, sha256: '0'.repeat(64),
    python: { python_major_minor: [3, 12], cache_tag: 'cpython-312',
      soabi: 'cpython-312-x86_64-linux-gnu', machine: 'x86_64' }, ...overrides }
}

async function temporary(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-wsl-test-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  return directory
}

async function hash(file) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(file)) digest.update(chunk)
  return digest.digest('hex')
}

function isAlive(pid) {
  try { process.kill(pid, 0); return true } catch { return false }
}

async function wslBackend(root, home, overrides = {}) {
  return await createWslBackend({ wsl: shim, runtimeRoot: WSL_ROOT,
    runtimeArchive: path.join(root, 'widget.tar.gz'),
    runtimeManifest: path.join(root, 'wsl-runtime.json'),
    directory: path.join(root, 'sessions'), environment: environment(home), ...overrides })
}

let shared = null
after(async () => {
  if (shared) { await rm(shared, { recursive: true, force: true }); shared = null }
})

async function sharedHome() {
  shared ??= await mkdtemp(path.join(os.tmpdir(), 'ddp-wsl-real-'))
  return shared
}

async function realTarball(t) {
  let manifest = null
  try { manifest = JSON.parse(await readFile(path.join(realDirectory, 'wsl-runtime.json'), 'utf8')) } catch { /* absent */ }
  const archive = manifest ? path.join(realDirectory, manifest.archive) : null
  if (!manifest || !existsSync(archive)) {
    process.stderr.write(`[runtime-wsl] ${realDirectory}/wsl-runtime.json or its tarball is missing; `
      + 'run scripts/build_wsl_runtime.py to exercise this test\n')
    t.skip('W3 WSL runtime tarball not present')
    return null
  }
  return { manifest, manifestPath: path.join(realDirectory, 'wsl-runtime.json'), archive }
}

function makeBundle() {
  return new Promise((resolve, reject) => {
    const child = spawn(path.join(repository, '.venv/bin/python'), [bundleBuilder], {
      env: { ...process.env, PYTHONPATH: path.join(repository, 'python/ddp_core') },
    })
    const chunks = []
    child.stdout.on('data', chunk => chunks.push(chunk))
    child.stderr.on('data', () => {})
    child.once('error', reject)
    child.once('close', code => {
      if (code === 0) resolve(Buffer.concat(chunks))
      else reject(new Error(`make_bundle.py exited ${code}`))
    })
  })
}

test('the default wsl.exe is the System32 path on win32 and stays injectable', () => {
  assert.equal(resolveWslExecutable('linux', { SystemRoot: 'D:\\Windows' }), 'wsl.exe')
  assert.equal(resolveWslExecutable('darwin', {}), 'wsl.exe')
  assert.equal(resolveWslExecutable('win32', { SystemRoot: 'D:\\Windows' }),
    path.join('D:\\Windows', 'System32', 'wsl.exe'))
  assert.equal(resolveWslExecutable('win32', {}),
    path.join('C:\\Windows', 'System32', 'wsl.exe'))
})

test('detection decodes UTF-16LE and classifies ok, wsl1, missing, distro and list errors', linuxOnly, async t => {
  const root = await temporary(t)
  const archive = path.join(root, 'deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz')
  await writeFile(archive, Buffer.from('placeholder'))
  const manifest = path.join(root, 'wsl-runtime.json')
  await writeFile(manifest, JSON.stringify(manifestValue()))
  await mkdir(path.join(root, 'sessions'))
  const base = { wsl: shim, runtimeRoot: WSL_ROOT, runtimeArchive: archive,
    runtimeManifest: manifest, directory: path.join(root, 'sessions'), environment: environment(root) }

  const ok = await createWslBackend(base)
  assert.equal(ok.name, 'wsl')
  assert.equal(ok.isolation, 'wsl_vm')
  assert.equal(typeof ok.spawn, 'function')
  assert.equal(typeof ok.stop, 'function')
  assert.equal(typeof ok.validateBundle, 'function')
  assert.equal(typeof ok.cleanupOrphans, 'function')
  assert.ok(await createWslBackend({ ...base, distro: 'Ubuntu-22.04' }))
  await assert.rejects(createWslBackend({ ...base, distro: 'Debian' }),
    error => error.code === 'wsl1_unsupported')
  await assert.rejects(createWslBackend({ ...base, distro: 'Fedora' }),
    error => error.code === 'wsl_distro_not_found')
  await assert.rejects(createWslBackend({ ...base, wsl: path.join(root, 'absent-wsl.exe') }),
    error => error.code === 'wsl_missing')
  await assert.rejects(createWslBackend({ ...base,
    environment: environment(root, { WSL_SHIM_LIST_EXIT: '1' }) }), error => error.code === 'wsl_unavailable')
  await assert.rejects(createWslBackend({ ...base,
    environment: environment(root, { WSL_SHIM_LIST: 'garbage output' }) }), error => error.code === 'wsl_unavailable')
  await assert.rejects(createWslBackend({ ...base, distro: 'not a distro' }),
    error => error.code === 'invalid_wsl_distro')
  await assert.rejects(createWslBackend({ kind: 'wsl' }),
    error => error.code === 'wsl_backend_unavailable')
  await assert.rejects(createRuntimeBackend({ kind: 'wsl' }),
    error => error.code === 'wsl_backend_unavailable')
  const native = await createRuntimeBackend({ kind: 'native', python: '/opt/python',
    launcher: '/app/src/runtime-launcher.py', pythonPaths: [], cwd: '/tmp' })
  assert.equal(native.name, 'native')

  process.env.DDP_WSL_DISTRO = 'Debian'
  try {
    await assert.rejects(createWslBackend(base), error => error.code === 'wsl1_unsupported')
  } finally { delete process.env.DDP_WSL_DISTRO }

  const decoded = decodeWslText(Buffer.from('\uFEFF  NAME  STATE  VERSION\r\n* Ubuntu  Running  2\r\n', 'utf16le'))
  assert.match(decoded, /^  NAME/)
  assert.equal(decoded.includes('\u0000'), false)
  assert.deepEqual(parseWslDistros(decoded), [
    { name: 'Ubuntu', state: 'Running', version: 2, default: true },
  ])
  assert.equal(selectWslDistro(parseWslDistros(decoded), 'ubuntu').name, 'Ubuntu')
  assert.equal(selectWslDistro(parseWslDistros(decoded), 'missing'), null)
  assert.equal(decodeWslText(Buffer.from('plain utf8\r\n')), 'plain utf8\r\n')
})

test('provisioning refuses a tarball or manifest that does not match the W3 release pins', linuxOnly, async t => {
  const root = await temporary(t)
  const home = path.join(root, 'home')
  await mkdir(home)
  await mkdir(path.join(root, 'sessions'))
  const archive = path.join(root, 'deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz')
  await writeFile(archive, Buffer.from('not the runtime'))
  const info = await stat(archive)
  const manifest = path.join(root, 'wsl-runtime.json')
  const backend = () => wslBackend(root, home, { runtimeArchive: archive, runtimeManifest: manifest })

  await writeFile(manifest, JSON.stringify(manifestValue({ archive: path.basename(archive),
    size: info.size, sha256: 'f'.repeat(64) })))
  await assert.rejects((await backend()).prepare(), error => error.code === 'wsl_runtime_archive_mismatch')

  await writeFile(manifest, JSON.stringify(manifestValue({ archive: path.basename(archive),
    size: info.size + 1, sha256: await hash(archive) })))
  await assert.rejects((await backend()).prepare(), error => error.code === 'wsl_runtime_archive_mismatch')

  await writeFile(manifest, JSON.stringify(manifestValue({ archive: 'other.tar.gz',
    size: info.size, sha256: await hash(archive) })))
  await assert.rejects((await backend()).prepare(), error => error.code === 'wsl_runtime_manifest_invalid')

  await writeFile(manifest, '{ not json')
  await assert.rejects((await backend()).prepare(), error => error.code === 'wsl_runtime_manifest_invalid')
  assert.equal(existsSync(path.join(home, '.deepdocparse/runtime')), false)
})

test('provisioning removes the root when the installed interpreter ABI differs from the manifest', linuxOnly, async t => {
  const root = await temporary(t)
  const home = path.join(root, 'home')
  await mkdir(home)
  const bin = path.join(root, 'stage/top/runtime/python/bin')
  await mkdir(bin, { recursive: true })
  const fake = path.join(bin, 'python3')
  await writeFile(fake, '#!/bin/sh\n'
    + 'echo \'{"python_major_minor":[3,11],"cache_tag":"cpython-311",'
    + '"soabi":"cpython-311-x86_64-linux-gnu","machine":"aarch64"}\'\n')
  await chmod(fake, 0o755)
  const archive = path.join(root, 'synthetic.tar.gz')
  const tar = spawnSync('tar', ['-czf', archive, '-C', path.join(root, 'stage'), 'top'])
  assert.equal(tar.status, 0, String(tar.stderr))
  const info = await stat(archive)
  const manifest = path.join(root, 'wsl-runtime.json')
  await writeFile(manifest, JSON.stringify(manifestValue({ archive: path.basename(archive),
    size: info.size, sha256: await hash(archive) })))
  await mkdir(path.join(root, 'sessions'))
  const backend = await wslBackend(root, home, { runtimeArchive: archive, runtimeManifest: manifest })
  await assert.rejects(backend.prepare(), error => error.code === 'wsl_runtime_abi_mismatch')
  assert.equal(existsSync(path.join(home, '.deepdocparse/runtime')), false)
  const names = await readdir(path.join(home, '.deepdocparse'))
  assert.equal(names.some(name => name.includes('.tmp-')), false)
})

test('ready times out without a bootstrap line and stop never terminates the distribution', linuxOnly, async t => {
  const root = await temporary(t)
  const home = path.join(root, 'home')
  await mkdir(home)
  const runtime = path.join(home, '.deepdocparse/runtime')
  await mkdir(path.join(runtime, 'runtime/python/bin'), { recursive: true })
  await mkdir(path.join(runtime, 'app/src'), { recursive: true })
  const fake = path.join(runtime, 'runtime/python/bin/python3')
  await writeFile(fake, '#!/bin/sh\necho $$ > "$HOME/fake.pid"\nexec sleep 600\n')
  await chmod(fake, 0o755)
  await writeFile(path.join(runtime, 'app/src/runtime-launcher.py'), '')
  const manifestJson = manifestValue()
  const manifest = path.join(root, 'wsl-runtime.json')
  await writeFile(manifest, JSON.stringify(manifestJson))
  await writeFile(path.join(runtime, 'INSTALLED.json'), JSON.stringify({ format: 1,
    version: manifestJson.version, sha256: manifestJson.sha256 }))
  const archive = path.join(root, manifestJson.archive)
  await writeFile(archive, 'unused')
  await mkdir(path.join(root, 'sessions'))
  const calls = []
  const spawnProcess = (command, args, options) => { calls.push({ command, args }); return spawn(command, args, options) }
  const backend = await wslBackend(root, home, { runtimeArchive: archive, runtimeManifest: manifest,
    spawnProcess, startupMs: 500 })
  const handle = await backend.spawn({ workspace: WSL_WORKSPACE,
    sessionDir: path.join(root, 'sessions'), tokenFile: path.join(root, 'sessions/session.json') })
  assert.equal(calls[0].command, shim, 'an injected wsl executable must win over the default')
  const started = Date.now()
  await assert.rejects(handle.ready(), error => error.code === 'runtime_start_failed')
  assert.ok(Date.now() - started < 5000)
  await backend.stop(handle.child, { graceMs: 200 })
  assert.ok(handle.child.exitCode !== null || handle.child.signalCode !== null)
  const fakePid = Number(await readFile(path.join(home, 'fake.pid'), 'utf8'))
  t.after(() => { try { process.kill(fakePid, 'SIGKILL') } catch { /* already gone */ } })
  assert.equal(calls.some(call => call.args.includes('--terminate')), false)
  assert.equal(calls.some(call => call.args.includes('kill')), false)
})

test('orphan cleanup kills only recorded pids whose argv carries our runtime launcher', linuxOnly, async t => {
  const root = await temporary(t)
  const home = path.join(root, 'home')
  await mkdir(home)
  const sessions = path.join(root, 'sessions')
  await mkdir(path.join(sessions, 'owned-live'), { recursive: true })
  await mkdir(path.join(sessions, 'owned-stale'), { recursive: true })
  await mkdir(path.join(sessions, 'owned-foreign'), { recursive: true })
  const launcher = path.join(home, '.deepdocparse/runtime/app/src/runtime-launcher.py')
  const matching = spawn('/usr/bin/python3', ['-c', 'import time; time.sleep(60)', launcher], { stdio: 'ignore' })
  const other = spawn('/usr/bin/python3', ['-c', 'import time; time.sleep(60)', '/tmp/not-our-runtime.py'], { stdio: 'ignore' })
  t.after(() => {
    for (const child of [matching, other]) {
      try { if (child.exitCode === null) child.kill('SIGKILL') } catch { /* already gone */ }
    }
  })
  const closed = new Promise(resolve => matching.once('close', resolve))
  await writeFile(path.join(sessions, 'owned-live/wsl-pid'), String(matching.pid))
  await writeFile(path.join(sessions, 'owned-foreign/wsl-pid'), String(other.pid))
  await writeFile(path.join(sessions, 'owned-stale/wsl-pid'), '999999')
  const backend = await wslBackend(root, home)
  const result = await backend.cleanupOrphans()
  assert.deepEqual(result, { scanned: 3, killed: [matching.pid] })
  await closed
  assert.equal(isAlive(other.pid), true)
  assert.equal(existsSync(path.join(sessions, 'owned-live')), false)
  assert.equal(existsSync(path.join(sessions, 'owned-stale')), false)
  assert.equal(existsSync(path.join(sessions, 'owned-foreign')), false)
  assert.deepEqual(await backend.cleanupOrphans(), { scanned: 0, killed: [] })
})

test('WSL workspaces are virtual, stable and never touched through the host filesystem', linuxOnly, async t => {
  const handles = new WorkspaceHandles()
  const first = handles.selectedWsl({ directory: WSL_WORKSPACE })
  assert.deepEqual(Object.keys(first).sort(), ['name', 'workspaceId'])
  assert.equal(first.name, 'default')
  assert.equal(handles.selectedWsl({ directory: WSL_WORKSPACE }).workspaceId, first.workspaceId)
  assert.equal(await handles.directory(first.workspaceId), WSL_WORKSPACE)
  assert.throws(() => handles.selectedWsl({ directory: 'relative/path' }), /invalid_workspace/)
  assert.throws(() => handles.selectedWsl({ directory: '~/x; rm -rf /' }), /invalid_workspace/)
  const root = await temporary(t)
  await mkdir(path.join(root, 'Docs'))
  const native = await handles.selectedByNativeDialog(path.join(root, 'Docs'))
  assert.equal(path.basename(await handles.directory(native.workspaceId)), 'Docs')
  assert.equal(path.basename(await realpath(path.join(root, 'Docs'))), await handles.public(native.workspaceId).name)
})

test('the real W3 tarball provisions, launches, handshakes and stops by inner pid', linuxOnly, async t => {
  const real = await realTarball(t)
  if (!real) return
  const home = await sharedHome()
  const sessions = path.join(home, 'sessions')
  await mkdir(sessions, { recursive: true })
  const calls = []
  const spawnProcess = (command, args, options) => { calls.push({ command, args }); return spawn(command, args, options) }
  const backend = await wslBackend(home, home, { runtimeArchive: real.archive,
    runtimeManifest: real.manifestPath, environment: environment(home), spawnProcess, startupMs: 60000 })
  const prepared = await backend.prepare()
  assert.equal(prepared.installed, true)
  assert.equal(prepared.version, real.manifest.version)
  assert.equal(prepared.sha256, real.manifest.sha256)
  const root = path.join(home, '.deepdocparse/runtime')
  const installed = JSON.parse(await readFile(path.join(root, 'INSTALLED.json'), 'utf8'))
  assert.equal(installed.version, real.manifest.version)
  assert.equal(installed.sha256, real.manifest.sha256)
  await cp(path.join(repository, 'python/ddp_local/ddp_local'),
    path.join(root, 'runtime/site-packages/ddp_local'), { recursive: true, force: true })
  const canary = path.join(root, 'keep-me')
  await writeFile(canary, 'canary')
  assert.equal((await backend.prepare()).installed, false)
  assert.equal(await readFile(canary, 'utf8'), 'canary')
  let handle = null
  try {
    handle = await backend.spawn({ workspace: WSL_WORKSPACE, sessionDir: sessions,
      tokenFile: path.join(sessions, 'session.json') })
    const ready = await handle.ready()
    assert.match(ready.url, /^http:\/\/127\.0\.0\.1:\d+$/)
    assert.match(ready.token, /^[A-Za-z0-9_-]{32,128}$/)
    const innerPid = Number((await readFile(path.join(sessions, 'wsl-pid'), 'utf8')).trim())
    assert.ok(Number.isInteger(innerPid) && innerPid > 0)
    assert.notEqual(innerPid, handle.child.pid)
    const value = await handshake(ready)
    assert.equal(value.protocol_version, 'ddp-client/1')
    assert.equal(typeof value.identity.workspace_id, 'string')
    await backend.stop(handle.child, { graceMs: 10000 })
    handle = null
    const kill = calls.find(call => call.args.includes('kill') && call.args.includes('-TERM'))
    assert.ok(kill, 'stop must signal through wsl.exe')
    assert.deepEqual(kill.args.slice(-2), ['-TERM', String(innerPid)])
    await delay(50)
    assert.equal(isAlive(innerPid), false)
  } finally {
    if (handle) { try { handle.child.kill('SIGKILL') } catch { /* already gone */ } }
  }
})

test('validateBundle speaks the bundled runtime-files.py protocol inside WSL', linuxOnly, async t => {
  const real = await realTarball(t)
  if (!real) return
  const home = await sharedHome()
  await mkdir(path.join(home, 'sessions'), { recursive: true })
  const backend = await wslBackend(home, home, { runtimeArchive: real.archive,
    runtimeManifest: real.manifestPath, environment: environment(home) })
  await backend.prepare()
  await assert.rejects(backend.validateBundle(Buffer.from('not-a-bundle')),
    error => error.code === 'invalid_bundle')
  await backend.validateBundle(await makeBundle())
  const missing = await wslBackend(home, home, { runtimeArchive: real.archive,
    runtimeManifest: real.manifestPath, environment: environment(home),
    runtimeRoot: '~/.deepdocparse/not-installed' })
  await assert.rejects(missing.validateBundle(Buffer.from('x')),
    error => error.code === 'wsl_runtime_not_installed')
})
