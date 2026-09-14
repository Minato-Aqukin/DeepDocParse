/** Shared connection ownership. Credentials and authoritative tasks stay in providers. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type TransportState = 'disconnected' | 'connecting' | 'authenticating' | 'ready' | 'backoff' | 'blocked'
export type SnapshotState = 'loading' | 'current' | 'stale' | 'failed'
export interface Environment {
  environmentId: string
  workspaceId: string
  authorityNodeId: string
  endpoint: string
}
export interface Profile { profileId: string; issuer: string; subject: string }
export interface Identity { environmentId: string; workspaceId: string; authorityNodeId: string; capabilities: string[] }
export interface Projection { cursor: string; sequence: number; state: Json }
export interface Event { cursor: string; sequence: number; state: Json; previousSequence?: number }
export interface View {
  transport: TransportState
  snapshot: SnapshotState
  reason: string | null
  projection: Projection | null
}
export type FaultCode = 'authentication_required' | 'identity_mismatch' | 'profile_mismatch' |
  'connection_failed' | 'cursor_expired' | 'event_gap' | 'invalid_event' | 'cache_failure' | 'disposed' | 'protocol_incompatible' | 'outcome_unknown' | 'receipt_required'
export class ConnectionFault extends Error {
  readonly code: FaultCode
  constructor(code: FaultCode) { super(code); this.code = code }
}
export interface Session {
  actor: { issuer: string; subject: string }
  snapshot(signal: AbortSignal): Promise<Projection>
  events(cursor: string, signal: AbortSignal): AsyncIterable<Event>
  query?(name: string, payload: Json, signal: AbortSignal): Promise<Json>
  command(name: string, payload: Json, idempotencyKey: string, signal: AbortSignal): Promise<Json>
  receipt(idempotencyKey: string, signal: AbortSignal): Promise<Json | null>
  close(): Promise<void>
}
export interface Provider {
  inspect(environment: Environment, signal: AbortSignal): Promise<Identity>
  credential(profile: Profile, signal: AbortSignal): Promise<unknown>
  authenticate(environment: Environment, profile: Profile, credential: unknown, signal: AbortSignal): Promise<Session>
}
export interface Intent { digest: string; status: 'pending' | 'unknown' | 'confirmed' | 'retired'; receipt: Json | null }
/** Implementations MUST transact epoch, state and cursor together; never store credentials. */
export interface ProjectionStore {
  claim(scope: string, binding: string): Promise<{ epoch: number; projection: Projection | null }>
  commit(scope: string, epoch: number, expectedCursor: string | null, projection: Projection): Promise<boolean>
  invalidate(scope: string, epoch: number): Promise<void>
  intent(scope: string, key: string, digest: string): Promise<Intent>
  readIntent(scope: string, key: string): Promise<Intent | null>
  settle(scope: string, key: string, digest: string, receipt: Json | null): Promise<void>
  discardUndispatched(scope: string, key: string, digest: string): Promise<void>
  forget(scope: string): Promise<void>
}
const copy = <T>(value: T): T => structuredClone(value)
export function scopeKey(environment: Environment, profile: Profile): string {
  return JSON.stringify([environment.environmentId, profile.profileId])
}
function binding(environment: Environment, profile: Profile): string {
  return JSON.stringify([environment.workspaceId, environment.authorityNodeId,
    profile.issuer, profile.subject])
}
function validProjection(value: Projection): void {
  if (!value || typeof value.cursor !== 'string' || !value.cursor ||
      !Number.isSafeInteger(value.sequence) || value.sequence < 0) throw new ConnectionFault('invalid_event')
  canonical(value.state) // Reject values that cannot be persisted as protocol JSON.
}
function canonical(value: Json, depth = 0, ancestors = new Set<object>()): string {
  if (depth > 64) throw new ConnectionFault('invalid_event')
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value)
  if (value && typeof value === 'object') {
    if (ancestors.has(value)) throw new ConnectionFault('invalid_event')
    ancestors.add(value)
    try {
      if (Array.isArray(value)) return '[' + value.map(item => canonical(item, depth + 1, ancestors)).join(',') + ']'
      if (Object.getPrototypeOf(value) === Object.prototype)
        return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k]!, depth + 1, ancestors)).join(',') + '}'
    } finally { ancestors.delete(value) }
  }
  throw new ConnectionFault('invalid_event')
}
async function commandDigest(name: string, payload: Json): Promise<string> {
  const bytes = new TextEncoder().encode(canonical({ name, payload }))
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
    .map(b => b.toString(16).padStart(2, '0')).join('')
}
async function sleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return
  await new Promise<void>(resolve => {
    const stop = () => { clearTimeout(timer); signal.removeEventListener('abort', stop); resolve() }
    const timer = setTimeout(stop, ms)
    signal.addEventListener('abort', stop, { once: true })
  })
}
export interface RuntimeOptions {
  maxRetries?: number
  delaysMs?: number[]
  wait?: (ms: number, signal: AbortSignal) => Promise<void>
  observerError?: (error: unknown) => void
  /** A sustained healthy interval resets the retry budget; immediate flapping does not. */
  stableMs?: number
  now?: () => number
}
export class Connection {
  readonly environment: Environment
  readonly profile: Profile
  readonly scope: string
  private provider: Provider
  private store: ProjectionStore
  private options: RuntimeOptions
  private epoch = 0
  private controller: AbortController | null = null
  private session: Session | null = null
  private loop: Promise<void> | null = null
  private listeners = new Set<(view: View) => void>()
  private view: View = { transport: 'disconnected', snapshot: 'loading', reason: null, projection: null }
  constructor(environment: Environment, profile: Profile, provider: Provider,
              store: ProjectionStore, options: RuntimeOptions = {}) {
    for (const id of [environment.environmentId, environment.workspaceId, environment.authorityNodeId,
      profile.profileId, profile.issuer, profile.subject]) {
      if (!id || id.length > 512) throw new ConnectionFault('identity_mismatch')
    }
    this.environment = copy(environment); this.profile = copy(profile)
    this.scope = scopeKey(environment, profile); this.provider = provider; this.store = store; this.options = options
  }
  get state(): View { return copy(this.view) }
  subscribe(listener: (view: View) => void): () => void {
    this.listeners.add(listener); this.notify(listener)
    return () => { this.listeners.delete(listener) }
  }
  private notify(listener: (view: View) => void) {
    try { listener(this.state) } catch (error) { this.options.observerError?.(error) }
  }
  private update(patch: Partial<View>) {
    this.view = { ...this.view, ...patch }
    for (const listener of this.listeners) this.notify(listener)
  }
  start(): void {
    if (this.loop || this.view.transport === 'blocked') return
    const controller = new AbortController(); this.controller = controller
    this.loop = this.run(controller).finally(() => {
      if (this.controller === controller) { this.loop = null; this.session = null }
    })
  }
  /** Explicit wake/re-authentication restarts queries only. It never replays commands. */
  async wake(): Promise<void> { await this.stop(); this.start() }
  async stop(): Promise<void> {
    const controller = this.controller
    if (!controller) return
    controller.abort()
    const epoch = this.epoch
    // close() must abort provider I/O. A transport that ignores cancellation is fenced
    // by the persistent epoch and cannot prevent a replacement connection from starting.
    const session = this.session
    this.controller = null; this.session = null; this.loop = null
    this.update({ transport: 'disconnected', snapshot: this.view.projection ? 'stale' : 'loading' })
    // A hung transport cannot block a new generation or a window closing. The
    // provider owns actual I/O cancellation; all late callbacks remain fenced.
    if (session) void session.close().catch(() => {})
    await this.store.invalidate(this.scope, epoch)
  }
  private active(controller: AbortController): boolean {
    return this.controller === controller && !controller.signal.aborted
  }
  private async apply(value: Projection, controller: AbortController, epoch: number): Promise<boolean> {
    validProjection(value)
    if (!this.active(controller)) return false
    let saved: boolean
    try { saved = await this.store.commit(this.scope, epoch, this.view.projection?.cursor ?? null, value) }
    catch { throw new ConnectionFault('cache_failure') }
    if (!this.active(controller)) return false
    if (!saved) throw new ConnectionFault('disposed')
    this.update({ projection: copy(value), snapshot: 'current' }); return true
  }
  private async run(controller: AbortController): Promise<void> {
    const signal = controller.signal
    let forceSnapshot = false
    let epoch: number
    try {
      const claim = await this.store.claim(this.scope, binding(this.environment, this.profile)); epoch = claim.epoch
      if (!this.active(controller)) { await this.store.invalidate(this.scope, epoch); return }
      this.epoch = epoch
      this.update({ projection: claim.projection, snapshot: claim.projection ? 'stale' : 'loading' })
    } catch (error) {
      if (this.active(controller)) this.update({ transport: 'blocked', snapshot: 'failed',
        reason: error instanceof ConnectionFault ? error.code : 'cache_failure' })
      return
    }
    const max = Math.max(0, Math.min(this.options.maxRetries ?? 5, 20))
    const delays = this.options.delaysMs ?? [250, 1000, 2500, 5000, 10000]
    let failures = 0
    const now = this.options.now ?? Date.now
    const stableMs = Math.max(1000, this.options.stableMs ?? 10000)
    for (; this.active(controller);) {
      let session: Session | null = null
      let readyAt: number | null = null
      try {
        this.update({ transport: 'connecting', reason: null })
        const identity = await this.provider.inspect(this.environment, signal)
        if (!this.active(controller)) return
        if (identity.environmentId !== this.environment.environmentId ||
            identity.workspaceId !== this.environment.workspaceId ||
            identity.authorityNodeId !== this.environment.authorityNodeId)
          throw new ConnectionFault('identity_mismatch')
        this.update({ transport: 'authenticating' })
        const credential = await this.provider.credential(this.profile, signal)
        if (!this.active(controller)) return
        session = await this.provider.authenticate(this.environment, this.profile, credential, signal)
        if (!this.active(controller)) { await session.close(); return }
        if (session.actor.issuer !== this.profile.issuer || session.actor.subject !== this.profile.subject)
          throw new ConnectionFault('profile_mismatch')
        this.session = session
        if (!this.view.projection || forceSnapshot) {
          this.update({ snapshot: 'loading' })
          if (!await this.apply(await session.snapshot(signal), controller, epoch)) return
          forceSnapshot = false
        }
        this.update({ transport: 'ready' })
        readyAt = now()
        for await (const event of session.events(this.view.projection!.cursor, signal)) {
          if (!this.active(controller)) return
          validProjection(event)
          const current = this.view.projection!
          if (event.sequence === current.sequence && event.cursor === current.cursor) {
            this.update({ snapshot: 'current' }); continue
          }
          if (event.previousSequence !== undefined
            ? event.previousSequence !== current.sequence || event.sequence <= current.sequence
            : event.sequence !== current.sequence + 1) throw new ConnectionFault('event_gap')
          if (!await this.apply(event, controller, epoch)) return
        }
        if (this.active(controller)) throw new ConnectionFault('connection_failed')
      } catch (error) {
        if (!this.active(controller)) return
        const code = error instanceof ConnectionFault ? error.code : 'connection_failed'
        if (code === 'identity_mismatch' || code === 'profile_mismatch' || code === 'authentication_required' ||
            code === 'invalid_event' || code === 'cache_failure' || code === 'disposed' || code === 'protocol_incompatible') {
          this.update({ transport: 'blocked', snapshot: this.view.projection ? 'stale' : 'failed', reason: code }); return
        }
        forceSnapshot ||= code === 'cursor_expired' || code === 'event_gap'
        if (readyAt !== null && now() - readyAt >= stableMs) failures = 0
        if (failures >= max) {
          this.update({ transport: 'blocked', snapshot: this.view.projection ? 'stale' : 'failed', reason: code }); return
        }
        this.update({ transport: 'backoff', snapshot: this.view.projection ? 'stale' : 'loading', reason: code })
        const delay = delays[Math.min(failures++, delays.length - 1)] ?? 0
        await (this.options.wait ?? sleep)(Math.max(0, Math.min(delay, 30000)), signal)
      } finally {
        if (session) { try { await session.close() } catch { /* provider cleanup cannot start another retry loop */ } }
        if (this.session === session) this.session = null
      }
    }
  }
  async query(name: string, payload: Json): Promise<Json> {
    if (!['corpus.search', 'evidence.get', 'models.list', 'resource.page', 'task.page', 'wiki.list', 'wiki.get', 'wiki.revisions'].includes(name))
      throw new ConnectionFault('protocol_incompatible')
    canonical(payload)
    const session = this.session, controller = this.controller
    if (this.view.transport !== 'ready' || !session || !controller)
      throw new ConnectionFault('connection_failed')
    if (!session.query) throw new ConnectionFault('protocol_incompatible')
    const result = await session.query(name, payload, controller.signal)
    // A query from a former identity/connection must not populate the new panel.
    if (!this.active(controller) || this.session !== session) throw new ConnectionFault('disposed')
    canonical(result)
    return result
  }
  async execute(name: string, payload: Json, idempotencyKey: string): Promise<Json> {
    if (!/^[a-z][a-z0-9_.-]{0,95}$/.test(name) || !/^[A-Za-z0-9_-]{8,128}$/.test(idempotencyKey))
      throw new ConnectionFault('invalid_event')
    const session = this.session, controller = this.controller
    if (this.view.transport !== 'ready' || !session || !controller) throw new ConnectionFault('connection_failed')
    const digest = await commandDigest(name, payload)
    const intent = await this.store.intent(this.scope, idempotencyKey, digest)
    // Existing uncertain intents require explicit receipt reconciliation. Retrying
    // execute is not permission to send an expensive command twice.
    if (!this.active(controller) || this.session !== session || this.view.transport !== 'ready') {
      // This execution knows no I/O happened. A crash at any other point remains
      // unknown and requires server reconciliation; a null lookup is not proof.
      await this.store.discardUndispatched(this.scope, idempotencyKey, digest)
      throw new ConnectionFault('disposed')
    }
    if (intent.status === 'confirmed') return copy(intent.receipt!)
    if (intent.status === 'unknown') throw new ConnectionFault('outcome_unknown')
    if (intent.status === 'retired') throw new ConnectionFault('receipt_required')
    let result: Json
    try {
      result = await session.command(name, payload, idempotencyKey, controller.signal)
      canonical(result)
      await this.store.settle(this.scope, idempotencyKey, digest, result)
    } catch (error) {
      await this.store.settle(this.scope, idempotencyKey, digest, null)
      throw error
    }
    // Preserve the original identity's durable receipt even when its transport
    // has gone away; only the new active generation may present it to a caller.
    if (!this.active(controller) || this.session !== session) throw new ConnectionFault('disposed')
    return result
  }
  async receipt(idempotencyKey: string): Promise<Json | null> {
    if (!/^[A-Za-z0-9_-]{8,128}$/.test(idempotencyKey)) throw new ConnectionFault('invalid_event')
    const session = this.session, controller = this.controller
    if (this.view.transport !== 'ready' || !session || !controller) throw new ConnectionFault('connection_failed')
    const intent = await this.store.readIntent(this.scope, idempotencyKey)
    if (!this.active(controller) || this.session !== session) throw new ConnectionFault('disposed')
    const receipt = await session.receipt(idempotencyKey, controller.signal)
    if (intent && receipt !== null) await this.store.settle(this.scope, idempotencyKey, intent.digest, receipt)
    if (!this.active(controller) || this.session !== session) throw new ConnectionFault('disposed')
    canonical(receipt)
    return receipt
  }
}

/** In-memory adapter for tests/session-only mode. Durable desktop storage uses the same CAS contract. */
export class MemoryProjectionStore implements ProjectionStore {
  private rows = new Map<string, { binding: string | null; epoch: number; projection: Projection | null; intents: Map<string, Intent> }>()
  private row(scope: string) {
    let row = this.rows.get(scope)
    if (!row) { row = { binding: null, epoch: 0, projection: null, intents: new Map() }; this.rows.set(scope, row) }
    return row
  }
  async claim(scope: string, expectedBinding: string) {
    const row = this.row(scope)
    if (row.binding !== null && row.binding !== expectedBinding) throw new ConnectionFault('identity_mismatch')
    row.binding = expectedBinding; row.epoch++
    return { epoch: row.epoch, projection: copy(row.projection) }
  }
  async commit(scope: string, epoch: number, expectedCursor: string | null, projection: Projection) {
    const row = this.row(scope)
    if (row.epoch !== epoch || (row.projection?.cursor ?? null) !== expectedCursor) return false
    row.projection = copy(projection); return true
  }
  async invalidate(scope: string, epoch: number) { const row = this.row(scope); if (row.epoch === epoch) row.epoch++ }
  async intent(scope: string, key: string, digest: string) {
    const row = this.row(scope), found = row.intents.get(key)
    if (found && found.digest !== digest) throw new Error('idempotency_conflict')
    if (found) return copy({ ...found, status: found.status === 'pending' ? 'unknown' : found.status })
    const intent: Intent = { digest, status: 'pending', receipt: null }
    row.intents.set(key, intent); return copy(intent)
  }
  async readIntent(scope: string, key: string) { return copy(this.row(scope).intents.get(key) ?? null) }
  async settle(scope: string, key: string, digest: string, receipt: Json | null) {
    const found = this.row(scope).intents.get(key)
    if (!found || found.digest !== digest) throw new Error('idempotency_conflict')
    // A late failure must not erase a previously confirmed receipt.
    if (found.status === 'confirmed' || (found.status === 'retired' && receipt === null)) return
    found.status = receipt === null ? 'unknown' : 'confirmed'; found.receipt = copy(receipt)
    if (receipt !== null) {
      const others = [...this.row(scope).intents.values()].filter(item => item !== found && item.status === 'confirmed')
      for (const old of others.slice(0, -127)) { old.status = 'retired'; old.receipt = null }
    }
  }
  async discardUndispatched(scope: string, key: string, digest: string) {
    const row = this.row(scope), found = row.intents.get(key)
    if (found?.digest === digest && found.status === 'pending') row.intents.delete(key)
  }
  async forget(scope: string) {
    // Preserve the epoch high-water mark: deletion must not allow an ABA stale write.
    const row = this.row(scope); row.epoch++; row.projection = null
    // Pending/unknown command receipts are reconciliation records, not a cache.
  }
}
export class ConnectionRegistry {
  private connections = new Map<string, { binding: string; connection: Connection; references: number }>()
  private store: ProjectionStore
  constructor(store: ProjectionStore) { this.store = store }
  acquire(environment: Environment, profile: Profile, provider: Provider, options: RuntimeOptions = {}) {
    const key = scopeKey(environment, profile), signature = binding(environment, profile)
    let entry = this.connections.get(key)
    if (entry && entry.binding !== signature) throw new ConnectionFault('identity_mismatch')
    if (entry && entry.connection.environment.endpoint !== environment.endpoint)
      throw new ConnectionFault('connection_failed') // Use relocate; never silently keep the old transport.
    if (!entry) {
      entry = { binding: signature, connection: new Connection(environment, profile, provider, this.store, options), references: 0 }
      this.connections.set(key, entry)
    }
    entry.references++; entry.connection.start()
    let released = false
    return { connection: entry.connection, release: async () => {
      if (released) return; released = true; entry!.references--
      if (entry!.references === 0) {
        if (this.connections.get(key) === entry) this.connections.delete(key)
        await entry!.connection.stop()
      }
    } }
  }
  /** Disconnecting/removing a connection never deletes the user's runtime assets. */
  async remove(environment: Environment, profile: Profile, forgetProjection = false) {
    const key = scopeKey(environment, profile), entry = this.connections.get(key)
    if (entry) { this.connections.delete(key); await entry.connection.stop() }
    if (forgetProjection) await this.store.forget(key)
  }
  /** Address changes retain the identity-scoped cache, but must handshake again. */
  async relocate(environment: Environment, profile: Profile, provider: Provider, options: RuntimeOptions = {}) {
    await this.remove(environment,profile)
    return this.acquire(environment,profile,provider,options)
  }
}
