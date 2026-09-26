import { createHash, randomBytes, randomUUID, createPublicKey, verify as edVerify } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, open, rename, unlink, link } from 'node:fs/promises'
import path from 'node:path'
import { HostError } from './policy.mjs'
import { secureDirectory, secureFile } from './platform.mjs'
import { uploadRemoteCompute } from './file-transfer.mjs'
import { sourceArguments } from './source-policy.mjs'
import { ConnectionRegistry, ConnectionFault, SqliteProjectionStore, HttpProvider, OperationFault } from './shared-client.mjs'
const copy = value => structuredClone(value)
const connectionId = (environment, profile) => 'connection-' + createHash('sha256')
  .update(JSON.stringify([environment.environmentId, profile.profileId])).digest('hex').slice(0, 40)
const profileId = actor => 'profile-' + createHash('sha256').update(JSON.stringify(actor)).digest('hex').slice(0, 40)
const binding = entry => JSON.stringify([entry.environment.workspaceId, entry.environment.authorityNodeId,
  entry.profile.issuer, entry.profile.subject])
const emptyView = () => ({ transport: 'disconnected', snapshot: 'loading', reason: null, projection: null })
const LIMIT = 32 * 1024 * 1024
const CENTER_FEATURES = Object.freeze(['resources', 'documents', 'search', 'wiki', 'federation_tasks'])
const PROXY_BODY_LIMIT = 64 * 1024 * 1024
const PROXY_SAFE_HEADERS = new Set(['content-type', 'content-length', 'content-disposition',
  'cache-control', 'etag', 'last-modified'])
const STRIP_REQUEST_HEADERS = new Set(['authorization', 'cookie', 'origin', 'referer', 'host',
  'connection', 'keep-alive', 'transfer-encoding', 'upgrade'])

/** What the native approval dialog shows, derived from the runtime's stored scope only. */
export function approvalSummary(plan, phase) {
  const scope = plan?.scope ?? {}, list = value => Array.isArray(value) ? value : []
  const center = scope.center_execution, graph = center?.plan ?? scope.plan ?? {}
  return {
    planId: plan.plan_id, phase, scopeDigest: plan.scope_digest,
    description: typeof scope.task_spec?.query === 'string' ? scope.task_spec.query : '',
    payloads: list(scope.payload_bindings).filter(item => item?.phase === phase)
      .map(item => ({ kind: item.payload_kind, recipient: item.recipient_node_id, bytes: item.size_bytes, digest: item.digest })),
    transports: list(scope.transport_bindings).map(item => ({ recipient: item.recipient_node_id, endpoint: item.endpoint,
      workspace: item.workspace_id, subject: item.subject })),
    inputs: list(scope.input_manifest).length, retention: scope.retention, outputLocations: list(scope.output_locations),
    validUntil: graph.valid_until ?? null,
    exploration: phase === 'exploration' ? { recipients: list(scope.exploration?.allowed_recipients),
      payloads: list(scope.exploration?.allowed_payload), budget: scope.exploration?.budget } : null,
    centerExecution: center ? { rootTaskId: center.root_task_id, parentPlanId: center.parent_plan_id,
      planDigest: graph.plan_digest, steps: list(graph.steps), dataEdges: list(graph.data_edges), budget: graph.budget,
      finalResultWriter: graph.final_result_writer } : null,
  }
}

export function clientFailure(error) {
  const known = error instanceof HostError || error instanceof ConnectionFault || error instanceof OperationFault
  const local = ['draft_conflict', 'idempotency_conflict'].includes(error?.message)
  return { ok: false, error: { code: known ? error.code : local ? error.message : 'host_operation_failed' } }
}

/** One host-owned reference per connection, independent of renderer subscriptions/lifetime. */
export class ClientHost {
  #entries = new Map()
  #listeners = new Map()
  #fileTransfers = new Map()
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
      if (error.code === 'ENOENT') {
        await this.#loadActiveSource().catch(() => {})
        return this
      }
      this.store.close()
      throw new HostError('client_configuration_invalid')
    }
    try {
      const entries = JSON.parse(await file.readFile('utf8'))
      if (!Array.isArray(entries) || entries.length > 64) throw new Error('invalid')
      for (const metadata of entries) {
        if (!['local', 'remote'].includes(metadata.kind)) throw new Error('invalid')
        if (metadata.kind === 'remote') {
          // Same normalization the removed clientPairRemote IPC applied: an
          // absent/empty uploadOrigin defaults to the endpoint origin, and the
          // endpoint is stored without a trailing slash. Host-internal only.
          const endpoint = new URL(metadata.environment.endpoint)
          const upload = metadata.uploadOrigin === undefined || metadata.uploadOrigin === ''
            ? endpoint.origin : new URL(metadata.uploadOrigin).origin
          metadata.environment = { ...metadata.environment, endpoint: endpoint.href.replace(/\/$/, '') }
          metadata.uploadOrigin = upload
        }
        else {
          if (typeof metadata.directory !== 'string') throw new Error('invalid')
          // Entries persisted before the workspace kind existed were native by construction.
          metadata.workspaceKind = metadata.workspaceKind ?? 'native'
          if (!['native', 'wsl'].includes(metadata.workspaceKind)) throw new Error('invalid')
          // A native handle must stay an absolute host directory. A WSL handle is a
          // virtual path inside the distribution (~/...), validated by selectedWsl.
          if (metadata.workspaceKind === 'native' && !path.isAbsolute(metadata.directory)) throw new Error('invalid')
          if (metadata.contentFeatures !== undefined
            && (!Array.isArray(metadata.contentFeatures) || !metadata.contentFeatures.length
              || !metadata.contentFeatures.every(item => CENTER_FEATURES.includes(item)))) {
            throw new Error('invalid')
          }
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
      await this.#loadActiveSource()
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
  // -- Source registry: the single active data source for the /api proxy ----
  // sourceId IS the connectionId verbatim. Active source persists in userData
  // (active-source.json) so a restart restores it. After a successful
  // sourceActivate/workspaceOpen/centerConnect the RENDERER reloads the page;
  // the host only records the active source.
  #sourceListeners = new Map()
  #activeSourceId = null
  #sourceLoaded = false
  #sourceConnecting = new Set()
  #objects = new Map()
  #objectSequence = 0
  #activeSourceFile() { return path.join(this.directory, 'active-source.json') }
  async #loadActiveSource() {
    if (this.#sourceLoaded) return
    this.#sourceLoaded = true
    try {
      await secureFile(this.#activeSourceFile(), { platform: this.platform, maxBytes: 4096,
        code: 'client_configuration_invalid' })
    } catch (error) {
      // Missing file = first run (no active source yet). An unsafe file is a
      // real integrity failure and surfaces; anything else reads as no source.
      if (error.code === 'ENOENT') return
      if (error instanceof HostError) throw error
      return
    }
    let file
    try {
      file = await open(this.#activeSourceFile(), constants.O_RDONLY | constants.O_NOFOLLOW)
      const value = JSON.parse(await file.readFile('utf8'))
      if (typeof value?.sourceId === 'string') this.#activeSourceId = value.sourceId
    } catch { this.#activeSourceId = null }
    finally { await file?.close() }
    if (this.#activeSourceId && !this.#entries.has(this.#activeSourceId)) this.#activeSourceId = null
  }
  async #saveActiveSource() {
    const data = JSON.stringify({ sourceId: this.#activeSourceId })
    const temporary = path.join(this.directory, randomUUID() + '.tmp')
    const file = await open(temporary, constants.O_CREAT | constants.O_WRONLY | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    try { await file.writeFile(data); await file.sync() } finally { await file.close() }
    try { await rename(temporary, this.#activeSourceFile()) }
    finally { await unlink(temporary).catch(() => {}) }
  }
  #sourceSummary(entry) {
    const features = entry.kind === 'local' ? this.#contentFeatures(entry) : [...CENTER_FEATURES]
    const state = this.#sourceConnecting.has(entry.connectionId) ? 'connecting'
      : entry.view.transport === 'ready' && entry.view.snapshot === 'current' ? 'ready'
      : entry.view.transport === 'blocked' && entry.view.reason === 'authentication_required' ? 'signed_out'
      : 'unavailable'
    // reason is a source_error contract code or null — never a transport reason.
    return { sourceId: entry.connectionId,
      kind: entry.kind === 'local' ? 'local' : 'center',
      label: entry.label, state, readOnly: entry.kind !== 'local', features,
      active: entry.connectionId === this.#activeSourceId,
      reason: state === 'signed_out' ? 'source_signed_out' : null,
      environment: { environmentId: entry.environment.environmentId,
        workspaceId: entry.environment.workspaceId, authorityNodeId: entry.environment.authorityNodeId },
      profile: copy(entry.profile) }
  }
  #contentFeatures(entry) {
    // Cached from the owned runtime's GET /api/v1/capabilities at connect time
    // (LocalContent owns the content_features field). Before first contact, or
    // when the runtime predates the field, fall back to the full set — the
    // proxy forwards everything regardless, so this only drives nav filtering.
    if (Array.isArray(entry.contentFeatures) && entry.contentFeatures.length
        && entry.contentFeatures.every(item => CENTER_FEATURES.includes(item))) {
      return [...entry.contentFeatures]
    }
    return [...CENTER_FEATURES]
  }
  async #refreshContentFeatures(entry) {
    if (entry.kind !== 'local' || !entry.workspaceId) return
    let current
    try { current = this.runtime.connection(entry.workspaceId) }
    catch { return }
    try {
      const response = await (this.centerFetch ?? fetch)(current.url + '/api/v1/capabilities', {
        headers: { Accept: 'application/json', Authorization: 'Bearer ' + current.token },
        redirect: 'error', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(5000) })
      if (!response.ok) { await response.body?.cancel().catch(() => {}); return }
      const value = await response.json()
      if (Array.isArray(value?.content_features) && value.content_features.length
          && value.content_features.every(item => CENTER_FEATURES.includes(item))) {
        entry.contentFeatures = [...value.content_features]
      }
    } catch { /* keep the fallback; nav filtering stays permissive */ }
  }
  #emitSources() {
    const summaries = this.sourceListSync()
    for (const [subscriptionId, send] of this.#sourceListeners) {
      try { send(summaries) } catch { this.#sourceListeners.delete(subscriptionId) }
    }
  }
  sourceListSync() { return [...this.#entries.values()].map(entry => this.#sourceSummary(entry)) }
  async sourceList() { await this.#loadActiveSource(); return this.sourceListSync() }
  onSourceChange(send) {
    const subscriptionId = randomUUID()
    this.#sourceListeners.set(subscriptionId, send)
    return () => { this.#sourceListeners.delete(subscriptionId) }
  }
  clearSourceListeners() { this.#sourceListeners.clear() }
  activeSourceId() { return this.#activeSourceId }
  async sourceActivate({ sourceId }) {
    await this.#loadActiveSource()
    const entry = this.#entry(sourceId)
    this.#sourceConnecting.add(sourceId)
    this.#emitSources()
    try {
      if (entry.kind === 'local') {
        if (!entry.workspaceId) throw new HostError('workspace_unavailable')
        await this.connectLocal({ workspaceId: entry.workspaceId })
      } else {
        await this.wake({ connectionId: sourceId })
      }
      // wake/attach only restart the connection loop; stay `connecting` until the
      // snapshot is current or the loop gives up, so the renderer never reads a
      // transient `unavailable` as a failed switch. Bounded: a hung center ends as
      // the honest `unavailable`.
      const current = this.#entry(sourceId), deadline = Date.now() + 20000
      while (!this.#closing && Date.now() < deadline
          && !(current.view.transport === 'ready' && current.view.snapshot === 'current')
          && !['blocked', 'backoff'].includes(current.view.transport)) {
        await new Promise(resolve => setTimeout(resolve, 50))
      }
    } finally { this.#sourceConnecting.delete(sourceId) }
    this.#activeSourceId = sourceId
    await this.#saveActiveSource()
    this.#emitSources()
    return this.#sourceSummary(this.#entry(sourceId))
  }
  // Startup: reconnect the persisted active source. Failure leaves it `unavailable`
  // (still active, so the sources page can offer to reopen it); never throws.
  async restoreActiveSource() {
    try {
      await this.#loadActiveSource()
      if (this.#activeSourceId && this.#entries.has(this.#activeSourceId)) {
        await this.sourceActivate({ sourceId: this.#activeSourceId })
      }
    } catch { this.#emitSources() }
  }
  async sourceRemove({ sourceId }) {
    await this.#loadActiveSource()
    const entry = this.#entry(sourceId)
    // Disconnect first (aborts in-flight file transfers for this connection).
    await this.disconnect({ connectionId: sourceId }).catch(error => {
      if (!(error instanceof HostError && error.code === 'host_closing')) throw error
    })
    // Clearing the stored center JWT is part of removal; local workspace data
    // on disk is never touched (T65).
    if (entry.kind === 'remote') {
      await this.credentials.clear({
        environmentId: entry.environment.environmentId, profileId: entry.profile.profileId,
      }).catch(() => {})
    }
    await this.registry.remove(entry.environment, entry.profile)
    this.#entries.delete(sourceId)
    for (const [id, record] of this.#objects) {
      if (record.sourceId === sourceId) { this.#objects.delete(id); record.revoke?.() }
    }
    if (this.#activeSourceId === sourceId) {
      this.#activeSourceId = null
      await this.#saveActiveSource()
    }
    await this.#persist()
    this.#emitSources()
    return null
  }
  // workspaceOpen: native directory dialog → start runtime → connect → activate.
  // Returns null when the user cancels. Never exposes the directory path or token.
  async workspaceOpen() {
    if (this.#closing || this.#closed) throw new HostError('host_closing')
    if (typeof this.selectWorkspace !== 'function') throw new HostError('workspace_unavailable')
    const selected = await this.selectWorkspace()
    if (!selected) return null
    const summary = await this.connectLocal({ workspaceId: selected.workspaceId })
    return this.sourceActivate({ sourceId: summary.connectionId })
  }
  // centerConnect: validate endpoint → node challenge proof → login → handshake
  // → register → store JWT → activate. The password is used once for the login
  // POST and never stored, logged or returned.
  async centerConnect(input, options = {}) {
    if (this.#closing || this.#closed) throw new HostError('host_closing')
    const { endpoint, username, password, persist, storageOrigin } =
      sourceArguments('centerConnect', input, options)
    const origin = new URL(endpoint).origin
    const nonce = randomBytes(24).toString('base64url')
    const node = await this.#centerJson(origin + '/api/v1/federation/node?challenge='
      + encodeURIComponent(nonce), { timeoutMs: 15000 })
    const verified = await this.#verifyNodeProof(node, nonce, endpoint)
    const login = await this.#centerJson(endpoint + '/api/auth/login', { timeoutMs: 15000,
      method: 'POST', body: { username, password } })
    const token = typeof login?.access_token === 'string' && login.access_token.length >= 16
      ? login.access_token : null
    if (!token) throw new HostError('authentication_required')
    const shake = await this.#centerJson(endpoint + '/api/v1/client/handshake', { timeoutMs: 15000,
      token })
    if (shake?.protocol_version !== 'ddp-client/1' || !shake?.identity?.environment_id
        || !shake?.identity?.workspace_id || !shake?.profile?.issuer || !shake?.profile?.subject) {
      throw new HostError('protocol_incompatible')
    }
    if (shake.identity.environment_id !== verified.nodeId || shake.identity.authority_node_id !== verified.nodeId) {
      throw new HostError('identity_mismatch')
    }
    const environment = { environmentId: verified.nodeId, workspaceId: shake.identity.workspace_id,
      authorityNodeId: verified.nodeId, endpoint }
    const actor = { issuer: shake.profile.issuer, subject: shake.profile.subject }
    const digest = createHash('sha256')
      .update(JSON.stringify([verified.nodeId, actor.issuer, actor.subject])).digest('hex').slice(0, 32)
    const profile = { profileId: 'profile-' + digest, ...actor }
    const label = new URL(endpoint).host
    await this.credentials.set({ environmentId: environment.environmentId,
      profileId: profile.profileId, secret: token, persist })
    let summary
    try {
      summary = await this.pairRemote({ environment, profile, label,
        ...(storageOrigin === undefined ? {} : { uploadOrigin: storageOrigin }) })
    } catch (error) {
      await this.credentials.clear({ environmentId: environment.environmentId,
        profileId: profile.profileId }).catch(() => {})
      throw error
    }
    return this.sourceActivate({ sourceId: summary.connectionId })
  }
  async #centerJson(url, { method = 'GET', body, token, timeoutMs = 15000 } = {}) {
    const encoded = body === undefined ? undefined : JSON.stringify(body)
    let response
    try {
      response = await (this.centerFetch ?? fetch)(url, { method,
        headers: { Accept: 'application/json',
          ...(encoded === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(token ? { Authorization: 'Bearer ' + token } : {}) },
        body: encoded, redirect: 'error', credentials: 'omit', cache: 'no-store',
        signal: AbortSignal.timeout(timeoutMs) })
    } catch { throw new HostError('connection_failed') }
    let value = null
    try { value = await response.json() } catch { throw new HostError('protocol_incompatible') }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new HostError('authentication_required')
      if (response.status === 404) throw new HostError('not_found')
      throw new HostError('connection_failed')
    }
    return value
  }
  async #verifyNodeProof(node, nonce, endpoint) {
    // Same proof rules the shared HttpProvider enforces for pairing: node-bound
    // id, matching nonce/endpoint, fresh timestamps, valid signature.
    try {
      const nodeId = node?.authority_node_id
      const publicKey = Buffer.from(node?.public_key ?? '', 'base64')
      const proof = node?.proof
      if (typeof nodeId !== 'string' || publicKey.length !== 32 || !proof || typeof proof !== 'object') {
        throw new HostError('identity_mismatch')
      }
      const hash = createHash('sha256').update(publicKey).digest('hex')
      if (nodeId !== 'node-' + hash.slice(0, 48) || proof.node_id !== nodeId
          || proof.schema !== 'ddp-node-proof/1' || proof.nonce !== nonce
          || String(proof.endpoint).replace(/\/$/, '') !== String(endpoint).replace(/\/$/, '')) {
        throw new HostError('identity_mismatch')
      }
      const issued = Date.parse(proof.issued_at), expires = Date.parse(proof.expires_at), now = Date.now()
      if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now + 5000
          || expires <= now || expires - issued > 60000 || expires <= issued || now - issued > 65000) {
        throw new HostError('identity_mismatch')
      }
      const signed = Buffer.from(JSON.stringify([proof.schema, proof.nonce, proof.node_id,
        proof.endpoint, proof.issued_at, proof.expires_at]))
      const signature = Buffer.from(proof.signature, 'base64url')
      const key = createPublicKey({ key: Buffer.concat(
        [Buffer.from('302a300506032b6570032100', 'hex'), publicKey]), format: 'der', type: 'spki' })
      if (signature.length !== 64 || !edVerify(null, signed, key, signature)) throw new HostError('identity_mismatch')
      return { nodeId }
    } catch (error) {
      if (error instanceof HostError) throw error
      throw new HostError('identity_mismatch')
    }
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
    this.#emitSources()
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
      ...(entry.kind === 'local' ? { directory: entry.directory, workspaceKind: entry.workspaceKind ?? 'native',
        ...(entry.contentFeatures ? { contentFeatures: entry.contentFeatures } : {}) }
        : { uploadOrigin: entry.uploadOrigin }) })))
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
  /**
   * The credential for a host-bound paired center. Only fixed native reads and the
   * owned runtime's approved dispatch/reconcile/ack operations may consume it;
   * the renderer, drafts, receipts and connection metadata never receive it.
   */
  async #planCenter(binding) {
    const remote = [...this.#entries.values()].find(item => item.kind === 'remote'
      && item.environment.environmentId === binding.environmentId && item.profile.profileId === binding.profileId)
    if (!remote) throw new OperationFault('center_not_paired')
    if (remote.environment.authorityNodeId !== binding.recipientNodeId || remote.environment.workspaceId !== binding.workspaceId
        || remote.profile.issuer !== binding.issuer || remote.profile.subject !== binding.subject
        || remote.environment.endpoint !== binding.endpoint) throw new OperationFault('center_identity_changed')
    // Ready means this generation proved the node's Ed25519 identity at this endpoint.
    if (this.#closing || !remote.handle || remote.view.transport !== 'ready') throw new OperationFault('center_not_current')
    const credential = await this.credentials.withCredential({ environmentId: remote.environment.environmentId,
      profileId: remote.profile.profileId }, secret => secret).catch(() => { throw new OperationFault('authentication_required') })
    return { endpoint: remote.environment.endpoint, credential }
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
      planCenter: binding => this.#planCenter(binding),
      hashLocalDelivery: (environment, planId, signal) => this.#hashLocalDelivery(entry, environment, planId, signal),
    })
    // HttpProvider verifies the Ed25519 node challenge before asking for a credential.
    // Its transport is https-only; `loopbackCenters` (unpackaged builds only, set by
    // main.mjs) admits the same http://127.0.0.1 escape hatch the source policy allows.
    return new HttpProvider({ ownedLoopback: this.loopbackCenters === true,
      credential: async () => this.credentials.withCredential({
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
    if (!entry.pending) entry.pending = (async () => { await this.#attach(entry, this.#provider(entry)); await this.#refreshContentFeatures(entry); await this.#persist() })()
      .finally(() => { entry.pending = null })
    await entry.pending
    return this.#summary(entry)
  }
  // Host-internal center registration (used only by centerConnect, which passes
  // already-validated metadata derived from the node proof + login handshake).
  // Not reachable from the renderer: no IPC channel, policy entry, preload
  // method or bridge type names it.
  async pairRemote(metadata) {
    if (this.#closing || this.#closed) throw new HostError('host_closing')
    const entry = this.#register({ ...metadata, kind: 'remote' })
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
    const transfers = [...this.#fileTransfers.values()].filter(item => item.connections.has(connectionId))
    for (const transfer of transfers) transfer.controller.abort()
    await entry.pending
    entry.unsubscribe?.(); entry.unsubscribe = null
    await entry.handle?.release(); entry.handle = null
    await Promise.allSettled(transfers.map(item => item.promise))
    this.#changed(entry, { ...entry.view, transport: 'disconnected', snapshot: entry.view.projection ? 'stale' : 'loading' })
    return this.#summary(entry)
  }
  #connection(entry) {
    if (this.#closing) throw new HostError('host_closing')
    if (!entry.handle || entry.view.transport !== 'ready' || entry.view.snapshot !== 'current') throw new HostError('connection_not_current')
    return entry.handle.connection
  }
  // Named reads stay on the local workspace only. A center is read through the
  // GET-only /api proxy (with its JWT), never through this connection query
  // path; refusing here keeps remote reads on the audited proxy with its
  // X-DDP-Source fencing and signed_out transitions.
  async query({ connectionId, name, payload }) {
    const entry = this.#entry(connectionId)
    if (entry.kind !== 'local') throw new HostError('approved_plan_required')
    return this.#connection(entry).query(name, payload)
  }
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
    const connection = this.#connection(entry)
    const local = entry.kind === 'local'
    let current
    if (local) current = this.#local(entry).current
    else {
      if ((options.method && options.method !== 'GET')
          || !/^\/api\/v1\/versions\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\/(?:source|bundle)$/.test(route))
        throw new HostError('approved_plan_required')
      const target = await this.#planCenter({
        recipientNodeId: entry.environment.authorityNodeId, environmentId: entry.environment.environmentId,
        workspaceId: entry.environment.workspaceId, profileId: entry.profile.profileId,
        issuer: entry.profile.issuer, subject: entry.profile.subject, endpoint: entry.environment.endpoint,
      })
      if (this.#connection(entry) !== connection) throw new HostError('disposed')
      current = { url: target.endpoint, token: target.credential }
      route = route.replace('/api/v1/versions/', '/api/v1/client/versions/')
    }
    const response = await fetch(current.url + route, { ...options, redirect: 'error', credentials: 'omit', cache: 'no-store',
      signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
      headers: { Authorization: 'Bearer ' + current.token, ...options.headers } })
    if (!response.ok) {
      await response.body?.cancel()
      throw new HostError(response.status === 401 || response.status === 403 ? 'authentication_required'
        : response.status === 404 ? 'not_found' : response.status === 410 ? 'source_unavailable' : 'file_operation_failed')
    }
    if (!local && (response.headers.get('X-DDP-Authority-Node') !== entry.environment.authorityNodeId
        || response.headers.get('X-DDP-Actor-Subject') !== entry.profile.subject)) {
      await response.body?.cancel()
      throw new HostError('identity_mismatch')
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
    if (this.#connection(entry) !== connection
        || (local ? this.runtime.connection(entry.workspaceId).url : entry.environment.endpoint) !== current.url)
      throw new HostError('disposed')
    return { bytes: Buffer.concat(chunks), contentType: response.headers.get('content-type') ?? '',
      sourceDigest: response.headers.get('X-DDP-Source-Digest') }
  }
  // -- Host /api proxy (ddp://app/api/** → active source) ----
  // Local: any method + process token, streaming (SSE flows incrementally).
  // Center: GET/HEAD only + login JWT; anything else is 403 with ZERO network
  // I/O. The renderer never receives any token: Authorization is stripped from
  // the renderer request and set from the host-held credential.
  async apiProxy({ sourceId, method, path, query = '', headers = {}, body } = {}) {
    await this.#loadActiveSource()
    if (!this.#activeSourceId) throw new HostError('no_active_source')
    const activeId = this.#activeSourceId
    const entry = this.#entries.get(sourceId ?? activeId) ?? this.#entry(sourceId ?? activeId)
    // A request naming a non-active source, or one that arrives after a switch,
    // is discarded so in-flight responses cannot leak across sources.
    if (entry.connectionId !== activeId) throw new HostError('source_changed')
    const normalized = this.#proxyPath(path)
    const upper = String(method ?? 'GET').toUpperCase()
    if (!['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'].includes(upper)) {
      throw new HostError('invalid_arguments')
    }
    if (entry.kind !== 'local') {
      if (upper !== 'GET' && upper !== 'HEAD') throw new HostError('approved_plan_required')
    } else if (localPrivate(normalized, upper)) {
      // The local runtime's private routes (plans, consents, models, client protocol)
      // stay behind the typed bridge: e.g. plan approval must go through the native
      // dialog, never a renderer POST to /api/v1/plans/{id}/approve.
      throw new HostError('not_supported_locally')
    }
    const outgoing = {}
    for (const [name, value] of Object.entries(headers ?? {})) {
      if (typeof value !== 'string' || STRIP_REQUEST_HEADERS.has(name.toLowerCase())) continue
      outgoing[name] = value
    }
    let upstream
    if (entry.kind === 'local') {
      if (!entry.workspaceId) throw new HostError('workspace_unavailable')
      let current
      try { current = this.runtime.connection(entry.workspaceId) }
      catch { throw new HostError('source_unavailable') }
      upstream = { url: current.url, token: current.token }
    } else {
      let credential
      try {
        credential = await this.credentials.withCredential({
          environmentId: entry.environment.environmentId, profileId: entry.profile.profileId,
        }, secret => secret)
      } catch { throw new HostError('source_signed_out') }
      upstream = { url: entry.environment.endpoint, token: credential }
    }
    const target = upstream.url + normalized + (query ? '?' + String(query).replace(/^\?/, '') : '')
    if (body !== undefined && body !== null) {
      const size = typeof body === 'string' ? Buffer.byteLength(body)
        : body?.byteLength ?? body?.length ?? 0
      if (size > PROXY_BODY_LIMIT) throw new HostError('input_too_large')
    }
    let response
    try {
      response = await (this.centerFetch ?? fetch)(target, { method: upper,
        headers: { ...outgoing, Authorization: 'Bearer ' + upstream.token },
        body: body ?? undefined, redirect: 'error', credentials: 'omit', cache: 'no-store',
        signal: AbortSignal.timeout(120000) })
    } catch (error) {
      if (error instanceof HostError) throw error
      throw new HostError('source_unavailable')
    }
    if (this.#activeSourceId !== activeId || !this.#entries.has(activeId)) {
      await response.body?.cancel().catch(() => {})
      throw new HostError('source_changed')
    }
    if (response.status === 401 && entry.kind !== 'local') {
      await response.body?.cancel().catch(() => {})
      this.#changed(entry, { ...entry.view, transport: 'blocked', snapshot: entry.view.projection ? 'stale' : 'failed',
        reason: 'authentication_required' })
      this.#emitSources()
      throw new HostError('source_signed_out')
    }
    const safeHeaders = { 'X-DDP-Source': activeId }
    for (const [name, value] of response.headers) {
      const lower = name.toLowerCase()
      if (PROXY_SAFE_HEADERS.has(lower) || lower === 'content-range' || lower === 'accept-ranges'
          || lower.startsWith('x-ddp-')) safeHeaders[name] = value
    }
    // Center absolute object URLs (same origin as the center endpoint or the
    // registered storage origin) are rewritten to opaque _object ids. The page
    // never sees the presigned URL.
    const rewriteOrigins = entry.kind === 'local' ? []
      : [new URL(entry.environment.endpoint).origin,
        ...(entry.uploadOrigin ? [new URL(entry.uploadOrigin).origin] : [])]
    return { status: response.status, headers: safeHeaders, sourceId: activeId,
      body: response.body, rewriteOrigins }
  }
  #proxyPath(path) {
    if (typeof path !== 'string' || !path.startsWith('/api/') || path.includes('\0')
        || path.includes('\\') || /\/\.(?:\/|$)/.test(path.split('?')[0])) {
      throw new HostError('invalid_arguments')
    }
    const [clean] = path.split('?')
    if (clean.length > 4096) throw new HostError('invalid_arguments')
    // Upstream servers percent-decode the path before routing; decide on what they will see.
    let decoded
    try { decoded = decodeURIComponent(clean) } catch { throw new HostError('invalid_arguments') }
    if (decoded !== clean && (/[\\\0?#]/.test(decoded) || decoded.split('/').length !== clean.split('/').length
        || /\/\.(?:\/|$)/.test(decoded))) throw new HostError('invalid_arguments')
    return clean
  }
  // Rewrite one absolute center object URL to an opaque short-lived id bound to
  // this source. Returns null when the URL is not an allowed center origin.
  rewriteObjectUrl(sourceId, url) {
    const entry = this.#entry(sourceId)
    if (entry.kind === 'local') return null
    let parsed
    try { parsed = new URL(String(url)) } catch { return null }
    const allowed = [new URL(entry.environment.endpoint).origin,
      ...(entry.uploadOrigin ? [new URL(entry.uploadOrigin).origin] : [])]
    if (!allowed.includes(parsed.origin)) return null
    return this.#registerObject(entry, String(url))
  }
  #registerObject(entry, url) {
    const id = 'obj-' + (entry.connectionId.slice(-8) + '-' + (++this.#objectSequence).toString(36)
      + '-' + Math.random().toString(36).slice(2, 10))
    this.#objects.set(id, { sourceId: entry.connectionId, url,
      origin: new URL(url).origin, createdAt: Date.now() })
    if (this.#objects.size > 256) {
      const oldest = [...this.#objects.keys()][0]
      this.#objects.delete(oldest)
    }
    return id
  }
  // Fetch an opaque object: streams bytes from the allowed origin with NO
  // Authorization header (presigned storage URLs are self-authenticating).
  async fetchObject(id) {
    const record = this.#objects.get(id)
    if (!record || Date.now() - record.createdAt > 10 * 60 * 1000) {
      this.#objects.delete(id)
      throw new HostError('not_found')
    }
    if (record.sourceId !== this.#activeSourceId) throw new HostError('source_changed')
    const entry = this.#entries.get(record.sourceId)
    if (!entry) throw new HostError('unknown_connection')
    const allowed = [new URL(entry.environment.endpoint).origin,
      ...(entry.uploadOrigin ? [new URL(entry.uploadOrigin).origin] : [])]
    if (!allowed.includes(record.origin)) throw new HostError('identity_mismatch')
    let response
    try {
      response = await (this.centerFetch ?? fetch)(record.url, { method: 'GET',
        redirect: 'error', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(120000) })
    } catch { throw new HostError('source_unavailable') }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {})
      throw new HostError(response.status === 401 || response.status === 403 ? 'source_signed_out'
        : response.status === 404 ? 'not_found' : 'source_unavailable')
    }
    const safeHeaders = { 'X-DDP-Source': record.sourceId }
    for (const [name, value] of response.headers) {
      const lower = name.toLowerCase()
      if (PROXY_SAFE_HEADERS.has(lower) || lower.startsWith('x-ddp-')) safeHeaders[name] = value
    }
    return { status: response.status, headers: safeHeaders, sourceId: record.sourceId, body: response.body }
  }
  async #hashLocalDelivery(entry, environment, planId, signal) {
    const { connection, current } = this.#local(entry)
    if (environment.endpoint !== current.url) throw new HostError('disposed')
    const response = await fetch(current.url + '/api/v1/plans/' + encodeURIComponent(planId) + '/delivery/result', {
      headers: { Authorization: 'Bearer ' + current.token }, redirect: 'error', credentials: 'omit', cache: 'no-store',
      signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
    })
    if (!response.ok || !response.body || !['application/json', 'application/zip'].includes(
      (response.headers.get('content-type') ?? '').split(';', 1)[0].trim())) {
      await response.body?.cancel()
      throw new HostError('delivery_unverified')
    }
    const reader = response.body.getReader(), hash = createHash('sha256')
    let size = 0
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        size += value.length
        if (size > 64 * 1024 * 1024) throw new HostError('delivery_too_large')
        hash.update(value)
      }
    } finally { await reader.cancel() }
    if (this.#connection(entry) !== connection || this.runtime.connection(entry.workspaceId).url !== current.url)
      throw new HostError('disposed')
    return 'sha256:' + hash.digest('hex')
  }
  // -- Remote plan flow: every step goes through the owned local runtime's ledger ----
  #planConnection(connectionId) { return this.#local(this.#entry(connectionId)).connection }
  async planPropose(input) { return this.#propose(input, 'plan.propose', { query: input.query }) }
  async planProposeFile(input) { return this.#propose(input, 'plan.propose-file', { filename: input.filename }) }
  async #propose({ connectionId, centerConnectionId, inputs, retention, validMinutes, idempotencyKey,
    template = 'center_only', participantConnectionIds = [], purpose = 'answer', wiki }, operation, content) {
    const connection = this.#planConnection(connectionId)
    const remote = this.#entry(centerConnectionId)
    if (remote.kind !== 'remote') throw new HostError('center_not_paired')
    if (!remote.handle || remote.view.transport !== 'ready') throw new HostError('center_not_current')
    if (operation === 'plan.propose') {
      if (!['center_only', 'trusted_federation'].includes(template) ||
          (template === 'center_only' && participantConnectionIds.length)) throw new HostError('invalid_arguments')
      const recipients = new Set([remote.environment.authorityNodeId])
      for (const id of participantConnectionIds) {
        const participant = this.#entry(id)
        if (participant.kind !== 'remote') throw new HostError('center_not_paired')
        if (!participant.handle || participant.view.transport !== 'ready') throw new HostError('center_not_current')
        recipients.add(participant.environment.authorityNodeId)
      }
      content = { ...content, template, recipients: [...recipients].sort() }
      // Typed intent: purpose/wiki cross exactly as named; illegal shapes fail
      // here as well as in client-policy and the local HTTP template.
      if (!['answer', 'wiki'].includes(purpose)) throw new HostError('invalid_arguments')
      if (purpose === 'wiki') {
        if (!wiki || typeof wiki !== 'object' || Array.isArray(wiki)) throw new HostError('invalid_arguments')
        const keys = Object.keys(wiki)
        if (!keys.includes('title') || keys.some(key => !['title', 'max_pages'].includes(key))
            || typeof wiki.title !== 'string' || !wiki.title.trim() || wiki.title.length > 255
            || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(wiki.title)
            || (wiki.max_pages !== undefined && (!Number.isInteger(wiki.max_pages) || wiki.max_pages < 1 || wiki.max_pages > 12)))
          throw new HostError('invalid_arguments')
        content = { ...content, purpose, wiki: { title: wiki.title,
          ...(wiki.max_pages !== undefined ? { max_pages: wiki.max_pages } : {}) } }
      } else {
        if (wiki !== undefined) throw new HostError('invalid_arguments')
        content = { ...content, purpose }
      }
    }
    // The recipient comes from host-held pairing metadata, never from the renderer.
    const center = { recipient_node_id: remote.environment.authorityNodeId, environment_id: remote.environment.environmentId,
      workspace_id: remote.environment.workspaceId, profile_id: remote.profile.profileId, issuer: remote.profile.issuer,
      subject: remote.profile.subject, endpoint: remote.environment.endpoint,
      ...(operation === 'plan.propose-file' ? { upload_endpoint: remote.uploadOrigin } : {}) }
    return connection.execute(operation, { center, ...content, retention, valid_seconds: validMinutes * 60,
      inputs: inputs.map(item => ({ ref: item.ref, digest: item.digest, size_bytes: item.sizeBytes })) }, idempotencyKey)
  }
  async planList({ connectionId }) { return this.#planConnection(connectionId).query('plan.list', {}) }
  async planGet(input) { return this.#readPlan(input, 'plan.get') }
  async #readPlan({ connectionId, planId }, operation) {
    const entry = this.#entry(connectionId)
    const detail = await this.#local(entry).connection.query(operation, { plan_id: planId })
    const saved = await this.store.readDraft(entry.scope, '@file-transfer:' + planId)
    if (!saved) return detail
    const { phase, uploadedBytes, totalBytes } = saved.value
    if (!Number.isSafeInteger(uploadedBytes) || !Number.isSafeInteger(totalBytes) ||
        uploadedBytes < 0 || uploadedBytes > totalBytes || totalBytes < 1) throw new HostError('cache_failure')
    return { ...detail, transfer: { state: phase, uploadedBytes, totalBytes } }
  }
  async planApprove({ connectionId, planId, phase, scopeDigest, userConfirmed, idempotencyKey }) {
    const entry = this.#entry(connectionId), connection = this.#planConnection(connectionId)
    if (userConfirmed !== true) throw new HostError('approval_cancelled')
    const { plan } = await connection.query('plan.get', { plan_id: planId })
    // The renderer's click is not proof of a user action. The grant needs a native
    // dialog showing the stored scope, which renderer script cannot answer.
    if (plan.scope_digest !== scopeDigest) throw new HostError('plan_changed')
    if (typeof this.confirmApproval !== 'function') throw new HostError('approval_unavailable')
    if (await this.confirmApproval(approvalSummary(plan, phase)) !== true) throw new HostError('approval_cancelled')
    if (this.#connection(entry) !== connection) throw new HostError('disposed')
    return connection.execute('plan.approve', { plan_id: planId, phase, confirmed_scope_digest: scopeDigest,
      user_confirmed: true }, idempotencyKey)
  }
  async planReviewCenter({ connectionId, planId, idempotencyKey }) {
    return this.#planConnection(connectionId).execute('plan.review-center', { plan_id: planId }, idempotencyKey)
  }
  async planRevoke({ connectionId, planId, idempotencyKey }) {
    this.#fileTransfers.get(connectionId + '/' + planId)?.controller.abort()
    return this.#planConnection(connectionId).execute('plan.revoke', { plan_id: planId }, idempotencyKey)
  }
  async planCancel({ connectionId, planId, idempotencyKey }) {
    this.#fileTransfers.get(connectionId + '/' + planId)?.controller.abort()
    return this.#planConnection(connectionId).execute('plan.cancel', { plan_id: planId }, idempotencyKey)
  }
  async planDispatch({ connectionId, planId, phase, idempotencyKey }) {
    const entry = this.#entry(connectionId), connection = this.#planConnection(connectionId)
    const { plan } = await connection.query('plan.get', { plan_id: planId })
    if (phase !== 'execution' || plan.scope?.task_spec?.operation !== 'corpus.parse')
      return connection.execute('plan.dispatch', { plan_id: planId, phase }, idempotencyKey)
    const key = connectionId + '/' + planId
    if (this.#fileTransfers.has(key)) throw new HostError('transfer_in_progress')
    const binding = plan.scope.transport_bindings.find(item => item.transport_ref === 'center')
    const remote = [...this.#entries.values()].find(item => item.kind === 'remote' &&
      item.environment.environmentId === binding?.environment_id && item.profile.profileId === binding?.profile_id)
    if (!remote) throw new HostError('center_not_paired')
    const controller = new AbortController()
    const promise = this.#uploadFile(entry, remote, connection, plan, idempotencyKey, controller.signal)
      .finally(() => { this.#fileTransfers.delete(key) })
    this.#fileTransfers.set(key, { controller, promise, connections: new Set([connectionId, remote.connectionId]) })
    return promise
  }
  async planResume({ connectionId, planId, idempotencyKey }) {
    // Stages a fresh center ready revision; never approves it. The caller must
    // show, review and approve the staged revision before any dispatch.
    return this.#planConnection(connectionId).execute('plan.resume', { plan_id: planId }, idempotencyKey)
  }
  async planReconcile(input) { return this.#readPlan(input, 'plan.reconcile') }
  async planFetchDelivery(input) { return this.#readPlan(input, 'plan.delivery.fetch') }
  async planConfirmDelivery({ connectionId, planId, deliveryId, resultManifestDigest, idempotencyKey }) {
    return this.#planConnection(connectionId).execute('plan.delivery.confirm', { plan_id: planId, delivery_id: deliveryId,
      result_manifest_digest: resultManifestDigest }, idempotencyKey)
  }
  activeTransferCount() { return this.#fileTransfers.size }
  async #uploadFile(entry, remote, connection, plan, idempotencyKey, signal) {
    const authorized = await connection.execute('plan.dispatch', { plan_id: plan.plan_id, phase: 'execution' }, idempotencyKey)
    if (!['uploading', 'waiting_input'].includes(authorized.state)) return authorized
    const key = '@file-transfer:' + plan.plan_id
    const saved = await this.store.readDraft(entry.scope, key)
    let revision = saved?.revision ?? 0
    const journal = saved?.value ?? { scopeDigest: plan.scope_digest, createKey: randomUUID(), createAttempted: false,
      uploadId: null, remoteComputeId: null, phase: 'prepared', uploadedBytes: 0,
      totalBytes: plan.scope.input_manifest[0].size_bytes }
    if (journal.scopeDigest !== plan.scope_digest) throw new HostError('plan_changed')
    const checkpoint = async value => { revision = await this.store.saveDraft(entry.scope, key, revision, value) }
    if (!saved) await checkpoint(journal)
    const reviewed = plan.scope.transport_bindings.find(item => item.transport_ref === 'center')
    const binding = { recipientNodeId: reviewed.recipient_node_id, environmentId: reviewed.environment_id,
      workspaceId: reviewed.workspace_id, profileId: reviewed.profile_id, issuer: reviewed.issuer,
      subject: reviewed.subject, endpoint: reviewed.endpoint }
    const current = () => {
      signal.throwIfAborted()
      if (this.#connection(entry) !== connection) throw new HostError('disposed')
    }
    const transfer = await uploadRemoteCompute({ plan, journal, checkpoint, signal,
      authorize: async (action, uploadId, offset, length) => {
        current()
        const ticket = await connection.query('plan.file.authorize', { plan_id: plan.plan_id, action,
          upload_id: uploadId, offset, length, operation_key: randomUUID() })
        current()
        return ticket
      },
      readSource: async ref => {
        current()
        const source = await this.#request(entry, '/api/v1/versions/' + encodeURIComponent(ref) + '/source', { signal })
        if (!source.contentType.startsWith('application/pdf') || !source.bytes.subarray(0, 5).equals(Buffer.from('%PDF-')))
          throw new HostError('invalid_pdf')
        return source.bytes
      },
      center: async ticket => {
        current()
        if (remote.uploadOrigin !== ticket.upload_origin) throw new HostError('center_identity_changed')
        const target = await this.#planCenter(binding)
        current()
        return { ...target, uploadOrigin: remote.uploadOrigin }
      },
    })
    return { ...authorized, state: transfer.input_state, transfer }
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

// Only the center-shaped content routes and the capability read are page-reachable on a
// local source; everything else under /api/v1 is the runtime's private host protocol.
function localPrivate(path, method) {
  const decoded = decodeURIComponent(path)
  if (!decoded.startsWith('/api/v1/') && decoded !== '/api/v1') return false
  return !(decoded === '/api/v1/capabilities' && (method === 'GET' || method === 'HEAD'))
}
