import { HostError, object } from './policy.mjs'


export const SOURCE_CHANNELS = Object.freeze({
  sourceList: 'ddp:source-list',
  sourceActivate: 'ddp:source-activate',
  sourceRemove: 'ddp:source-remove',
  workspaceOpen: 'ddp:workspace-open',
  centerConnect: 'ddp:center-connect',
  onSourceChange: 'ddp:source-change-subscribe',
})

const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/

function id(value) {
  if (typeof value !== 'string' || !SOURCE_ID.test(value)) throw new HostError('invalid_arguments')
}

// Renderer input validation for the source bridge. Unknown fields fail before any
// host work; no endpoint, token, path or filesystem value crosses this boundary.
export function sourceArguments(method, input, options = {}) {
  if (method === 'sourceList' || method === 'workspaceOpen') {
    if (input !== undefined && input !== null) throw new HostError('invalid_arguments')
    return undefined
  }
  if (method === 'sourceActivate' || method === 'sourceRemove') {
    object(input, ['sourceId'])
    id(input.sourceId)
    return { sourceId: input.sourceId }
  }
  if (method === 'onSourceChange') {
    if (input !== undefined && input !== null) throw new HostError('invalid_arguments')
    return undefined
  }
  if (method === 'centerConnect') {
    object(input, ['endpoint', 'username', 'password', 'persist',
      ...(Object.hasOwn(input ?? {}, 'storageOrigin') ? ['storageOrigin'] : [])])
    if (typeof input.username !== 'string' || !input.username.trim() || input.username.length > 256
        || /[\x00-\x1f\x7f]/.test(input.username)) throw new HostError('invalid_arguments')
    if (typeof input.password !== 'string' || !input.password || input.password.length > 4096
        || /[\x00-\x1f\x7f]/.test(input.password)) throw new HostError('invalid_arguments')
    if (typeof input.persist !== 'boolean') throw new HostError('invalid_arguments')
    // Endpoint and storage origin are normalized here; the password is validated
    // but never stored, logged or returned — only the issued JWT is kept.
    const endpoint = normalizeEndpoint(input.endpoint, options)
    const storageOrigin = input.storageOrigin === undefined || input.storageOrigin === ''
      ? undefined : normalizeOrigin(input.storageOrigin, options)
    return { endpoint, username: input.username, password: input.password,
      persist: input.persist, ...(storageOrigin === undefined ? {} : { storageOrigin }) }
  }
  throw new HostError('unknown_operation')
}

// Center endpoints are https. Plain http loopback is a development-only escape
// hatch for testing against the local stack; packaged builds refuse it. Hosts
// match what the shared HttpProvider accepts for loopback (no `localhost`).
export function normalizeEndpoint(raw, { packaged = true } = {}) {
  let url
  try { url = new URL(typeof raw === 'string' ? raw : '') }
  catch { throw new HostError('invalid_endpoint') }
  if (url.username || url.password || url.hash || url.search) throw new HostError('invalid_endpoint')
  if (url.protocol === 'https:') return url.href.replace(/\/$/, '')
  if (!packaged && url.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(url.hostname))
    return url.href.replace(/\/$/, '')
  throw new HostError('invalid_endpoint')
}

// A storage origin is only ever an allowlist entry for object-URL rewriting and
// a fetch target for presigned (self-authenticating) object URLs. It never
// receives an Authorization header.
export function normalizeOrigin(raw, { packaged = true } = {}) {
  let url
  try { url = new URL(typeof raw === 'string' ? raw : '') }
  catch { throw new HostError('invalid_endpoint') }
  if (url.username || url.password || url.hash || url.search || url.pathname !== '/') {
    throw new HostError('invalid_endpoint')
  }
  if (url.protocol === 'https:') return url.origin
  if (!packaged && url.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(url.hostname)) {
    return url.origin
  }
  throw new HostError('invalid_endpoint')
}
