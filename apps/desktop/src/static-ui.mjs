import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import { HostError, contentSecurityPolicy } from './policy.mjs'

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf',
  '.wasm': 'application/wasm' }

// ddp://app/api/** → active source (Host /api proxy); ddp://app/_object/<id> →
// opaque center object fetch. Everything else is the packaged static UI.
// clients: () => ClientHost (avoids a main/static-ui import cycle).
export function staticUI(root, expected, { clients } = {}) {
  return async request => {
    const url = new URL(request.url)
    if (clients && url.protocol === 'ddp:' && url.host === 'app'
        && (url.pathname === '/api' || url.pathname.startsWith('/api/'))) {
      return apiProxy(request, url, clients)
    }
    if (clients && url.protocol === 'ddp:' && url.host === 'app'
        && url.pathname.startsWith('/_object/')) {
      return objectFetch(request, url, clients)
    }
    return staticFile(request, root, expected)
  }
}

const PROXY_BODY_LIMIT = 64 * 1024 * 1024
const PROXY_REWRITE_LIMIT = 16 * 1024 * 1024
const SUCCESS_HEADERS = { 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' }

// Stream the renderer request body with an incremental byte cap (invariant 6):
// a Compromised renderer must not OOM the host by POSTing a body whose
// Content-Length lies (or is absent) and then streaming gigabytes. The
// contract promises a streamed 64 MiB cap, so enforce it while reading, not
// after Buffer.from(await request.arrayBuffer()).
async function readProxyBody(request) {
  if (['GET', 'HEAD'].includes(request.method)) return undefined
  const declared = request.headers.get('content-length')
  if (declared !== null) {
    const size = Number(declared)
    if (!Number.isSafeInteger(size) || size < 0) throw new HostError('invalid_arguments')
    if (size > PROXY_BODY_LIMIT) throw new HostError('input_too_large')
  }
  if (!request.body) {
    const buffered = Buffer.from(await request.arrayBuffer())
    if (buffered.length > PROXY_BODY_LIMIT) throw new HostError('input_too_large')
    return buffered.length ? buffered : undefined
  }
  const reader = request.body.getReader()
  const chunks = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > PROXY_BODY_LIMIT) throw new HostError('input_too_large')
      chunks.push(Buffer.from(value))
    }
  } finally { reader.releaseLock() }
  return chunks.length ? Buffer.concat(chunks, total) : undefined
}

async function readBoundedStream(stream, maximum, code) {
  const reader = stream.getReader()
  const chunks = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > maximum) throw new HostError(code)
      chunks.push(Buffer.from(value))
    }
  } finally { reader.releaseLock() }
  return Buffer.concat(chunks, total).toString('utf8')
}

function successHeaders(proxied) {
  const headers = { ...proxied.headers, ...SUCCESS_HEADERS }
  const policy = contentSecurityPolicy(new URL('ddp://app/'))
  if (!Object.keys(headers).some(name => name.toLowerCase() === 'content-security-policy')) {
    headers['Content-Security-Policy'] = policy
  }
  return headers
}

async function apiProxy(request, url, clients) {
  const host = clients()
  const errorHeaders = { 'Content-Security-Policy': contentSecurityPolicy(new URL('ddp://app/')),
    'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' }
  // Error responses carry X-DDP-Source while a source is active, so the
  // renderer's fail-closed fence (missing/mismatched header → source_changed)
  // still surfaces the real error for the current source instead of discarding
  // it. no_active_source has no source to name: the header stays absent and a
  // booted renderer treats it as source_changed (re-sync, never stale data).
  const fail = (status, code, message) => {
    const headers = { ...errorHeaders, 'Content-Type': 'application/json' }
    const active = (() => {
      try { return typeof host.activeSourceId === 'function' ? host.activeSourceId() : null }
      catch { return null }
    })()
    if (active) headers['X-DDP-Source'] = active
    return new Response(JSON.stringify({ error: { code, message } }), { status, headers })
  }
  try {
    if (url.username || url.password) throw new HostError('invalid_arguments')
    const headers = {}
    request.headers.forEach((value, name) => { headers[name] = value })
    const body = await readProxyBody(request)
    const proxied = await host.apiProxy({ method: request.method,
      path: url.pathname, query: url.search.replace(/^\?/, ''), headers, body })
    // JSON responses from a center source get _object rewriting before the
    // bytes leave the host; the renderer never sees a presigned URL.
    let stream = proxied.body
    const responseHeaders = successHeaders(proxied)
    const contentType = responseHeaders['content-type'] ?? responseHeaders['Content-Type'] ?? ''
    if (stream && contentType.includes('application/json') && proxied.rewriteOrigins?.length) {
      const declared = Number(responseHeaders['content-length'] ?? responseHeaders['Content-Length'] ?? NaN)
      if (Number.isFinite(declared) && declared > PROXY_REWRITE_LIMIT) throw new HostError('protocol_incompatible')
      const text = await readBoundedStream(stream, PROXY_REWRITE_LIMIT, 'protocol_incompatible')
      stream = rewriteUrls(proxied.sourceId, host, text, proxied.rewriteOrigins)
      // The rewritten body has a different length than the upstream one.
      for (const name of Object.keys(responseHeaders)) if (name.toLowerCase() === 'content-length') delete responseHeaders[name]
    }
    return new Response(stream, { status: proxied.status, headers: responseHeaders })
  } catch (error) {
    if (!(error instanceof HostError)) return fail(500, 'host_operation_failed', '操作未完成')
    const messages = { no_active_source: '还没有选择数据源', approved_plan_required: '中心在桌面里只读；写操作请作为联邦任务发起并批准',
      source_signed_out: '登录已过期，请重新连接该中心', source_changed: '数据源已切换，此结果已丢弃',
      not_supported_locally: '本机工作区不支持这项功能', source_unavailable: '数据源不可用',
      protocol_incompatible: '中心版本不兼容，已拒绝', connection_not_current: '中心连接未就绪，已拒绝',
      input_too_large: '请求过大', invalid_arguments: '非法请求' }
    const status = error.code === 'no_active_source' ? 503
      : error.code === 'approved_plan_required' ? 403
      : error.code === 'source_signed_out' ? 401
      : error.code === 'not_supported_locally' ? 404
      : error.code === 'source_changed' ? 409
      : error.code === 'connection_not_current' ? 502 : 400
    return fail(status, error.code, messages[error.code] ?? '操作未完成')
  }
}

function rewriteUrls(sourceId, host, text, origins) {
  // Rewrite absolute center object URLs to ddp://app/_object/<opaque>. Only
  // exact-origin matches; relative URLs pass through untouched. Works on decoded
  // string values, not raw JSON text: Go's encoder writes '&' as \u0026, and a
  // raw-text match would stop there and hand the presigned query to the renderer.
  let value
  try { value = JSON.parse(text) } catch { throw new HostError('protocol_incompatible') }
  const rewrite = string => string.replace(/https?:\/\/[^\s"'<>\\]+/g, candidate => {
    let origin
    try { origin = new URL(candidate).origin } catch { return candidate }
    if (!origins.includes(origin)) return candidate
    const id = host.rewriteObjectUrl(sourceId, candidate)
    if (!id) return candidate
    return 'ddp://app/_object/' + id
  })
  const walk = node => typeof node === 'string' ? rewrite(node)
    : Array.isArray(node) ? node.map(walk)
    : node && typeof node === 'object' ? Object.fromEntries(Object.entries(node).map(([key, item]) => [key, walk(item)]))
    : node
  return JSON.stringify(walk(value))
}

async function objectFetch(request, url, clients) {
  const host = clients()
  const errorHeaders = { 'Content-Security-Policy': contentSecurityPolicy(new URL('ddp://app/')),
    'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' }
  try {
    if (request.method !== 'GET' || url.username || url.password) throw new HostError('invalid_arguments')
    const id = decodeURIComponent(url.pathname.slice('/_object/'.length))
    if (!id || id.includes('/') || id.length > 256) throw new HostError('invalid_arguments')
    const fetched = await host.fetchObject(id)
    return new Response(fetched.body, { status: fetched.status, headers: successHeaders(fetched) })
  } catch (error) {
    const code = error instanceof HostError ? error.code : 'host_operation_failed'
    const status = code === 'not_found' ? 404 : code === 'source_changed' ? 409
      : code === 'source_signed_out' ? 401
      : code === 'connection_not_current' || code === 'protocol_incompatible' ? 502 : 400
    return new Response(JSON.stringify({ error: { code, message: '对象读取失败' } }),
      { status, headers: { ...errorHeaders, 'Content-Type': 'application/json' } })
  }
}

async function staticFile(request, root, expected) {
  const headers = { 'Content-Security-Policy': contentSecurityPolicy(expected),
    'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' }
  try {
    const url = new URL(request.url)
    if (request.method !== 'GET' || url.protocol !== 'ddp:' || url.host !== 'app'
        || url.username || url.password) return new Response('', { status: 403, headers })
    const pathname = decodeURIComponent(url.pathname)
    if (pathname.includes('\0') || pathname.includes('\\')) throw new Error('invalid')
    const candidate = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname))
    const canonicalRoot = await realpath(root), canonical = await realpath(candidate)
    if (!canonical.startsWith(canonicalRoot + path.sep)) throw new Error('outside')
    const metadata = await stat(canonical)
    if (!metadata.isFile() || metadata.size > 32 * 1024 * 1024) throw new Error('invalid')
    return new Response(await readFile(canonical), { headers: { ...headers,
      'Content-Type': TYPES[path.extname(canonical)] ?? 'application/octet-stream' } })
  } catch { return new Response('', { status: 404, headers }) }
}
