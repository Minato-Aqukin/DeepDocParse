import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import { contentSecurityPolicy } from './policy.mjs'

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.wasm': 'application/wasm' }

export function staticUI(root, expected) {
  return async request => {
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
}
