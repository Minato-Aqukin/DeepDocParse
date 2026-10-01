import { createHash } from 'node:crypto'
import { HostError } from './policy.mjs'

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
const fail = code => { throw new HostError(code) }

async function jsonResponse(response) {
  if ([401, 403, 410].includes(response.status)) {
    await response.body?.cancel()
    fail(response.status === 410 ? 'upload_expired' : 'authentication_required')
  }
  if (!response.body || !response.headers.get('content-type')?.includes('application/json')) {
    await response.body?.cancel(); fail('invalid_response')
  }
  const reader = response.body.getReader(), chunks = []
  let size = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.length
      if (size > 256 * 1024) fail('invalid_response')
      chunks.push(value)
    }
  } finally { await reader.cancel() }
  let value
  try { value = JSON.parse(Buffer.concat(chunks, size).toString('utf8')) }
  catch { fail('invalid_response') }
  if (!response.ok) {
    const code = value?.error?.code
    fail(typeof code === 'string' && /^[a-z][a-z0-9_]{0,95}$/.test(code) ? code : 'transfer_unknown')
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('invalid_response')
  return value
}

/**
 * One explicit, resumable transfer. The host owns the snapshot, credentials and
 * journal; the local consent ledger authorizes every individual HTTP attempt.
 * Nothing in the journal/result contains a credential or a presigned URL.
 */
export async function uploadRemoteCompute({ plan, journal, checkpoint, authorize, readSource, center, signal }) {
  const scope = plan.scope, input = scope.input_manifest?.[0]
  const descriptor = scope.payload_bindings?.find(item => item.phase === 'exploration')
  const transport = scope.transport_bindings?.find(item => item.transport_ref === 'center')
  const storage = scope.transport_bindings?.find(item => item.transport_ref === 'center-storage')
  if (scope.input_manifest?.length !== 1 || !input || !transport || !storage || !descriptor ||
      !ID.test(input.ref) || !/^sha256:[0-9a-f]{64}$/.test(input.digest) ||
      !Number.isSafeInteger(input.size_bytes) || input.size_bytes < 1 || input.size_bytes > 32 * 1024 * 1024)
    fail('input_changed')
  let state = journal, identity = null
  const save = async change => { state = { ...state, ...change }; await checkpoint(state) }
  const permit = async (action, offset = null, length = null) => {
    signal.throwIfAborted()
    let ticket
    try { ticket = await authorize(action, state.uploadId ?? null, offset, length) }
    catch (error) {
      // The ledger check precedes this action's center request: a refusal is a known
      // outcome with its own code (revoked, budget, protocol), never a lost receipt.
      const code = error?.code
      if (error instanceof HostError || typeof code !== 'string' || !/^[a-z][a-z0-9_]{0,95}$/.test(code)) throw error
      throw new HostError(code)
    }
    const description = Buffer.from(`${ticket.filename}\n${input.digest}\n${input.size_bytes}\n`)
    if (ticket.plan_id !== plan.plan_id || !ID.test(ticket.remote_compute_id) || ticket.input_ref !== input.ref ||
        ticket.input_sha256 !== input.digest.slice(7) || ticket.input_size !== input.size_bytes ||
        ticket.recipient_node_id !== transport.recipient_node_id || ticket.retention !== scope.retention ||
        ticket.upload_origin !== storage.endpoint || ticket.action !== action ||
        (state.uploadId && ticket.upload_id !== state.uploadId) ||
        'sha256:' + sha256(description) !== descriptor.digest || description.length !== descriptor.size_bytes)
      fail('plan_changed')
    if (state.remoteComputeId && state.remoteComputeId !== ticket.remote_compute_id) fail('plan_changed')
    const next = JSON.stringify([ticket.remote_compute_id, ticket.filename, ticket.input_sha256,
      ticket.input_ref, ticket.input_size, ticket.recipient_node_id, ticket.retention, ticket.upload_origin])
    if (identity && identity !== next) fail('plan_changed')
    identity = next
    return ticket
  }
  const control = async (action, route, { method = 'GET', body, key } = {}) => {
    const ticket = await permit(action)
    const target = await center(ticket)
    if (target.endpoint !== transport.endpoint || target.uploadOrigin !== storage.endpoint) fail('center_identity_changed')
    signal.throwIfAborted()
    const response = await fetch(target.endpoint + route, {
      method, headers: { Authorization: 'Bearer ' + target.credential, Accept: 'application/json',
        ...(body ? { 'Content-Type': 'application/json' } : {}), ...(key ? { 'Idempotency-Key': key } : {}) },
      body: body ? JSON.stringify(body(ticket)) : undefined,
      signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]), redirect: 'error',
      credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
    })
    return { session: await jsonResponse(response), ticket }
  }
  const checkSession = (session, ticket) => {
    if (!ID.test(session.id) || (state.uploadId && session.id !== state.uploadId) ||
        session.purpose !== 'temporary_compute' || session.remote_compute_id !== ticket.remote_compute_id ||
        session.filename !== ticket.filename || session.mime !== 'application/pdf' ||
        session.declared_size !== input.size_bytes || session.declared_sha256 !== input.digest.slice(7)) fail('input_changed')
    if (session.status === 'expired' || !(Date.parse(session.expires_at) > Date.now())) fail('upload_expired')
    if (session.status === 'failed') fail('upload_failed')
    if (!['created', 'uploading', 'verifying', 'ready'].includes(session.status)) fail('invalid_response')
  }
  const finish = async session => {
    if (session.status === 'ready' && (session.verified_sha256 !== input.digest.slice(7) || session.actual_size !== input.size_bytes))
      fail('input_changed')
    await save({ phase: session.status === 'ready' ? 'verified' : 'verifying', uploadedBytes: input.size_bytes, errorCode: null })
    return { upload_id: state.uploadId, remote_compute_id: state.remoteComputeId,
      input_state: session.status === 'ready' ? 'content_verified' : 'content_verifying' }
  }
  try {
    // This one bounded snapshot is the body source for every part; never reopen
    // a mutable path or trust a renderer-supplied digest for the bytes sent.
    const bytes = await readSource(input.ref)
    if (bytes.length !== input.size_bytes || sha256(bytes) !== input.digest.slice(7)) fail('input_changed')
    let found
    const create = async () => {
      await save({ phase: 'creating', createAttempted: true, errorCode: null })
      return control('create', '/api/uploads', { method: 'POST', key: state.createKey, body: ticket => ({
        filename: ticket.filename, size: input.size_bytes, mime: 'application/pdf', sha256: input.digest.slice(7),
        purpose: 'temporary_compute', remote_compute_id: ticket.remote_compute_id,
      }) })
    }
    if (state.uploadId) found = await control('resume', '/api/uploads/' + encodeURIComponent(state.uploadId))
    else if (state.createAttempted) {
      try { found = await control('resume', '/api/uploads/reconcile', { key: state.createKey }) }
      catch (error) {
        if (!(error instanceof HostError) || error.code !== 'no_such_upload') throw error
        // This is an explicit user resume, not a reconnect replay. The same
        // atomic creation key remains safe even if an earlier request is live.
        found = await create()
      }
    } else found = await create()
    let { session, ticket } = found
    checkSession(session, ticket)
    await save({ uploadId: session.id, remoteComputeId: ticket.remote_compute_id, totalBytes: input.size_bytes })
    if (['verifying', 'ready'].includes(session.status)) return await finish(session)
    if (session.allocation_state !== 'ready' || session.transfer_state === 'unknown') fail('transfer_unknown')
    if (session.transfer_state !== 'complete_pending_finalize') {
      const partSize = session.part_size, total = Math.ceil(input.size_bytes / partSize)
      if (!Number.isSafeInteger(partSize) || partSize < 1 || total < 1 || total > 10000 ||
          !Array.isArray(session.parts) || !Array.isArray(session.completed_parts)) fail('invalid_response')
      const seen = new Set(), pending = [], completed = []
      let uploaded = 0, multipart = null
      const range = number => {
        if (!Number.isInteger(number) || number < 1 || number > total || seen.has(number)) fail('invalid_response')
        seen.add(number)
        const start = (number - 1) * partSize
        return { start, length: Math.min(partSize, input.size_bytes - start) }
      }
      for (const part of session.completed_parts) {
        const piece = range(part.part_number)
        if (part.size !== piece.length || typeof part.etag !== 'string' || !part.etag) fail('invalid_response')
        uploaded += piece.length; completed.push(part.part_number)
      }
      if (typeof session.object_key !== 'string' || session.object_key.split('/').some(part => !part || part === '.' || part === '..'))
        fail('invalid_response')
      const suffix = '/' + session.object_key.split('/').map(encodeURIComponent).join('/')
      for (const part of session.parts) {
        const piece = range(part.part_number)
        if (typeof part.url !== 'string' || part.url.length > 16384) fail('invalid_response')
        let url
        try { url = new URL(part.url) } catch { fail('storage_origin_not_approved') }
        if (url.protocol !== 'https:' || url.origin !== storage.endpoint || url.username || url.password || url.hash ||
            !url.pathname.endsWith(suffix) || url.searchParams.get('partNumber') !== String(part.part_number) ||
            !url.searchParams.get('uploadId')) fail('storage_origin_not_approved')
        const binding = JSON.stringify([url.origin, url.pathname, url.searchParams.get('uploadId')])
        if (multipart && multipart !== binding) fail('invalid_response')
        multipart = binding; pending.push({ ...piece, number: part.part_number, url: part.url })
      }
      if (seen.size !== total) fail('upload_incomplete')
      await save({ phase: 'uploading', uploadedBytes: uploaded, completedParts: completed, errorCode: null })
      for (const part of pending.sort((a, b) => a.number - b.number)) {
        ticket = await permit('part', part.start, part.length)
        // Recheck the paired center generation too; only the presigned object
        // URL receives bytes, and it never receives the center's Bearer token.
        const target = await center(ticket)
        if (target.endpoint !== transport.endpoint || target.uploadOrigin !== storage.endpoint) fail('center_identity_changed')
        signal.throwIfAborted()
        const response = await fetch(part.url, { method: 'PUT', body: bytes.subarray(part.start, part.start + part.length),
          signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]), redirect: 'error', credentials: 'omit',
          cache: 'no-store', referrerPolicy: 'no-referrer' })
        await response.body?.cancel()
        if (!response.ok) fail('transfer_unknown')
        uploaded += part.length; completed.push(part.number)
        await save({ uploadedBytes: uploaded, completedParts: [...completed] })
      }
    }
    // The server lists its actual S3 parts, so a missing/lost ETag never turns
    // into a fabricated completion list or a second multipart allocation.
    ;({ session, ticket } = await control('finalize', '/api/uploads/' + encodeURIComponent(state.uploadId) + '/finalize', {
      method: 'POST', key: state.createKey, body: () => ({}),
    }))
    checkSession(session, ticket)
    if (!['verifying', 'ready'].includes(session.status)) fail('transfer_unknown')
    return await finish(session)
  } catch (error) {
    const code = error instanceof HostError ? error.code : 'transfer_unknown'
    await save({ phase: 'unknown', errorCode: code })
    throw new HostError(code)
  }
}
