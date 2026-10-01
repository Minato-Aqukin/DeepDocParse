import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { createHash } from 'node:crypto'
import { uploadRemoteCompute } from '../src/file-transfer.mjs'
import { HostError } from '../src/policy.mjs'
import { OperationFault } from '../src/shared-client.mjs'

const FILE = Buffer.from('%PDF-1.7\n' + 'bounded original bytes'.repeat(4))
const HASH = createHash('sha256').update(FILE).digest('hex')
const TOKEN = 'synthetic-center-session-only'
const OBJECT = 'tmp-remote-compute/org/compute-1/source.bin'
const DESCRIPTION = Buffer.from(`manual.pdf\nsha256:${HASH}\n${FILE.length}\n`)
const PLAN = { plan_id: 'plan-1', scope_digest: 'sha256:' + 'a'.repeat(64), scope: {
  input_manifest: [{ ref: 'version-1', digest: 'sha256:' + HASH, size_bytes: FILE.length }], retention: 'temporary',
  payload_bindings: [{ phase: 'exploration', digest: 'sha256:' + createHash('sha256').update(DESCRIPTION).digest('hex'), size_bytes: DESCRIPTION.length }],
  transport_bindings: [{ transport_ref: 'center', recipient_node_id: 'node-center', endpoint: 'https://center.test' },
    { transport_ref: 'center-storage', recipient_node_id: 'node-center', endpoint: 'https://objects.test' }],
} }

async function fixture(t, flags = {}) {
  const writes = [], requests = [], stored = new Map(), permits = []
  let session = null, allocations = 0, finalized = 0, revoked = false, redirectedBytes = 0
  let journal = { scopeDigest: PLAN.scope_digest, createKey: 'stable-upload-creation-key', createAttempted: false,
    uploadId: null, remoteComputeId: null, phase: 'prepared', uploadedBytes: 0, totalBytes: FILE.length }
  const listen = async handler => {
    const server = createServer(handler)
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) })
    return 'http://127.0.0.1:' + server.address().port
  }
  const outside = await listen(async (req, res) => {
    for await (const chunk of req) redirectedBytes += chunk.length
    res.end()
  })
  const answer = (res, body) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body)) }
  const view = () => {
    const completed = [...stored].map(([number, bytes]) => ({ part_number: number, size: bytes.length, etag: 'part-' + number }))
    return { ...session, completed_parts: completed,
      parts: Array.from({ length: Math.ceil(FILE.length / 32) }, (_, i) => i + 1).filter(number => !stored.has(number)).map(number => ({
        part_number: number,
        url: `https://${flags.unapproved ? 'unapproved.test' : 'objects.test'}/bucket/${OBJECT}?partNumber=${number}&uploadId=multipart-1&X-Amz-Signature=test-signature`,
      })) }
  }
  const origin = await listen(async (req, res) => {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const bytes = Buffer.concat(chunks), url = new URL(req.url, 'http://local')
    requests.push({ route: url.pathname, method: req.method, authorization: req.headers.authorization, key: req.headers['idempotency-key'] })
    if (url.pathname === '/api/uploads') {
      assert.equal(req.headers.authorization, 'Bearer ' + TOKEN)
      const body = JSON.parse(bytes)
      allocations++
      session = { id: 'upload-1', purpose: body.purpose, remote_compute_id: body.remote_compute_id,
        filename: body.filename, mime: body.mime, declared_size: body.size, declared_sha256: body.sha256,
        status: 'uploading', allocation_state: 'ready', part_size: 32, object_key: OBJECT, expires_at: '2099-01-01T00:00:00Z' }
      return answer(res, view())
    }
    if (url.pathname === '/api/uploads/upload-1' || url.pathname === '/api/uploads/reconcile') return answer(res, view())
    if (url.pathname === '/api/uploads/upload-1/finalize') {
      finalized++
      const actual = Buffer.concat([...stored].sort(([a], [b]) => a - b).map(([, value]) => value))
      assert.deepEqual(actual, FILE)
      session = { ...session, status: 'ready', actual_size: actual.length, verified_sha256: createHash('sha256').update(actual).digest('hex') }
      return answer(res, view())
    }
    assert.equal(url.pathname, '/bucket/' + OBJECT)
    assert.equal(req.headers.authorization, undefined, 'object storage never receives the center session')
    if (flags.redirect) { res.writeHead(307, { Location: outside }); res.end(); return }
    const number = Number(url.searchParams.get('partNumber'))
    writes.push(number); stored.set(number, bytes)
    if (flags.dropFirst && number === 1) { flags.dropFirst = false; req.socket.destroy(); return }
    if (flags.revokeAfterFirst && number === 1) revoked = true
    res.setHeader('ETag', 'part-' + number); res.end()
  })
  const realFetch = globalThis.fetch
  // A real HTTP transport behind synthetic reviewed HTTPS identities: redirects,
  // dropped TCP responses, request bodies and headers are exercised, not echoed.
  t.mock.method(globalThis, 'fetch', async (input, options) => {
    const url = new URL(input)
    assert.ok(['center.test', 'objects.test'].includes(url.hostname), 'unreviewed origins never reach transport')
    return realFetch(origin + url.pathname + url.search, options)
  })
  const run = () => uploadRemoteCompute({ plan: PLAN, journal, signal: new AbortController().signal,
    checkpoint: async value => { journal = structuredClone(value) },
    readSource: async () => flags.changed ? Buffer.from('different bytes') : FILE,
    center: async () => ({ endpoint: 'https://center.test', uploadOrigin: 'https://objects.test', credential: TOKEN }),
    authorize: async (action, uploadId, offset, length) => {
      // The real host authorizer is Connection.query: ledger refusals arrive as OperationFault.
      if (revoked) throw new OperationFault('consent_revoked')
      permits.push({ action, uploadId, offset, length })
      return { plan_id: PLAN.plan_id, remote_compute_id: 'compute-1', input_ref: 'version-1', filename: 'manual.pdf',
        input_sha256: HASH, input_size: FILE.length, recipient_node_id: 'node-center', retention: 'temporary',
        action, upload_id: uploadId, upload_origin: 'https://objects.test' }
    },
  })
  return { run, writes, requests, permits, state: () => journal, allocations: () => allocations,
    finalized: () => finalized, redirectedBytes: () => redirectedBytes }
}

test('lost part response resumes the server-confirmed missing parts without a second allocation', async t => {
  const f = await fixture(t, { dropFirst: true })
  await assert.rejects(f.run(), { code: 'transfer_unknown' })
  assert.equal(f.state().uploadedBytes, 0, 'a lost receipt is not fabricated progress')
  assert.deepEqual(f.writes, [1])
  const result = await f.run()
  assert.equal(result.input_state, 'content_verified')
  assert.deepEqual(f.writes, [1, 2, 3, 4])
  assert.equal(f.allocations(), 1); assert.equal(f.finalized(), 1)
  assert.equal(f.state().uploadedBytes, FILE.length)
  assert.equal(JSON.stringify(f.state()).includes(TOKEN), false)
  assert.equal(JSON.stringify(f.state()).includes('test-signature'), false)
})

test('revocation between parts stops further bytes and never finalizes', async t => {
  const f = await fixture(t, { revokeAfterFirst: true })
  await assert.rejects(f.run(), { code: 'consent_revoked' })
  assert.deepEqual(f.writes, [1]); assert.equal(f.finalized(), 0)
  assert.equal(f.state().uploadedBytes, 32)
})

test('a center-issued presigned URL cannot move original bytes to an unreviewed origin', async t => {
  const f = await fixture(t, { unapproved: true })
  await assert.rejects(f.run(), { code: 'storage_origin_not_approved' })
  assert.deepEqual(f.writes, []); assert.equal(f.finalized(), 0)
})

test('an approved object endpoint cannot redirect the PUT to a third receiver', async t => {
  const f = await fixture(t, { redirect: true })
  await assert.rejects(f.run(), { code: 'transfer_unknown' })
  assert.equal(f.redirectedBytes(), 0); assert.equal(f.finalized(), 0)
})

test('changed source bytes fail before any remote metadata or content request', async t => {
  const f = await fixture(t, { changed: true })
  await assert.rejects(f.run(), { code: 'input_changed' })
  assert.deepEqual(f.requests, []); assert.deepEqual(f.permits, [])
})
