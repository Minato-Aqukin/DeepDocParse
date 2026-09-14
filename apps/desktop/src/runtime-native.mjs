import { spawn } from 'node:child_process'
import { constants } from 'node:fs'
import { open } from 'node:fs/promises'
import http from 'node:http'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { HostError } from './policy.mjs'
import { platformName, secureFile } from './platform.mjs'

const READY_POLL_MS = 80

export function runtimeEnvironment(pythonPaths, platform = process.platform, source = process.env) {
  if (platform === 'win32') {
    const env = { PYTHONPATH: pythonPaths.join(path.delimiter), PYTHONSAFEPATH: '1',
      PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1' }
    for (const key of ['SystemRoot', 'SystemDrive', 'TEMP', 'USERPROFILE', 'PATH']) {
      if (source[key]) env[key] = source[key]
    }
    return env
  }
  const env = { PATH: '/usr/bin:/bin', PYTHONPATH: pythonPaths.join(path.delimiter),
    PYTHONSAFEPATH: '1', PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1' }
  for (const key of ['HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ']) {
    if (source[key]) env[key] = source[key]
  }
  return env
}

export async function readConnection(file, pid) {
  await secureFile(file, { platform: platformName(), maxBytes: 8192, code: 'unsafe_runtime_token' })
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const stat = await handle.stat()
    if (!stat.isFile() || stat.size > 8192
        || (platformName() !== 'win32' && ((stat.mode & 0o077) !== 0
          || (process.getuid && stat.uid !== process.getuid())))) throw new HostError('unsafe_runtime_token')
    const value = JSON.parse(await handle.readFile('utf8'))
    const url = new URL(value.url)
    if (value.pid !== pid || url.protocol !== 'http:' || url.hostname !== '127.0.0.1'
        || !url.port || url.pathname !== '/' || url.search || url.hash || url.username || url.password
        || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(value.token)) {
      throw new HostError('unsafe_runtime_token')
    }
    return { url: url.origin, token: value.token }
  } finally { await handle.close() }
}

// Main process only. Fixed bounded endpoint, literal loopback, no Origin, proxy or redirect.
export function handshake(connection) {
  return new Promise((resolve, reject) => {
    const request = http.get(`${connection.url}/api/v1/client/handshake`, {
      headers: { Authorization: `Bearer ${connection.token}` }, timeout: 1500,
    }, response => {
      if (response.statusCode !== 200) { response.resume(); reject(new HostError('runtime_unavailable')); return }
      let size = 0, chunks = []
      response.on('data', data => {
        size += data.length
        if (size > 65536) response.destroy(new HostError('runtime_unavailable'))
        else chunks.push(data)
      })
      response.on('error', reject)
      response.on('end', () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
          if (value.protocol_version !== 'ddp-client/1' || !value.identity?.environment_id
              || !value.identity?.workspace_id) throw new HostError('runtime_incompatible')
          resolve(value)
        } catch { reject(new HostError('runtime_incompatible')) }
      })
    })
    request.on('timeout', () => request.destroy(new HostError('runtime_unavailable')))
    request.on('error', reject)
  })
}

function bundleValidator(spawnProcess, python, launcher, pythonPaths, cwd) {
  return async data => {
    const child = spawnProcess(python, ['-S', '-P', path.join(path.dirname(launcher), 'runtime-files.py')], {
      shell: false, detached: false, stdio: ['pipe', 'pipe', 'ignore'], cwd,
      env: runtimeEnvironment(pythonPaths),
    })
    await new Promise((resolve, reject) => {
      let output = '', finished = false
      const timer = setTimeout(() => { child.kill('SIGKILL') }, 10000)
      const done = success => {
        if (finished) return; finished = true; clearTimeout(timer)
        success ? resolve() : reject(new HostError('invalid_bundle'))
      }
      child.on('error', () => done(false))
      child.stdout.on('data', chunk => { output += chunk.toString(); if (output.length > 100) child.kill('SIGKILL') })
      child.stdin.on('error', () => {})
      child.on('close', code => done(code === 0 && output === 'validated'))
      child.stdin.end(data)
    })
  }
}

export function createNativeBackend(options) {
  const { python, launcher, pythonPaths, cwd, spawnProcess = spawn,
    startupMs = 15000, shutdownMs = 3000 } = options
  return {
    name: 'native',
    isolation: 'owned_child_process',
    async spawn({ workspace, sessionDir, tokenFile }) {
      const child = spawnProcess(python, ['-S', '-P', launcher, '--workspace', workspace,
        'serve', '--port', '0', '--token-file', tokenFile], {
        shell: false, detached: false, stdio: 'ignore', cwd,
        env: runtimeEnvironment(pythonPaths),
      })
      let alive = true, failed = false
      child.once('close', () => { alive = false })
      child.once('error', () => { failed = true })
      return {
        child,
        ready: async () => {
          const deadline = Date.now() + startupMs
          while (Date.now() < deadline && alive && !failed) {
            try { return await readConnection(tokenFile, child.pid) }
            catch (error) {
              if (error instanceof HostError && error.code === 'unsafe_runtime_token') throw error
              await delay(READY_POLL_MS)
            }
          }
          throw new HostError('runtime_start_failed')
        },
      }
    },
    async stop(child, { graceMs = shutdownMs } = {}) {
      if (!child || child.exitCode !== null || child.signalCode !== null) return
      const closed = new Promise(resolve => child.once('close', resolve))
      child.kill('SIGTERM')
      await Promise.race([closed, delay(graceMs)])
      if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await closed }
    },
    validateBundle: bundleValidator(spawnProcess, python, launcher, pythonPaths, cwd),
  }
}
