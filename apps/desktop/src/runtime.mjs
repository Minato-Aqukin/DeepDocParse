import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { HostError } from './policy.mjs'
import { platformName, secureDirectory } from './platform.mjs'
import { createRuntimeBackend } from './runtime-backends.mjs'
import { handshake, readConnection, runtimeEnvironment } from './runtime-native.mjs'

export { handshake, readConnection, runtimeEnvironment } from './runtime-native.mjs'

export class OwnedRuntimeManager {
  #entries = new Map()
  #suspended = []
  #closing = false
  #backendPromise
  constructor({ workspaces, directory, python, launcher, pythonPaths, cwd,
    spawnProcess = spawn, startupMs = 15000, shutdownMs = 3000, backend }) {
    Object.assign(this, { workspaces, directory, python, launcher, pythonPaths, cwd,
      spawnProcess, startupMs, shutdownMs })
    const selected = backend ?? createRuntimeBackend({ kind: 'native', python, launcher, pythonPaths,
      cwd, spawnProcess, startupMs, shutdownMs })
    this.#backendPromise = Promise.resolve(selected)
    // A backend that is selected but not yet used must not surface as an unhandled rejection.
    this.#backendPromise.catch(() => {})
  }
  async #backend() { return this.#backendPromise }
  async #session() {
    await secureDirectory(this.directory, { create: true, platform: platformName(), code: 'unsafe_runtime_directory' })
    return mkdtemp(path.join(this.directory, 'owned-'))
  }
  status(workspaceId) {
    this.workspaces.public(workspaceId)
    const entry = this.#entries.get(workspaceId)
    return { workspaceId, state: entry?.state ?? 'stopped', generation: entry?.generation ?? 0,
      reason: entry?.reason ?? null, identity: entry?.handshake?.identity ?? null }
  }
  start(workspaceId) {
    if (this.#closing) return Promise.reject(new HostError('host_closing'))
    this.workspaces.public(workspaceId)
    const previous = this.#entries.get(workspaceId)
    if (previous?.startPromise) return previous.startPromise
    if (previous?.state === 'ready') return Promise.resolve(this.status(workspaceId))
    if (previous?.state === 'stopping') return Promise.reject(new HostError('runtime_stopping'))
    const entry = { state: 'starting', generation: (previous?.generation ?? 0) + 1 }
    this.#entries.set(workspaceId, entry)
    entry.startPromise = this.#start(workspaceId, entry).finally(() => { entry.startPromise = null })
    return entry.startPromise
  }
  #awaitReady(entry, handle) {
    let cancel
    const cancelled = new Promise((_resolve, reject) => {
      cancel = () => reject(new HostError('runtime_stopped'))
    })
    entry.cancelReady = cancel
    return Promise.race([handle.ready(), cancelled]).finally(() => { entry.cancelReady = null })
  }
  async #start(workspaceId, entry) {
    try {
      const workspace = await this.workspaces.directory(workspaceId)
      entry.session = await this.#session()
      if (entry.stopRequested) throw new HostError('runtime_stopped')
      const tokenFile = path.join(entry.session, 'session.json')
      const backend = await this.#backend()
      const handle = await backend.spawn({ workspace, sessionDir: entry.session, tokenFile })
      entry.child = handle.child
      entry.closed = new Promise(resolve => {
        handle.child.once('close', () => {
          entry.alive = false
          entry.connection = null
          if (!entry.stopRequested) { entry.state = 'failed'; entry.reason = 'runtime_exited' }
          resolve()
        })
      })
      handle.child.once('error', () => { entry.spawnFailed = true })
      entry.alive = true
      const deadline = Date.now() + this.startupMs
      while (Date.now() < deadline && !entry.stopRequested && entry.alive && !entry.spawnFailed) {
        let connection, value
        try {
          connection = await this.#awaitReady(entry, handle)
          value = await handshake(connection)
        } catch (error) {
          if (error instanceof HostError && error.code === 'unsafe_runtime_token') throw error
          if (entry.stopRequested || !entry.alive || entry.spawnFailed) break
          await delay(80)
          continue
        }
        if (entry.stopRequested || !entry.alive) break
        entry.connection = connection
        entry.handshake = value
        entry.state = 'ready'
        entry.reason = null
        return this.status(workspaceId)
      }
      throw new HostError(entry.stopRequested ? 'runtime_stopped' : 'runtime_start_failed')
    } catch (error) {
      await this.#terminate(entry)
      entry.state = entry.stopRequested ? 'stopped' : 'failed'
      entry.reason = error instanceof HostError ? error.code : 'runtime_start_failed'
      throw new HostError(entry.reason)
    }
  }
  async #terminate(entry) {
    // Only a ChildProcess created by the selected backend can ever be signalled.
    if (entry.child && entry.alive) {
      const backend = await this.#backend()
      await backend.stop(entry.child, { graceMs: this.shutdownMs })
    }
    entry.connection = null
    if (entry.session) await rm(entry.session, { recursive: true, force: true })
  }
  async stop(workspaceId, reason = 'user_stopped') {
    this.workspaces.public(workspaceId)
    const entry = this.#entries.get(workspaceId)
    if (!entry) return this.status(workspaceId)
    if (entry.stopPromise) return entry.stopPromise
    entry.stopRequested = true
    entry.state = 'stopping'
    entry.cancelReady?.()
    entry.stopPromise = (async () => {
      await entry.startPromise?.catch(() => {})
      await this.#terminate(entry)
      entry.state = 'stopped'; entry.reason = reason
      return this.status(workspaceId)
    })().finally(() => { entry.stopPromise = null })
    return entry.stopPromise
  }
  wake(workspaceId) { return this.start(workspaceId) }
  connection(workspaceId) {
    const entry = this.#entries.get(workspaceId)
    if (!entry?.connection || entry.state !== 'ready') throw new HostError('runtime_unavailable')
    return { ...entry.connection, handshake: structuredClone(entry.handshake) }
  }
  activeCount() { return [...this.#entries.values()].filter(x => ['ready', 'starting'].includes(x.state)).length }
  async suspend() {
    this.#suspended = [...this.#entries].filter(([, x]) => ['ready', 'starting'].includes(x.state)).map(([id]) => id)
    await Promise.all(this.#suspended.map(id => this.stop(id, 'system_suspended')))
  }
  async resume() {
    const selected = this.#suspended; this.#suspended = []
    if (!this.#closing) await Promise.allSettled(selected.map(id => this.start(id)))
  }
  async validateBundle(data) {
    if (this.#closing || data.length > 32 * 1024 * 1024) throw new HostError('invalid_bundle')
    const backend = await this.#backend()
    await backend.validateBundle(data)
  }
  async shutdown() {
    this.#closing = true
    this.#suspended = []
    await Promise.all([...this.#entries.keys()].map(id => this.stop(id, 'host_quit')))
  }
}
