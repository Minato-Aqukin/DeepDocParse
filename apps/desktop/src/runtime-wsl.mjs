import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { Readable } from 'node:stream'
import { setTimeout as delay } from 'node:timers/promises'
import { HostError } from './policy.mjs'

const SESSION_PID_FILE = 'wsl-pid'
const INSTALLED_FILE = 'INSTALLED.json'
const PATH_PATTERN = /^[A-Za-z0-9._~/-]+$/
const DISTRO_PATTERN = /^[A-Za-z0-9._-]{1,64}$/
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/
const SHA256_PATTERN = /^[0-9a-f]{64}$/
const ABI_FIELDS = ['python_major_minor', 'cache_tag', 'soabi', 'machine']

// Bare `wsl.exe` resolves against the child's PATH, which the Electron host
// deliberately trims (runtimeEnvironment keeps only SystemRoot/SystemDrive/
// TEMP/USERPROFILE/PATH). System32 is where Windows keeps it and is on the
// system search path regardless; a caller-supplied `wsl` (the test shims) must
// still win, which is why this is only the default.
export function resolveWslExecutable(platform = process.platform, environment = process.env) {
  if (platform !== 'win32') return 'wsl.exe'
  const root = environment?.SystemRoot || 'C:\\Windows'
  return path.join(root, 'System32', 'wsl.exe')
}

export function decodeWslText(buffer) {
  const text = buffer.includes(0) ? buffer.toString('utf16le') : buffer.toString('utf8')
  const withoutBom = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
  return withoutBom.replace(/\u0000/g, '')
}

export function parseWslDistros(text) {
  const rows = []
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.trim()
    if (!line) continue
    if (/^NAME\s+STATE\s+VERSION$/i.test(line)) continue
    const columns = line.split(/\s{2,}/).map(column => column.trim()).filter(Boolean)
    if (columns.length < 3) return null
    const version = Number(columns[columns.length - 1])
    if (!Number.isInteger(version)) return null
    const labelled = columns[0]
    const isDefault = labelled.startsWith('*')
    const name = isDefault ? labelled.slice(1).trim() : labelled
    if (!name) return null
    rows.push({ name, state: columns[1], version, default: isDefault })
  }
  return rows.length ? rows : null
}

export function selectWslDistro(distros, requested) {
  if (!Array.isArray(distros) || distros.length === 0) return null
  if (requested) return distros.find(candidate => candidate.name.toLowerCase() === requested.toLowerCase()) ?? null
  const defaults = distros.filter(candidate => candidate.default)
  if (defaults.length === 1) return defaults[0]
  if (defaults.length === 0 && distros.length === 1) return distros[0]
  return null
}

export async function detectWsl({ executable = resolveWslExecutable(), distro = null, spawnProcess = spawn,
  environment, timeoutMs = 15000 } = {}) {
  const outcome = await runProcess({ spawnProcess, executable, args: ['-l', '-v'], environment,
    timeoutMs, maxBytes: 1024 * 1024 })
  if (outcome.status === 'missing') return { status: 'missing' }
  if (outcome.status !== 'settled') return { status: 'error', reason: outcome.status }
  if (outcome.exitCode !== 0) return { status: 'error', reason: 'wsl_exit' }
  const distros = parseWslDistros(decodeWslText(outcome.stdout))
  if (!distros) return { status: 'error', reason: 'unlistable' }
  const selected = selectWslDistro(distros, distro)
  if (!selected) return { status: 'error', reason: distro ? 'distro_not_found' : 'default_distro_ambiguous' }
  if (selected.version === 1) return { status: 'wsl1', distro: selected.name, state: selected.state }
  if (selected.version !== 2) return { status: 'error', reason: 'version_unsupported' }
  return { status: 'ok', distro: selected.name, state: selected.state }
}

export async function createWslBackend(options = {}) {
  const {
    distro: requestedDistro = null, runtimeRoot, runtimeArchive, runtimeManifest,
    spawnProcess = spawn, startupMs = 20000, shutdownMs = 3000, directory,
    wsl = resolveWslExecutable(), environment, detectMs = 15000, provisionMs = 600000,
    validateMs = 15000, killMs = 10000,
  } = options
  if (typeof runtimeRoot !== 'string' || !PATH_PATTERN.test(runtimeRoot)
      || typeof runtimeManifest !== 'string' || !runtimeManifest
      || typeof runtimeArchive !== 'string' || !runtimeArchive
      || typeof directory !== 'string' || !directory) {
    throw new HostError('wsl_backend_unavailable')
  }
  const distro = requestedDistro || process.env.DDP_WSL_DISTRO || null
  if (distro !== null && !DISTRO_PATTERN.test(distro)) throw new HostError('invalid_wsl_distro')
  const detection = await detectWsl({ executable: wsl, distro, spawnProcess, environment, timeoutMs: detectMs })
  if (detection.status === 'missing') throw new HostError('wsl_missing')
  if (detection.status === 'wsl1') throw new HostError('wsl1_unsupported')
  if (detection.status !== 'ok') {
    throw new HostError(detection.reason === 'distro_not_found' ? 'wsl_distro_not_found' : 'wsl_unavailable')
  }
  const selectedDistro = detection.distro

  const children = new Map()
  let provisionPromise = null

  const invoke = (args, runOptions = {}) => runProcess({ spawnProcess, executable: wsl, args,
    environment, ...runOptions })
  // Always `--exec`, never `--`. With `--` wsl.exe hands the rest of the command line to
  // the distro's *default* shell as one string: that shell (bash, zsh, fish, ...) expands
  // `$root`/`$tmp`/`$pid` to empty and eats the quoting before our `bash -lc` script even
  // starts, so provisioning runs `rm -rf ""` and the marker check reads "/INSTALLED.json".
  // `--exec` runs argv directly without any Linux shell in between.
  const inDistro = argv => ['-d', selectedDistro, '--exec', ...argv]
  const invokeShell = (script, runOptions = {}) => invoke(inDistro(['bash', '-lc', script]), runOptions)

  async function readMarker() {
    const outcome = await invokeShell(
      `root=${runtimeRoot}\nif [ -f "$root/${INSTALLED_FILE}" ]; then cat "$root/${INSTALLED_FILE}"; fi`,
      { timeoutMs: killMs })
    if (outcome.status !== 'settled' || outcome.exitCode !== 0) return null
    const text = outcome.stdout.toString('utf8').trim()
    if (!text) return null
    try {
      const value = JSON.parse(text)
      return value && typeof value === 'object' && !Array.isArray(value) ? value : null
    } catch { return null }
  }

  async function provisionRuntime() {
    let manifest
    try { manifest = JSON.parse(await readFile(runtimeManifest, 'utf8')) } catch {
      throw new HostError('wsl_runtime_manifest_invalid')
    }
    if (!validManifest(manifest, runtimeArchive)) throw new HostError('wsl_runtime_manifest_invalid')
    const marker = await readMarker()
    if (marker?.version === manifest.version && marker?.sha256 === manifest.sha256) {
      return { version: manifest.version, sha256: manifest.sha256, installed: false }
    }
    if (!(await archiveMatches(runtimeArchive, manifest))) throw new HostError('wsl_runtime_archive_mismatch')
    const temporary = `${runtimeRoot}.tmp-${process.pid}`
    const extracted = await invokeShell(
      `root=${runtimeRoot}\ntmp=${temporary}\n`
      + `rm -rf "$tmp" && mkdir -p "$tmp" && tar -xzf - --strip-components=1 -C "$tmp" `
      + '&& rm -rf "$root" && mv "$tmp" "$root"',
      { stdin: createReadStream(runtimeArchive), timeoutMs: provisionMs })
    if (extracted.status !== 'settled' || extracted.exitCode !== 0) {
      await invokeShell(`tmp=${temporary}\nrm -rf "$tmp"`, { timeoutMs: killMs })
      throw new HostError('wsl_runtime_provision_failed')
    }
    const probed = await invokeShell(
      `root=${runtimeRoot}\n`
      + `"$root/runtime/python/bin/python3" -S -P -c "import json,platform,sys,sysconfig; `
      + "print(json.dumps({'python_major_minor': list(sys.version_info[:2]), "
      + "'cache_tag': sys.implementation.cache_tag, "
      + "'soabi': sysconfig.get_config_var('SOABI'), 'machine': platform.machine()}))\"",
      { timeoutMs: killMs })
    if (probed.status !== 'settled' || probed.exitCode !== 0 || !sameAbi(probed.stdout, manifest.python)) {
      await invokeShell(`root=${runtimeRoot}\nrm -rf "$root"`, { timeoutMs: killMs })
      throw new HostError('wsl_runtime_abi_mismatch')
    }
    const markerJson = JSON.stringify({ format: 1, version: manifest.version, sha256: manifest.sha256 })
    const written = await invokeShell(
      `root=${runtimeRoot}\nmkdir -p "$root" && cat > "$root/${INSTALLED_FILE}"`,
      { stdin: Buffer.from(markerJson, 'utf8'), timeoutMs: killMs })
    if (written.status !== 'settled' || written.exitCode !== 0) {
      throw new HostError('wsl_runtime_provision_failed')
    }
    return { version: manifest.version, sha256: manifest.sha256, installed: true }
  }

  function prepare() {
    if (!provisionPromise) {
      provisionPromise = provisionRuntime().finally(() => { provisionPromise = null })
    }
    return provisionPromise
  }

  async function signalInner(pid, name) {
    // The shell builtin, not /usr/bin/kill (not every distro image ships procps); argv
    // reaches it through "$@", so nothing is re-parsed by a shell.
    await invoke(inDistro(['bash', '-c', 'kill "$@"', 'kill', `-${name}`, String(pid)]),
      { timeoutMs: killMs })
  }

  return {
    name: 'wsl',
    isolation: 'wsl_vm',
    prepare,
    async spawn({ workspace, sessionDir } = {}) {
      if (typeof workspace !== 'string' || !PATH_PATTERN.test(workspace)) {
        throw new HostError('invalid_workspace')
      }
      if (typeof sessionDir !== 'string' || !sessionDir) throw new HostError('invalid_runtime_session')
      await prepare()
      const script = `cd ${runtimeRoot} && PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 `
        + `PYTHONPATH=${runtimeRoot}/runtime/site-packages ${runtimeRoot}/runtime/python/bin/python3 -S -P `
        + `${runtimeRoot}/app/src/runtime-launcher.py --workspace ${workspace} serve --port 0 --token-file -`
      let child
      try {
        child = spawnProcess(wsl, inDistro(['bash', '-lc', script]),
          { shell: false, detached: false, stdio: ['ignore', 'pipe', 'ignore'], env: environment })
      } catch { throw new HostError('runtime_start_failed') }
      const state = { pid: null }
      children.set(child, state)
      child.once('close', () => { children.delete(child) })
      child.once('error', () => { children.delete(child) })
      let settled = false
      let resolveReady, rejectReady
      const readyPromise = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
      const finish = (error, value) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        if (error) rejectReady(error)
        else resolveReady(value)
      }
      const timer = setTimeout(() => finish(new HostError('runtime_start_failed')), startupMs)
      timer.unref?.()
      let pending = Buffer.alloc(0)
      child.stdout?.on('data', chunk => {
        if (settled) return
        pending = Buffer.concat([pending, chunk])
        if (pending.length > 64 * 1024) { finish(new HostError('runtime_start_failed')); return }
        let index
        while (!settled && (index = pending.indexOf(0x0a)) !== -1) {
          const line = pending.subarray(0, index).toString('utf8').replace(/\r$/, '')
          pending = pending.subarray(index + 1)
          const value = parseBootstrapLine(line)
          if (!value) continue
          state.pid = value.pid
          writeFile(path.join(sessionDir, SESSION_PID_FILE), String(value.pid), { mode: 0o600 })
            .then(() => finish(null, { url: value.url, token: value.token }))
            .catch(() => finish(new HostError('wsl_pid_record_failed')))
          return
        }
      })
      child.once('error', () => finish(new HostError('runtime_start_failed')))
      child.once('close', () => finish(new HostError('runtime_exited')))
      return { child, ready: () => readyPromise }
    },
    async stop(child, { graceMs = shutdownMs } = {}) {
      if (!child || child.exitCode !== null || child.signalCode !== null) return
      const state = children.get(child)
      const closed = new Promise(resolve => child.once('close', resolve))
      if (state?.pid) {
        await signalInner(state.pid, 'TERM')
        await Promise.race([closed, delay(graceMs)])
        if (child.exitCode !== null || child.signalCode !== null) return
        await signalInner(state.pid, 'KILL')
      } else {
        try { child.kill('SIGTERM') } catch { /* the relay is already gone */ }
        await Promise.race([closed, delay(graceMs)])
        if (child.exitCode === null && child.signalCode === null) {
          try { child.kill('SIGKILL') } catch { /* the relay is already gone */ }
        }
      }
      await Promise.race([closed, delay(graceMs)])
    },
    async validateBundle(data) {
      let bytes
      if (Buffer.isBuffer(data)) bytes = data
      else if (data instanceof Uint8Array) bytes = Buffer.from(data)
      else throw new HostError('invalid_bundle')
      const outcome = await invokeShell(
        `root=${runtimeRoot}\n`
        + `test -f "$root/app/src/runtime-files.py" || exit 3\n`
        + 'PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 '
        + 'PYTHONPATH="$root/runtime/site-packages" '
        + '"$root/runtime/python/bin/python3" -S -P "$root/app/src/runtime-files.py"',
        { stdin: bytes, timeoutMs: validateMs })
      if (outcome.status === 'missing') throw new HostError('wsl_missing')
      if (outcome.status !== 'settled') throw new HostError('invalid_bundle')
      if (outcome.exitCode === 3) throw new HostError('wsl_runtime_not_installed')
      if (outcome.exitCode !== 0 || outcome.stdout.toString('utf8') !== 'validated') {
        throw new HostError('invalid_bundle')
      }
    },
    async cleanupOrphans() {
      let entries
      try { entries = await readdir(directory, { withFileTypes: true }) }
      catch (error) {
        if (error.code === 'ENOENT') return { scanned: 0, killed: [] }
        throw new HostError('wsl_unavailable')
      }
      const killed = []
      let scanned = 0
      for (const entry of entries) {
        if (!entry.isDirectory()) continue
        const session = path.join(directory, entry.name)
        let text
        try { text = await readFile(path.join(session, SESSION_PID_FILE), 'utf8') }
        catch (error) {
          if (error.code === 'ENOENT') continue
          throw new HostError('wsl_unavailable')
        }
        const pid = Number.parseInt(text.trim(), 10)
        if (!Number.isInteger(pid) || pid <= 0) {
          await rm(session, { recursive: true, force: true }).catch(() => {})
          continue
        }
        scanned += 1
        const script = `root=${runtimeRoot}\npid=${pid}\n`
          + 'kill -0 "$pid" 2>/dev/null || exit 0\n'
          + 'command -v ps >/dev/null 2>&1 || exit 2\n'
          + 'args=$(ps -o args= -p "$pid" 2>/dev/null) || exit 2\n'
          + 'case "$args" in *"$root/app/src/runtime-launcher.py"*) ;; *) exit 0;; esac\n'
          + 'kill -TERM "$pid" 2>/dev/null || exit 0\n'
          + 'for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done\n'
          + 'kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null\n'
          + 'echo killed\n'
        const outcome = await invokeShell(script, { timeoutMs: Math.max(killMs, 10000) })
        if (outcome.status === 'settled' && outcome.exitCode === 0) {
          if (outcome.stdout.toString('utf8').includes('killed')) killed.push(pid)
          await rm(session, { recursive: true, force: true }).catch(() => {})
        }
      }
      return { scanned, killed }
    },
  }
}

function parseBootstrapLine(line) {
  const text = line.trim()
  if (!text || text[0] !== '{') return null
  let value
  try { value = JSON.parse(text) } catch { return null }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (typeof value.url !== 'string' || typeof value.token !== 'string'
      || !Number.isInteger(value.pid) || value.pid <= 0) return null
  let url
  try { url = new URL(value.url) } catch { return null }
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port
      || url.pathname !== '/' || url.search || url.hash || url.username || url.password) return null
  if (!TOKEN_PATTERN.test(value.token)) return null
  return { url: url.origin, token: value.token, pid: value.pid }
}

function validManifest(value, archivePath) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (value.format !== 1) return false
  if (typeof value.version !== 'string' || !value.version) return false
  if (value.platform !== undefined && value.platform !== 'linux-x86_64') return false
  if (typeof value.archive !== 'string' || path.basename(archivePath) !== value.archive) return false
  if (!Number.isSafeInteger(value.size) || value.size <= 0) return false
  if (typeof value.sha256 !== 'string' || !SHA256_PATTERN.test(value.sha256)) return false
  const abi = value.python
  if (!abi || typeof abi !== 'object' || Array.isArray(abi)) return false
  if (!Array.isArray(abi.python_major_minor) || abi.python_major_minor.length !== 2
      || !abi.python_major_minor.every(part => Number.isInteger(part))) return false
  return ABI_FIELDS.every(field => abi[field] !== undefined && abi[field] !== null)
}

function sameAbi(stdout, expected) {
  let actual
  try { actual = JSON.parse(stdout.toString('utf8').trim()) } catch { return false }
  if (!actual || typeof actual !== 'object') return false
  return JSON.stringify(actual.python_major_minor) === JSON.stringify(expected.python_major_minor)
    && actual.cache_tag === expected.cache_tag
    && actual.soabi === expected.soabi
    && actual.machine === expected.machine
}

async function archiveMatches(archivePath, manifest) {
  let info
  try { info = await stat(archivePath) } catch { return false }
  if (!info.isFile() || info.size !== manifest.size) return false
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(archivePath)) hash.update(chunk)
  return hash.digest('hex') === manifest.sha256
}

async function runProcess({ spawnProcess, executable, args, environment, stdin, timeoutMs,
  maxBytes = 256 * 1024 }) {
  return await new Promise(resolve => {
    let child
    try {
      child = spawnProcess(executable, args, { shell: false, detached: false,
        stdio: [stdin === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'],
        ...(environment === undefined ? {} : { env: environment }) })
    } catch (error) {
      resolve({ status: 'spawn_failed', errorCode: error?.code ?? null,
        stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) })
      return
    }
    let settled = false
    let timer
    const stdout = []
    const stderr = []
    let stdoutBytes = 0
    let stderrBytes = 0
    const finish = (status, extra = {}) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (stdin instanceof Readable && !stdin.destroyed) stdin.destroy()
      resolve({ status, stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr), ...extra })
    }
    timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* the child is already gone */ }
      finish('timeout')
    }, timeoutMs)
    child.once('error', error => finish(error?.code === 'ENOENT' ? 'missing' : 'spawn_failed',
      { errorCode: error?.code ?? null }))
    child.once('close', (code, signal) => finish('settled', { exitCode: code, signal }))
    child.stdout?.on('data', chunk => {
      if (stdoutBytes >= maxBytes) return
      stdout.push(chunk)
      stdoutBytes += chunk.length
    })
    child.stderr?.on('data', chunk => {
      if (stderrBytes >= maxBytes) return
      stderr.push(chunk)
      stderrBytes += chunk.length
    })
    if (stdin !== undefined) {
      if (!child.stdin) { finish('spawn_failed'); return }
      child.stdin.on('error', () => {})
      if (stdin instanceof Readable) {
        stdin.on('error', () => {})
        stdin.pipe(child.stdin)
      } else {
        child.stdin.end(stdin)
      }
    }
  })
}
