import { HostError, object } from './policy.mjs'
export const CLIENT_CHANNELS = Object.freeze(Object.fromEntries([
  'clientList', 'clientCommand', 'clientQuery', 'clientReceipt', 'clientReadDraft', 'clientSaveDraft',
  'clientPlanPropose', 'clientPlanProposeFile', 'clientPlanList', 'clientPlanGet', 'clientPlanApprove', 'clientPlanRevoke',
  'clientPlanReviewCenter',
  'clientPlanDispatch', 'clientPlanResume', 'clientPlanReconcile', 'clientPlanFetchDelivery', 'clientPlanConfirmDelivery',
  'clientPlanCancel',
].map(name => [name, 'ddp:' + name])))
const fail = () => { throw new HostError('invalid_arguments') }
const DIGEST = /^sha256:[0-9a-f]{64}$/
/**
 * Plan operations. The renderer names a local connection, a paired center connection
 * and imported versions; it never supplies a TaskSpec, TaskPlan, node, endpoint, path,
 * credential or payload binding. Unknown fields fail before any host work.
 */
const PLAN_FIELDS = Object.freeze({
  clientPlanPropose: ['centerConnectionId', 'query', 'inputs', 'retention', 'validMinutes', 'idempotencyKey'],
  clientPlanProposeFile: ['centerConnectionId', 'filename', 'inputs', 'retention', 'validMinutes', 'idempotencyKey'],
  clientPlanList: [], clientPlanGet: ['planId'], clientPlanReconcile: ['planId'], clientPlanFetchDelivery: ['planId'],
  clientPlanApprove: ['planId', 'phase', 'scopeDigest', 'userConfirmed', 'idempotencyKey'],
  clientPlanReviewCenter: ['planId', 'idempotencyKey'],
  clientPlanRevoke: ['planId', 'idempotencyKey'],
  clientPlanCancel: ['planId', 'idempotencyKey'],
  clientPlanDispatch: ['planId', 'phase', 'idempotencyKey'],
  clientPlanResume: ['planId', 'idempotencyKey'],
  clientPlanConfirmDelivery: ['planId', 'deliveryId', 'resultManifestDigest', 'idempotencyKey'],
})
function planArguments(method, input) {
  object(input, ['connectionId', ...PLAN_FIELDS[method],
    ...(method === 'clientPlanPropose' && Object.hasOwn(input ?? {}, 'template') ? ['template'] : []),
    ...(method === 'clientPlanPropose' && Object.hasOwn(input ?? {}, 'purpose') ? ['purpose'] : []),
    ...(method === 'clientPlanPropose' && Object.hasOwn(input ?? {}, 'wiki') ? ['wiki'] : []),
    ...(method === 'clientPlanPropose' && Object.hasOwn(input ?? {}, 'participantConnectionIds') ? ['participantConnectionIds'] : [])])
  id(input.connectionId)
  if (Object.hasOwn(input, 'idempotencyKey') && (typeof input.idempotencyKey !== 'string'
      || !/^[A-Za-z0-9_-]{8,128}$/.test(input.idempotencyKey))) fail()
  if (Object.hasOwn(input, 'planId')) id(input.planId)
  if (Object.hasOwn(input, 'phase') && !['exploration', 'execution'].includes(input.phase)) fail()
  if (method === 'clientPlanApprove' && (input.userConfirmed !== true || typeof input.scopeDigest !== 'string'
      || !DIGEST.test(input.scopeDigest))) fail()
  if (method === 'clientPlanConfirmDelivery') {
    id(input.deliveryId, 255)
    if (typeof input.resultManifestDigest !== 'string' || !DIGEST.test(input.resultManifestDigest)) fail()
  }
  if (method === 'clientPlanPropose' || method === 'clientPlanProposeFile') {
    id(input.centerConnectionId)
    if (method === 'clientPlanPropose') {
      if (typeof input.query !== 'string' || !input.query.trim() || input.query.length > 4096
          || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(input.query)) fail()
      // Typed intent, same names and bounds as the host and local HTTP template.
      const purpose = Object.hasOwn(input, 'purpose') ? input.purpose : 'answer'
      if (purpose !== 'answer' && purpose !== 'wiki') fail()
      const hasWiki = Object.hasOwn(input, 'wiki')
      if ((purpose === 'wiki') !== hasWiki) fail()
      if (hasWiki) {
        const wiki = input.wiki
        object(wiki, Object.hasOwn(wiki ?? {}, 'max_pages') ? ['title', 'max_pages'] : ['title'])
        if (typeof wiki.title !== 'string' || !wiki.title.trim() || wiki.title.length > 255
            || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(wiki.title)) fail()
        if (wiki.max_pages !== undefined && (!Number.isInteger(wiki.max_pages) || wiki.max_pages < 1 || wiki.max_pages > 12)) fail()
      }
      if (input.template !== undefined && !['center_only', 'trusted_federation'].includes(input.template)) fail()
      if (input.participantConnectionIds !== undefined) {
        if (!Array.isArray(input.participantConnectionIds) || input.participantConnectionIds.length > 99 ||
            new Set(input.participantConnectionIds).size !== input.participantConnectionIds.length ||
            (input.template !== 'trusted_federation' && input.participantConnectionIds.length)) fail()
        for (const connection of input.participantConnectionIds) id(connection)
      }
    } else if (typeof input.filename !== 'string' || !input.filename.trim() || input.filename.length > 255
        || /[\\/\x00-\x1f\x7f]/.test(input.filename) || ['.', '..'].includes(input.filename)) fail()
    if (!['temporary', 'task_pinned'].includes(input.retention)) fail()
    if (!Number.isInteger(input.validMinutes) || input.validMinutes < 5 || input.validMinutes > 1440) fail()
    if (!Array.isArray(input.inputs) || input.inputs.length > 20) fail()
    if (method === 'clientPlanProposeFile' && input.inputs.length !== 1) fail()
    const refs = new Set()
    for (const item of input.inputs) {
      object(item, ['ref', 'digest', 'sizeBytes']); id(item.ref)
      if (typeof item.digest !== 'string' || !DIGEST.test(item.digest) || !Number.isSafeInteger(item.sizeBytes)
          || item.sizeBytes < 1 || item.sizeBytes > 32 * 1024 * 1024 || refs.has(item.ref)) fail()
      refs.add(item.ref)
    }
  }
  return input
}
function id(value, maximum = 128) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(value) || value.length > maximum) fail()
}
function json(value, maximum) {
  try { if (Buffer.byteLength(JSON.stringify(value)) > maximum) fail() }
  catch { fail() }
}
export function clientArguments(method, input) {
  if (method === 'clientList') { if (input !== undefined && input !== null) fail(); return undefined }
  if (Object.hasOwn(PLAN_FIELDS, method)) return planArguments(method, input)
  const fields = { clientCommand: ['name', 'payload', 'idempotencyKey'], clientReceipt: ['idempotencyKey'],
    clientQuery: ['name', 'payload'], clientReadDraft: ['key'], clientSaveDraft: ['key', 'expectedRevision', 'value'] }[method]
  if (!fields) fail()
  object(input, ['connectionId', ...fields]); id(input.connectionId)
  if (fields.includes('idempotencyKey') && (typeof input.idempotencyKey !== 'string'
      || !/^[A-Za-z0-9_-]{8,128}$/.test(input.idempotencyKey))) fail()
  if (fields.includes('key')) id(input.key)
  if (method === 'clientQuery') {
    // The only renderer-side read left is models.list (LocalModelsView); content reads
    // (resources, search, evidence, Wiki) go through the /api proxy.
    if (input.name === 'models.list') object(input.payload, [])
    else throw new HostError('unsupported_operation')
  }
  if (method === 'clientCommand') {
    // Renderer-side writes left: local model management (LocalModelsView). Content
    // writes (uploads, Wiki, answers, deletions) go through the /api proxy.
    const body = input.payload
    if (input.name === 'models.stop') object(body, [])
    else if (['models.install', 'models.start'].includes(input.name)) {
      object(body, ['model_id', ...(input.name === 'models.start' && Object.hasOwn(body ?? {}, 'runtime_id') ? ['runtime_id'] : [])])
      if (typeof body.model_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(body.model_id)) fail()
      if (Object.hasOwn(body, 'runtime_id') && (typeof body.runtime_id !== 'string' || !/^[a-z0-9][a-z0-9_.-]{0,95}$/.test(body.runtime_id))) fail()
    } else throw new HostError('unsupported_operation')
    json(body, 65536)
  }
  return input
}
