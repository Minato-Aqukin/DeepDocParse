import { createHash, randomUUID } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, open, rename, unlink, link } from 'node:fs/promises'
import path from 'node:path'
import { HostError } from './policy.mjs'
import { secureDirectory, secureFile } from './platform.mjs'
import { clientArguments } from './client-policy.mjs'
import { ConnectionRegistry, ConnectionFault, SqliteProjectionStore, HttpProvider, OperationFault } from './shared-client.mjs'

const copy = value => structuredClone(value)
const connectionId = (environment, profile) => 'connection-' + createHash('sha256')
  .update(JSON.stringify([environment.environmentId, profile.profileId])).digest('hex').slice(0, 40)
const profileId = actor => 'profile-' + createHash('sha256').update(JSON.stringify(actor)).digest('hex').slice(0, 40)
const binding = entry => JSON.stringify([entry.environment.workspaceId, entry.environment.authorityNodeId,
  entry.profile.issuer, entry.profile.subject])
const emptyView = () => ({ transport: 'disconnected', snapshot: 'loading', reason: null, projection: null })
const LIMIT = 32 * 1024 * 1024

export function clientFailure(error) {
  const known = error instanceof HostError || error instanceof ConnectionFault || error instanceof OperationFault
  const local = ['draft_conflict', 'idempotency_conflict'].includes(error?.message)
  return { ok: false, error: { code: known ? error.code : local ? error.message : 'host_operation_failed' } }
}

/** One host-owned reference per connection, independent of renderer subscriptions/lifetime. */
export class ClientHost {
  #entries = new Map()
  #listeners = new Map()
  #save = Promise.resolve()
  #closed = false
  #closing = false
  constructor(options) { Object.assign(this, { platform: process.platform }, options) }
  async initialize() {
    await secureDirectory(this.directory, { platform: this.platform, code: 'unsafe_client_directory' })
    this.store = new SqliteProjectionStore(path.join(this.directory, 'client.sqlite'))
    this.registry = new ConnectionRegistry(this.store)
    const configuration = path.join(this.directory, 'connections.json')
    let file
    try {
      await secureFile(configuration, { platform: this.platform, maxBytes: 1024 * 1024,
        code: 'client_configuration_invalid' })
      file = await open(configuration, constants.O_RDONLY | constants.O_NOFOLLOW)
    } catch (error) {
      if (error.code === 'ENOENT') return this
      this.store.close()
      throw new HostError('client_configuration_invalid')
    }
    try {
      const entries = JSON.parse(await file.readFile('utf8'))
      if (!Array.isArray(entries) || entries.length > 64) throw new Error('invalid')
      for (const metadata of entries) {
        if (!['local', 'remote'].includes(metadata.kind)) throw new Error('invalid')
        if (metadata.kind === 'remote') clientArguments('clientPairRemote', {
          environment: metadata.environment, profile: metadata.profile, label: metadata.label,
        })
        else {
          if (typeof metadata.directory !== 'string') throw new Error('invalid')
          // Entries persisted before the workspace kind existed were native by construction.
          metadata.workspaceKind = metadata.workspaceKind ?? 'native'
          if (!['native', 'wsl'].includes(metadata.workspaceKind)) throw new Error('invalid')
          // A native handle must stay an absolute host directory. A WSL handle is a
          // virtual path inside the distribution (~/...), validated by selectedWsl.
          if (metadata.workspaceKind === 'native' && !path.isAbsolute(metadata.directory)) throw new Error('invalid')
        }
        const entry = this.#register(metadata)
        if (metadata.kind === 'local') {
          try {
            const selected = metadata.workspaceKind === 'wsl'
              ? this.workspaces.selectedWsl({ directory: metadata.directory })
              : await this.workspaces.selectedByNativeDialog(metadata.directory)
            entry.workspaceId = selected.workspaceId
          } catch { entry.view = { ...emptyView(), transport: 'blocked', snapshot: 'failed', reason: 'workspace_unavailable' } }
        }
        // Claim only the exact saved identity binding. Cached data is always stale until handshake.
        const restored = await this.store.claim(entry.scope, binding(entry))
        entry.view.projection = restored.projection
        entry.view.snapshot = restored.projection ? 'stale' : 'loading'
        await this.store.invalidate(entry.scope, restored.epoch)
      }
    } catch { this.store.close(); throw new HostError('client_configuration_invalid') }
    finally { await file.close() }
    return this
  }
  #register(metadata) {
    const id = connectionId(metadata.environment, metadata.profile)
    const prior = this.#entries.get(id)
    if (prior) {
      if (binding(prior) !== binding(metadata) || prior.kind !== metadata.kind) throw new HostError('identity_mismatch')
      if (prior.kind === 'local' && (prior.directory !== metadata.directory
          || (prior.workspaceKind ?? 'native') !== (metadata.workspaceKind ?? 'native')))
        throw new HostError('workspace_alias_conflict')
      return prior
    }
    if (this.#entries.size >= 64) throw new HostError('connection_limit')
    const entry = { ...copy(metadata), connectionId: id,
      scope: JSON.stringify([metadata.environment.environmentId, metadata.profile.profileId]),
      revision: 0, view: emptyView(), workspaceId: null, handle: null, unsubscribe: null, pending: null }
    this.#entries.set(id, entry)
    return entry
  }
  #entry(id) {
    if (this.#closed) throw new HostError('host_closing')
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_connection')
    return entry
  }
  #summary(entry) {
    const { endpoint: _endpoint, ...environment } = entry.environment
    return { connectionId: entry.connectionId, kind: entry.kind, label: entry.label, environment,
      profile: copy(entry.profile), workspaceId: entry.workspaceId, revision: entry.revision, view: copy(entry.view) }
  }
  list() { return [...this.#entries.values()].map(entry => this.#summary(entry)) }
  #changed(entry, view) {
    entry.view = copy(view); entry.revision++
    for (const [subscriptionId, listener] of this.#listeners) if (listener.id === entry.connectionId) {
      try { listener.send({ subscriptionId, connectionId: entry.connectionId, revision: entry.revision, view: copy(view) }) }
      catch { this.#listeners.delete(subscriptionId) }
    }
  }
  subscribe({ connectionId, subscriptionId }, send) {
    const entry = this.#entry(connectionId)
    if (this.#listeners.size >= 128 && !this.#listeners.has(subscriptionId)) throw new HostError('subscription_limit')
    this.#listeners.set(subscriptionId, { id: connectionId, send })
    return this.#summary(entry)
  }
  unsubscribe({ subscriptionId }) { this.#listeners.delete(subscriptionId); return null }
  clearSubscriptions() { this.#listeners.clear() }
  #persist() {
    const data = JSON.stringify([...this.#entries.values()].map(entry => ({ kind: entry.kind, label: entry.label,
      environment: entry.environment, profile: entry.profile,
      ...(entry.kind === 'local' ? { directory: entry.directory, workspaceKind: entry.workspaceKind ?? 'native' } : {}) })))
    const write = async () => {
      const temporary = path.join(this.directory, randomUUID() + '.tmp')
      const file = await open(temporary, constants.O_CREAT | constants.O_WRONLY | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
      try { await file.writeFile(data); await file.sync() } finally { await file.close() }
      try { await rename(temporary, path.join(this.directory, 'connections.json')) }
      finally { await unlink(temporary).catch(() => {}) }
    }
    this.#save = this.#save.then(write, write)
    return this.#save
  }
  async #attach(entry, provider) {
    entry.unsubscribe?.(); entry.unsubscribe = null
    await entry.handle?.release()
    entry.handle = this.registry.acquire(entry.environment, entry.profile, provider)
    entry.unsubscribe = entry.handle.connection.subscribe(view => this.#changed(entry, view))
  }
  #provider(entry) {
    if (entry.kind === 'local') return new HttpProvider({ ownedLoopback: true, localCommands: true,
      inspect: async () => {
        const current = this.runtime.connection(entry.workspaceId)
        const actual = current.handshake.identity
        if (current.url !== entry.environment.endpoint) throw new ConnectionFault('identity_mismatch')
        return { environmentId: actual.environment_id, workspaceId: actual.workspace_id,
          authorityNodeId: actual.authority_node_id, capabilities: current.handshake.capabilities }
      },
      credential: async () => this.runtime.connection(entry.workspaceId).token,
    })
    // HttpProvider verifies the Ed25519 node challenge before asking for a credential.
    return new HttpProvider({ credential: async () => this.credentials.withCredential({
      environmentId: entry.environment.environmentId, profileId: entry.profile.profileId,
    }, secret => secret).catch(() => { throw new ConnectionFault('authentication_required') }) })
  }
  async connectLocal({ workspaceId }) {
    if (this.#closing || this.#closed) throw new HostError('host_closing')
    // No WSL backend means an honest refusal, not a fake attempt at a native runtime.
    if (this.localRuntimeFailure) throw this.localRuntimeFailure
    await this.runtime.start(workspaceId)
    const current = this.runtime.connection(workspaceId), actual = current.handshake.identity
    const environment = { environmentId: actual.environment_id, workspaceId: actual.workspace_id,
      authorityNodeId: actual.authority_node_id, endpoint: current.url }
    const profile = { profileId: profileId(current.handshake.profile), ...current.handshake.profile }
    const directory = await this.workspaces.directory(workspaceId)
    const workspaceKind = this.workspaces.kind(workspaceId)
    const prior = [...this.#entries.values()].find(entry => entry.kind === 'local' && entry.directory === directory)
    if (prior && (connectionId(environment, profile) !== prior.connectionId
        || binding({ environment, profile }) !== binding(prior)
        || (prior.workspaceKind ?? 'native') !== workspaceKind)) {
      this.#changed(prior, { ...prior.view, transport: 'blocked', snapshot: prior.view.projection ? 'stale' : 'failed', reason: 'identity_mismatch' })
      throw new HostError('identity_mismatch')
    }
    const entry = this.#register({ kind: 'local', label: this.workspaces.public(workspaceId).name, directory,
      workspaceKind, environment, profile })
    entry.workspaceId = workspaceId
    if (entry.handle && entry.environment.endpoint === environment.endpoint && entry.view.transport === 'ready')
      return this.#summary(entry)
    // Launcher ports change after restart; authority, workspace and actor must remain bound.
    entry.environment = environment
    if (!entry.pending) entry.pending = (async () => { await this.#attach(entry, this.#provider(entry)); await this.#persist() })()
      .finally(() => { entry.pending = null })
    await entry.pending
    return this.#summary(entry)
  }
  async pairRemote(input) {
    if (this.#closing || this.#closed) throw new HostError('host_closing')
    const metadata = clientArguments('clientPairRemote', input)
    const entry = this.#register({ ...metadata, kind: 'remote' })
    if (binding(entry) !== binding(metadata)) throw new HostError('identity_mismatch')
    // Never silently substitute endpoint/profile settings for an existing connection.
    if (entry.environment.endpoint !== metadata.environment.endpoint) throw new HostError('connection_relocation_required')
    if (!entry.pending) entry.pending = (async () => { await this.#attach(entry, this.#provider(entry)); await this.#persist() })()
      .finally(() => { entry.pending = null })
    await entry.pending
    return this.#summary(entry)
  }
  async wake({ connectionId }) {
    const entry = this.#entry(connectionId)
    if (entry.kind === 'local') {
      if (!entry.workspaceId) throw new HostError('workspace_unavailable')
      return this.connectLocal({ workspaceId: entry.workspaceId })
    }
    await entry.pending
    if (entry.handle) await entry.handle.connection.wake()
    else await this.#attach(entry, this.#provider(entry))
    return this.#summary(entry)
  }
  async disconnect({ connectionId }) {
    const entry = this.#entry(connectionId)
    await entry.pending
    entry.unsubscribe?.(); entry.unsubscribe = null
    await entry.handle?.release(); entry.handle = null
    this.#changed(entry, { ...entry.view, transport: 'disconnected', snapshot: entry.view.projection ? 'stale' : 'loading' })
    return this.#summary(entry)
  }
  #connection(entry) {
    if (this.#closing) throw new HostError('host_closing')
    if (!entry.handle || entry.view.transport !== 'ready' || entry.view.snapshot !== 'current') throw new HostError('connection_not_current')
    return entry.handle.connection
  }
  async query({ connectionId, name, payload }) { return this.#connection(this.#entry(connectionId)).query(name, payload) }
  async command({ connectionId, name, payload, idempotencyKey }) { return this.#connection(this.#entry(connectionId)).execute(name, payload, idempotencyKey) }
  async receipt({ connectionId, idempotencyKey }) { return this.#connection(this.#entry(connectionId)).receipt(idempotencyKey) }
  readDraft({ connectionId, key }) { return this.store.readDraft(this.#entry(connectionId).scope, key) }
  async saveDraft({ connectionId, key, expectedRevision, value }) {
    return { revision: await this.store.saveDraft(this.#entry(connectionId).scope, key, expectedRevision, value) }
  }
  #local(entry) {
    const connection = this.#connection(entry)
    if (entry.kind !== 'local') throw new HostError('approved_plan_required')
    const current = this.runtime.connection(entry.workspaceId)
    if (current.url !== entry.environment.endpoint) throw new HostError('identity_mismatch')
    return { connection, current }
  }
  async #request(entry, route, options = {}) {
    const { connection, current } = this.#local(entry)
    const response = await fetch(current.url + route, { ...options, redirect: 'error', credentials: 'omit',
      signal: AbortSignal.timeout(30000), headers: { Authorization: 'Bearer ' + current.token, ...options.headers } })
    if (!response.ok) {
      await response.body?.cancel()
      throw new HostError(response.status === 401 || response.status === 403 ? 'authentication_required'
        : response.status === 404 ? 'not_found' : 'file_operation_failed')
    }
    if (!response.body) throw new HostError('file_operation_failed')
    const reader = response.body.getReader(), chunks = []; let length = 0
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break
        length += value.length
        if (length > LIMIT) throw new HostError('file_too_large')
        chunks.push(value)
      }
    } finally { await reader.cancel() }
    // A completed response from an old connection must not escape into a new scope/generation.
    if (this.#connection(entry) !== connection || this.runtime.connection(entry.workspaceId).url !== current.url)
      throw new HostError('disposed')
    return { bytes: Buffer.concat(chunks), contentType: response.headers.get('content-type') ?? '' }
  }
  async importFile({ connectionId, kind, idempotencyKey }) {
    const entry = this.#entry(connectionId), { connection } = this.#local(entry)
    const selected = await this.selectInput(kind)
    if (!selected) return null
    const file = await open(selected, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    let bytes
    try {
      const before = await file.stat({ bigint: true })
      if (!before.isFile() || before.size > BigInt(LIMIT) || before.size < 1n) throw new HostError('file_too_large')
      // Hash and upload this one bounded in-memory snapshot, never reopen the user's mutable path.
      bytes = Buffer.alloc(Number(before.size))
      let read = 0
      while (read < bytes.length) {
        const result = await file.read(bytes, read, bytes.length - read, read)
        if (!result.bytesRead) throw new HostError('file_changed')
        read += result.bytesRead
      }
      const after = await file.stat({ bigint: true })
      if (before.size !== after.size || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs)
        throw new HostError('file_changed')
    } finally { await file.close() }
    if (kind === 'bundle') await this.runtime.validateBundle(bytes)
    else if (!bytes.subarray(0, 5).equals(Buffer.from('%PDF-'))) throw new HostError('invalid_pdf')
    if (this.#connection(entry) !== connection) throw new HostError('disposed')
    const filename = path.basename(selected)
    const digest = createHash('sha256').update(JSON.stringify({ name: 'file.import', kind, filename,
      sha256: createHash('sha256').update(bytes).digest('hex') })).digest('hex')
    const intent = await this.store.intent(entry.scope, idempotencyKey, digest)
    if (intent.status === 'confirmed') return copy(intent.receipt)
    if (intent.status === 'retired') throw new HostError('receipt_required')
    if (intent.status === 'unknown') throw new HostError('outcome_unknown')
    if (this.#connection(entry) !== connection) {
      await this.store.discardUndispatched(entry.scope, idempotencyKey, digest)
      throw new HostError('disposed')
    }
    try {
      const response = await this.#request(entry, kind === 'pdf' ? '/api/v1/resources/upload' : '/api/v1/bundles/import', {
        method: 'POST', body: bytes, headers: { 'Idempotency-Key': idempotencyKey,
          'Content-Type': kind === 'pdf' ? 'application/pdf' : 'application/zip',
          ...(kind === 'pdf' ? { 'X-Filename-Encoded': encodeURIComponent(filename) } : {}) },
      })
      if (!response.contentType.includes('application/json') || response.bytes.length > 65536) throw new HostError('protocol_incompatible')
      const receipt = JSON.parse(response.bytes.toString('utf8'))
      await this.store.settle(entry.scope, idempotencyKey, digest, receipt)
      return receipt
    } catch (error) {
      await this.store.settle(entry.scope, idempotencyKey, digest, null)
      throw error
    }
  }
  async readOriginal({ connectionId, versionId }) {
    const response = await this.#request(this.#entry(connectionId), '/api/v1/versions/' + encodeURIComponent(versionId) + '/source')
    if (!response.contentType.startsWith('application/pdf') || !response.bytes.subarray(0, 5).equals(Buffer.from('%PDF-')))
      throw new HostError('invalid_pdf')
    return new Uint8Array(response.bytes)
  }
  async exportBundle({ connectionId, versionId }) {
    const entry = this.#entry(connectionId), { connection } = this.#local(entry)
    const selected = await this.selectOutput()
    if (!selected) return { saved: false }
    if (this.#connection(entry) !== connection) throw new HostError('disposed')
    let existing
    try { existing = await lstat(selected); if (!existing.isFile() || existing.isSymbolicLink()) throw new HostError('unsafe_export_target') }
    catch (error) { if (error.code !== 'ENOENT') throw error }
    const response = await this.#request(entry, '/api/v1/versions/' + encodeURIComponent(versionId) + '/bundle')
    if (!response.contentType.startsWith('application/zip')) throw new HostError('invalid_bundle')
    await this.runtime.validateBundle(response.bytes)
    if (this.#connection(entry) !== connection) throw new HostError('disposed')
    const temporary = path.join(path.dirname(selected), '.' + randomUUID() + '.ddp-tmp')
    const file = await open(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600)
    try {
      await file.writeFile(response.bytes); await file.sync(); await file.close()
      if (existing) {
        const current = await lstat(selected)
        if (current.isSymbolicLink() || current.dev !== existing.dev || current.ino !== existing.ino
            || current.size !== existing.size || current.mtimeMs !== existing.mtimeMs) throw new HostError('export_target_changed')
        await rename(temporary, selected)
      } else {
        // Atomic create-if-absent: do not overwrite a file created after the save dialog.
        await link(temporary, selected)
      }
      return { saved: true }
    } finally { await file.close().catch(() => {}); await unlink(temporary).catch(() => {}) }
  }
  async stopWorkspace(workspaceId) {
    await Promise.all([...this.#entries.values()].filter(entry => entry.workspaceId === workspaceId)
      .map(entry => this.disconnect({ connectionId: entry.connectionId })))
  }
  async suspend() {
    this.resumeIds = [...this.#entries.values()].filter(entry => entry.handle).map(entry => entry.connectionId)
    await Promise.all(this.resumeIds.map(connectionId => this.disconnect({ connectionId })))
  }
  async resume() { const ids = this.resumeIds ?? []; this.resumeIds = []; await Promise.allSettled(ids.map(connectionId => this.wake({ connectionId }))) }
  async close() {
    if (this.#closed) return
    this.#closing = true
    this.clearSubscriptions()
    await Promise.all([...this.#entries.values()].map(entry => this.disconnect({ connectionId: entry.connectionId })))
    await this.#save
    this.#closed = true
    this.store.close()
  }
}
