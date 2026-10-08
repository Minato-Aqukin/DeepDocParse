import test from 'node:test'
import assert from 'node:assert/strict'
import { staticUI } from '../src/static-ui.mjs'
import { contentSecurityPolicy, uiLocation } from '../src/policy.mjs'

// Proxy hardening: streamed 64 MiB request cap (declared pre-check + incremental
// byte count), 16 MiB center-JSON rewrite cap, success security headers, and
// X-DDP-Source on host error responses. Fake host: no runtime, no network.

const MIB = 1024 * 1024

function fakeHost(overrides = {}) {
  const calls = { apiProxy: 0 }
  const host = {
    activeSourceId: () => 'source-1',
    rewriteObjectUrl: () => null,
    fetchObject: async () => ({ status: 200, headers: {}, sourceId: 'source-1', body: 'bytes' }),
    apiProxy: async () => {
      calls.apiProxy++
      return { status: 200, headers: { 'content-type': 'application/json' }, body: '{"ok":true}',
        rewriteOrigins: [], sourceId: 'source-1' }
    },
    ...overrides,
  }
  return { host, calls }
}

const serve = host => staticUI('/nonexistent-ui-root', uiLocation(), { clients: () => host })

test('declared Content-Length over 64 MiB is rejected before the body is read', async () => {
  const { host, calls } = fakeHost()
  let pulls = 0
  const body = new ReadableStream({
    pull(controller) {
      pulls++
      controller.enqueue(new Uint8Array(1024))
    },
  })
  // Undici may pull a stream body once at Request construction; baseline it so the
  // assertion measures only what the host read.
  const request = new Request('ddp://app/api/uploads', { method: 'POST',
    headers: { 'content-length': String(64 * MIB + 1) }, body, duplex: 'half' })
  await new Promise(resolve => setTimeout(resolve, 0))
  const baseline = pulls
  const response = await serve(host)(request)
  assert.equal(response.status, 400)
  assert.equal((await response.json()).error.code, 'input_too_large')
  assert.equal(pulls, baseline, 'an over-cap declared length must not pull the body at all')
  assert.equal(calls.apiProxy, 0, 'rejected requests never reach the host proxy')
})

test('a lying Content-Length under the cap that streams past 64 MiB aborts early', async () => {
  const { host, calls } = fakeHost()
  let pulls = 0
  const chunk = new Uint8Array(MIB)
  const body = new ReadableStream({
    pull(controller) {
      pulls++
      controller.enqueue(chunk)
    },
  })
  const response = await serve(host)(new Request('ddp://app/api/uploads', { method: 'POST', body, duplex: 'half' }))
  assert.equal(response.status, 400)
  assert.equal((await response.json()).error.code, 'input_too_large')
  assert.ok(pulls < 70, `streaming cap must abort incrementally, pulled ${pulls} MiB`)
  assert.equal(calls.apiProxy, 0, 'rejected requests never reach the host proxy')
  await response.body?.cancel().catch(() => {})
})

test('a body under the cap still streams through intact', async () => {
  let seen
  const { host } = fakeHost({ apiProxy: async input => {
    seen = input
    return { status: 200, headers: {}, body: 'done', rewriteOrigins: [], sourceId: 'source-1' }
  } })
  const payload = JSON.stringify({ q: 'hello' })
  const response = await serve(host)(new Request('ddp://app/api/ask', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: payload, duplex: 'half' }))
  assert.equal(response.status, 200)
  assert.equal(Buffer.from(await response.arrayBuffer()).toString(), 'done')
  assert.equal(Buffer.from(seen.body).toString(), payload)
})

test('center JSON over the 16 MiB rewrite cap fails closed without buffering it all', async () => {
  let chunks = 0
  const big = new ReadableStream({
    pull(controller) {
      chunks++
      controller.enqueue(new Uint8Array(MIB).fill(65))
      if (chunks > 20) controller.close()
    },
  })
  const { host } = fakeHost({ apiProxy: async () => ({ status: 200,
    headers: { 'content-type': 'application/json' }, body: big,
    rewriteOrigins: ['https://center.test'], sourceId: 'source-1' }) })
  const response = await serve(host)(new Request('ddp://app/api/resources'))
  assert.equal(response.status, 400)
  assert.equal((await response.json()).error.code, 'protocol_incompatible')
  assert.ok(chunks < 20, `rewrite cap must abort incrementally, read ${chunks} MiB`)
})

test('a declared upstream Content-Length over the rewrite cap is refused without draining', async () => {
  // Streams fill one internal-queue chunk on their own; the declared check must
  // refuse after at most that, never by consuming the ~17 MiB body.
  let pulls = 0
  const big = new ReadableStream({ pull(controller) {
    pulls++
    if (pulls > 20) { controller.close(); return }
    controller.enqueue(new Uint8Array(MIB))
  } })
  const { host } = fakeHost({ apiProxy: async () => ({ status: 200,
    headers: { 'content-type': 'application/json', 'content-length': String(17 * MIB) }, body: big,
    rewriteOrigins: ['https://center.test'], sourceId: 'source-1' }) })
  const response = await serve(host)(new Request('ddp://app/api/resources'))
  assert.equal(response.status, 400)
  assert.equal((await response.json()).error.code, 'protocol_incompatible')
  assert.ok(pulls <= 1, `declared over-cap body must not be drained, pulled ${pulls} MiB`)
  await big.cancel().catch(() => {})
})

test('success proxy and object responses carry the security headers', async () => {
  const { host } = fakeHost()
  const proxied = await serve(host)(new Request('ddp://app/api/resources'))
  assert.equal(proxied.status, 200)
  assert.equal(proxied.headers.get('X-Content-Type-Options'), 'nosniff')
  assert.equal(proxied.headers.get('Cache-Control'), 'no-store')
  assert.ok((proxied.headers.get('Content-Security-Policy') ?? '').includes('default-src'))
  const object = await serve(host)(new Request('ddp://app/_object/obj-abc'))
  assert.equal(object.headers.get('X-Content-Type-Options'), 'nosniff')
  assert.equal(object.headers.get('Cache-Control'), 'no-store')
  assert.ok((object.headers.get('Content-Security-Policy') ?? '').includes('default-src'))
})

test('host error responses carry X-DDP-Source while a source is active', async () => {
  const { host } = fakeHost({ apiProxy: async () => {
    const { HostError } = await import('../src/policy.mjs')
    throw new HostError('invalid_arguments')
  } })
  const response = await serve(host)(new Request('ddp://app/api/resources'))
  assert.equal(response.status, 400)
  assert.equal(response.headers.get('X-DDP-Source'), 'source-1')
})

test('no active source means no X-DDP-Source on the error (renderer treats it as source_changed)', async () => {
  const { host } = fakeHost({ activeSourceId: () => null,
    apiProxy: async () => {
      const { HostError } = await import('../src/policy.mjs')
      throw new HostError('no_active_source')
    } })
  const response = await serve(host)(new Request('ddp://app/api/resources'))
  assert.equal(response.status, 503)
  assert.equal(response.headers.get('X-DDP-Source'), null)
})

test('packaged CSP helper is unchanged', () => {
  assert.match(contentSecurityPolicy(uiLocation()), /connect-src[^;]*ddp:\/\/app/)
}
)
