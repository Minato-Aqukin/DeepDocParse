import { ConnectionFault } from './index.ts'
import type { Environment, Event, Identity, Json, Profile, Projection, Provider, Session } from './index.ts'

const PROTOCOL = 'ddp-client/1'
const MAX_RESPONSE = 4 * 1024 * 1024
type ObjectValue = Record<string, unknown>
function object(value: unknown): ObjectValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ConnectionFault('protocol_incompatible')
  return value as ObjectValue
}
function identifier(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 512) throw new ConnectionFault('protocol_incompatible')
  return value
}
function base64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')
}
function decode(value: unknown, length: number, url = false): Uint8Array {
  if (typeof value !== 'string' || !(url ? /^[A-Za-z0-9_-]+$/ : /^[A-Za-z0-9+/]+={0,2}$/).test(value))
    throw new ConnectionFault('identity_mismatch')
  try {
    const raw = url ? value.replace(/-/g,'+').replace(/_/g,'/') : value
    const bytes = Uint8Array.from(atob(raw),char=>char.charCodeAt(0))
    if (bytes.length !== length) throw new Error('wrong_length')
    return bytes
  } catch { throw new ConnectionFault('identity_mismatch') }
}
function identity(value: unknown): Identity {
  const raw = object(value)
  return { environmentId: identifier(raw.environment_id), workspaceId: identifier(raw.workspace_id),
    authorityNodeId: identifier(raw.authority_node_id), capabilities: [] }
}
function projection(value: unknown): Projection {
  const raw = object(value)
  if (!Number.isSafeInteger(raw.sequence) || (raw.sequence as number) < 0 || !('state' in raw))
    throw new ConnectionFault('invalid_event')
  return { cursor: identifier(raw.cursor), sequence: raw.sequence as number, state: raw.state as Json }
}
async function pause(ms: number, signal: AbortSignal) {
  if (signal.aborted) return
  await new Promise<void>(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
    const timer = setTimeout(finish, ms)
    signal.addEventListener('abort', finish, { once: true })
  })
}
export class OperationFault extends Error {
  readonly code: string
  constructor(code: string) { super(code); this.code = code }
}
export interface HttpProviderOptions {
  /** Local: verified bootstrap from the owned launcher. Remote: public node descriptor. */
  inspect?: (environment: Environment, signal: AbortSignal) => Promise<Identity>
  credential: (profile: Profile, signal: AbortSignal) => Promise<string>
  fetch?: typeof fetch
  pollMs?: number
  /** Only the host may opt into its own literal loopback listener. */
  ownedLoopback?: boolean
  /** Explicitly selects the existing local task endpoints. Remote writes await plan approval. */
  localCommands?: boolean
}

/** No token persistence, redirects, ambient cookies, proxy logging or automatic write retry. */
export class HttpProvider implements Provider {
  private options: HttpProviderOptions
  constructor(options: HttpProviderOptions) { this.options = options }
  private base(environment: Environment): string {
    let url: URL
    try { url = new URL(environment.endpoint) } catch { throw new ConnectionFault('identity_mismatch') }
    if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== 'https:' && !(this.options.ownedLoopback && url.protocol === 'http:' &&
          ['127.0.0.1', '[::1]'].includes(url.hostname)))) throw new ConnectionFault('identity_mismatch')
    return url.href.replace(/\/$/, '')
  }
  private async request(environment: Environment, path: string, signal: AbortSignal, token?: string,
                        body?: Json, key?: string, missingIsNull = false, method?: 'PATCH'): Promise<unknown> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (token) headers.Authorization = `Bearer ${token}`
    if (key) headers['Idempotency-Key'] = key
    const encoded = body === undefined ? undefined : JSON.stringify(body)
    if (encoded !== undefined) {
      if (new TextEncoder().encode(encoded).length > 65536) throw new OperationFault('input_too_large')
      headers['Content-Type'] = 'application/json'
    }
    const response = await (this.options.fetch ?? fetch)(this.base(environment) + path, {
      method: method ?? (encoded === undefined ? 'GET' : 'POST'), headers, body: encoded,
      signal: AbortSignal.any([signal, AbortSignal.timeout(encoded === undefined ? 10000 : 120000)]),
      redirect: 'error', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
    })
    if (response.status === 401 || response.status === 403) {
      await response.body?.cancel(); throw new ConnectionFault('authentication_required')
    }
    if (missingIsNull && response.status === 404) { await response.body?.cancel(); return null }
    if (response.status === 410) { await response.body?.cancel(); throw new ConnectionFault('cursor_expired') }
    if (!response.headers.get('content-type')?.includes('application/json') || !response.body) {
      await response.body?.cancel(); throw new ConnectionFault('protocol_incompatible')
    }
    const reader = response.body.getReader(), parts: Uint8Array[] = []; let total = 0
    try {
      while (true) {
        const {value,done} = await reader.read(); if (done) break
        total += value.length
        if (total > MAX_RESPONSE) throw new ConnectionFault('protocol_incompatible')
        parts.push(value)
      }
    } finally { await reader.cancel() }
    const bytes = new Uint8Array(total); let offset = 0
    for (const part of parts) { bytes.set(part,offset); offset += part.length }
    let parsed: unknown
    try { parsed = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)) }
    catch { throw new ConnectionFault('protocol_incompatible') }
    if (!response.ok) {
      const code = object(object(parsed).error).code
      // Do not propagate server messages, URLs or echoed input into UI logs.
      if (typeof code !== 'string' || !/^[a-z][a-z0-9_]{0,95}$/.test(code))
        throw new ConnectionFault('connection_failed')
      throw new OperationFault(code)
    }
    return parsed
  }
  async inspect(environment: Environment, signal: AbortSignal): Promise<Identity> {
    this.base(environment)
    if (this.options.inspect) return this.options.inspect(environment, signal)
    const nonce = base64(crypto.getRandomValues(new Uint8Array(24)))
    const raw = object(await this.request(environment, '/api/v1/federation/node?challenge='+nonce, signal))
    const nodeId = identifier(raw.authority_node_id)
    if (nodeId !== environment.authorityNodeId) throw new ConnectionFault('identity_mismatch')
    const publicKey = decode(raw.public_key,32), proof = object(raw.proof)
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',publicKey as BufferSource))]
      .map(byte=>byte.toString(16).padStart(2,'0')).join('')
    if (nodeId !== 'node-'+hash.slice(0,48) || proof.node_id !== nodeId ||
        proof.schema !== 'ddp-node-proof/1' || proof.nonce !== nonce || proof.endpoint !== this.base(environment))
      throw new ConnectionFault('identity_mismatch')
    const issued = Date.parse(identifier(proof.issued_at)), expires = Date.parse(identifier(proof.expires_at)), now = Date.now()
    if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now+5000 ||
        expires <= now || expires-issued > 60000 || expires <= issued || now-issued > 65000)
      throw new ConnectionFault('identity_mismatch')
    const key = await crypto.subtle.importKey('raw',publicKey as BufferSource,{name:'Ed25519'},false,['verify'])
    const signed = new TextEncoder().encode(JSON.stringify([proof.schema,proof.nonce,proof.node_id,
      proof.endpoint,proof.issued_at,proof.expires_at]))
    if (!await crypto.subtle.verify('Ed25519',key,decode(proof.signature,64,true) as BufferSource,signed))
      throw new ConnectionFault('identity_mismatch')
    // Public descriptors reveal the node, not other organizations. The requested
    // workspace is verified before new authoritative data is applied.
    return { environmentId: nodeId, authorityNodeId: nodeId, workspaceId: environment.workspaceId, capabilities: [] }
  }
  credential(profile: Profile, signal: AbortSignal) { return this.options.credential(profile,signal) }
  async authenticate(environment: Environment, profile: Profile, credential: unknown, signal: AbortSignal): Promise<Session> {
    if (typeof credential !== 'string' || credential.length < 16 || /[\r\n]/.test(credential))
      throw new ConnectionFault('authentication_required')
    const controller = new AbortController(), sessionSignal = AbortSignal.any([signal, controller.signal])
    const data = object(await this.request(environment, '/api/v1/client/handshake', sessionSignal, credential))
    const capabilities = data.capabilities
    if (data.protocol_version !== PROTOCOL || !Array.isArray(capabilities) ||
        !capabilities.every(value => typeof value === 'string') ||
        !['client.snapshot','client.events','client.receipt'].every(value => capabilities.includes(value)))
      throw new ConnectionFault('protocol_incompatible')
    const actual = identity(data.identity), actor = object(data.profile)
    if (actual.environmentId !== environment.environmentId || actual.workspaceId !== environment.workspaceId ||
        actual.authorityNodeId !== environment.authorityNodeId) throw new ConnectionFault('identity_mismatch')
    if (actor.issuer !== profile.issuer || actor.subject !== profile.subject) throw new ConnectionFault('profile_mismatch')
    const request = (path: string, extraSignal: AbortSignal, body?: Json, key?: string, missingIsNull?: boolean, method?: 'PATCH') =>
      this.request(environment, path, AbortSignal.any([sessionSignal, extraSignal]), credential, body, key, missingIsNull, method)
    const options = this.options
    return {
      actor: { issuer: profile.issuer, subject: profile.subject },
      async snapshot(abort) { return projection(await request('/api/v1/client/snapshot',abort)) },
      async *events(cursor, abort) {
        const activeSignal = AbortSignal.any([sessionSignal,abort])
        while (!activeSignal.aborted) {
          const batch = object(await request('/api/v1/client/events?after='+encodeURIComponent(cursor),activeSignal))
          if (!Array.isArray(batch.events) || batch.events.length > 100) throw new ConnectionFault('invalid_event')
          for (const raw of batch.events) {
            const event: Event = projection(raw), previous = object(raw).previous_sequence
            if (!Number.isSafeInteger(previous) || (previous as number) < 0) throw new ConnectionFault('invalid_event')
            event.previousSequence = previous as number
            yield event; cursor = event.cursor
          }
          await pause(Math.max(50,Math.min(options.pollMs ?? 1000,30000)),activeSignal)
        }
      },
      async query(name,payload,abort) {
        const raw = object(payload)
        if (!options.localCommands) {
          if (!capabilities.includes('client.query') ||
              !['corpus.search','evidence.get','resource.page','task.page'].includes(name))
            throw new OperationFault('unsupported_operation')
          if (['resource.page','task.page'].includes(name) && !capabilities.includes('client.windows'))
            throw new OperationFault('unsupported_operation')
          return await request('/api/v1/client/query',abort,{name,payload}) as Json
        }
        if (name === 'corpus.search') return await request('/api/v1/search',abort,payload) as Json
        if (name === 'models.list' && Object.keys(raw).length === 0)
          return await request('/api/v1/models',abort) as Json
        const wikiId = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value)
        if (['wiki.list','wiki.revisions'].includes(name) &&
            Object.keys(raw).every(key=>['cursor','limit',...(name==='wiki.revisions'?['wiki_id']:[])].includes(key)) &&
            (name!=='wiki.revisions' || wikiId(raw.wiki_id)) &&
            (raw.cursor === undefined || (typeof raw.cursor === 'string' && raw.cursor.length <= 4096)) &&
            (raw.limit === undefined || (Number.isInteger(raw.limit) && Number(raw.limit)>0 && Number(raw.limit)<=100))) {
          const params = new URLSearchParams()
          if(raw.cursor) params.set('cursor',String(raw.cursor))
          if(raw.limit) params.set('limit',String(raw.limit))
          const path = name==='wiki.list' ? '/api/v1/wikis' : '/api/v1/wikis/'+raw.wiki_id+'/revisions'
          return await request(path+(params.size?'?'+params:''),abort) as Json
        }
        if(name==='wiki.get' && Object.keys(raw).every(key=>['wiki_id','revision_id'].includes(key)) &&
            wikiId(raw.wiki_id) && (raw.revision_id === undefined || wikiId(raw.revision_id)))
          return await request('/api/v1/wikis/'+raw.wiki_id+(raw.revision_id ? '/revisions/'+raw.revision_id : ''),abort) as Json
        if (name === 'evidence.get' && Object.keys(raw).length === 1 &&
            typeof raw.evidence_id === 'string' && /^[A-Za-z0-9_:.\/-]{1,512}$/.test(raw.evidence_id))
          return await request('/api/v1/evidence/'+encodeURIComponent(raw.evidence_id),abort) as Json
        throw new OperationFault('unsupported_operation')
      },
      async command(name,payload,key,abort) {
        if (!options.localCommands) throw new OperationFault('approved_plan_required')
        const raw = object(payload)
        if (['models.install','models.start'].includes(name) && Object.keys(raw).length === 1 &&
            typeof raw.model_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(raw.model_id))
          return await request('/api/v1/models/'+raw.model_id+'/'+(name === 'models.install' ? 'install' : 'start'),abort,{},key) as Json
        if (name === 'models.stop' && Object.keys(raw).length === 0)
          return await request('/api/v1/models/stop',abort,{},key) as Json
        const wikiId = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value)
        if (['wiki.create','wiki.rebuild','wiki.edit'].includes(name)) {
          const fields = name==='wiki.create' ? ['body'] : name==='wiki.rebuild' ? ['wiki_id','body'] : ['wiki_id','page_key','body']
          if (Object.keys(raw).length !== fields.length || Object.keys(raw).some(field=>!fields.includes(field)) ||
              (name!=='wiki.create' && !wikiId(raw.wiki_id)) || (name==='wiki.edit' && !wikiId(raw.page_key)))
            throw new OperationFault('unsupported_operation')
          object(raw.body)
          const path = name==='wiki.create' ? '/api/v1/wikis' : name==='wiki.rebuild'
            ? '/api/v1/wikis/'+raw.wiki_id+'/revisions' : '/api/v1/wikis/'+raw.wiki_id+'/pages/'+raw.page_key
          return await request(path,abort,raw.body as Json,key,false,name==='wiki.edit'?'PATCH':undefined) as Json
        }
        const path = name === 'answer.generate' ? '/api/v1/answer' : name === 'wiki.build' ? '/api/v1/wiki' : null
        if (path) return await request(path,abort,payload,key) as Json
        if (name === 'task.cancel' && typeof raw.task_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(raw.task_id))
          return await request('/api/v1/tasks/'+raw.task_id+'/cancel',abort,{},key) as Json
        throw new OperationFault('unsupported_operation')
      },
      async receipt(key,abort) { return await request('/api/v1/client/receipts/'+encodeURIComponent(key),abort,undefined,undefined,true) as Json | null },
      async close() { controller.abort() },
    }
  }
}
