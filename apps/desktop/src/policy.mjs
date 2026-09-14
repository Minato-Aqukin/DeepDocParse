export class HostError extends Error {
  constructor(code) { super(code); this.code = code }
}

export const CHANNELS = Object.freeze({
  hostStatus: 'ddp:host-status', selectWorkspace: 'ddp:select-workspace',
  startLocal: 'ddp:start-local', stopLocal: 'ddp:stop-local', runtimeStatus: 'ddp:runtime-status',
  setCredential: 'ddp:set-credential', credentialStatus: 'ddp:credential-status',
  clearCredential: 'ddp:clear-credential',
})

export function identity(input) {
  object(input, ['environmentId', 'profileId'])
  for (const key of ['environmentId', 'profileId']) {
    if (typeof input[key] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(input[key])) {
      throw new HostError('invalid_identity')
    }
  }
  return { environmentId: input.environmentId, profileId: input.profileId }
}

export function object(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) {
    throw new HostError('invalid_arguments')
  }
}

export function validate(method, input) {
  if (['hostStatus', 'selectWorkspace'].includes(method)) {
    if (input !== undefined && input !== null) throw new HostError('invalid_arguments')
    return undefined
  }
  if (['startLocal', 'stopLocal', 'runtimeStatus'].includes(method)) {
    object(input, ['workspaceId'])
    if (typeof input.workspaceId !== 'string' || !/^[a-f0-9-]{36}$/.test(input.workspaceId)) {
      throw new HostError('invalid_workspace')
    }
    return { workspaceId: input.workspaceId }
  }
  if (['credentialStatus', 'clearCredential'].includes(method)) return identity(input)
  if (method === 'setCredential') {
    object(input, ['environmentId', 'profileId', 'secret', 'persist'])
    const pair = identity({ environmentId: input.environmentId, profileId: input.profileId })
    if (typeof input.secret !== 'string' || !input.secret || input.secret.length > 16384
        || /[\x00-\x1f\x7f]/.test(input.secret) || typeof input.persist !== 'boolean') {
      throw new HostError('invalid_credential')
    }
    return { ...pair, secret: input.secret, persist: input.persist }
  }
  throw new HostError('unknown_operation')
}

export function uiLocation(devURL) {
  if (!devURL) return new URL('ddp://app/index.html')
  const url = new URL(devURL)
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)
      || !url.port || url.username || url.password || url.search || url.hash
      || !['/', '/index.html'].includes(url.pathname)) throw new HostError('invalid_development_url')
  return url
}

export function isUI(url, expected) {
  try {
    const actual = new URL(url)
    return actual.protocol === expected.protocol && actual.host === expected.host
      && ['/', '/index.html'].includes(actual.pathname) && !actual.username && !actual.password
  } catch { return false }
}

export function authorizeSender(event, contents, expected) {
  if (!event || event.sender !== contents || contents.isDestroyed()
      || !event.senderFrame || event.senderFrame !== contents.mainFrame
      || !isUI(event.senderFrame.url, expected)) throw new HostError('untrusted_sender')
}

export function contentSecurityPolicy(expected) {
  const dev = expected.protocol === 'http:'
  const connection = dev ? `${expected.origin} ${expected.origin.replace('http:', 'ws:')}` : "'self'"
  return `default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; `
    + `img-src 'self' data: blob:; font-src 'self' data:; connect-src ${connection} blob:; `
    + `worker-src 'self' blob:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`
}

export function allowRequest(rawURL, expected) {
  try {
    const url = new URL(rawURL)
    if (url.protocol === 'ddp:') return url.host === 'app'
    if (['blob:', 'data:'].includes(url.protocol)) return true
    return expected.protocol === 'http:' && ['http:', 'ws:'].includes(url.protocol)
      && url.host === expected.host && !url.username && !url.password
  } catch { return false }
}

export function safeFailure(error) {
  return { ok: false, error: { code: error instanceof HostError ? error.code : 'host_operation_failed' } }
}
