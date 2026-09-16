import { HostError, object } from './policy.mjs'
export const CLIENT_CHANNELS = Object.freeze(Object.fromEntries([
  'clientList', 'clientConnectLocal', 'clientPairRemote', 'clientWake', 'clientDisconnect',
  'clientSubscribe', 'clientUnsubscribe', 'clientCommand', 'clientQuery', 'clientImportFile', 'clientExportBundle', 'clientReadOriginal', 'clientReceipt', 'clientReadDraft', 'clientSaveDraft',
  'clientPlanPropose', 'clientPlanList', 'clientPlanGet', 'clientPlanApprove', 'clientPlanRevoke',
  'clientPlanDispatch', 'clientPlanReconcile', 'clientPlanFetchDelivery', 'clientPlanConfirmDelivery',
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
  clientPlanList: [], clientPlanGet: ['planId'], clientPlanReconcile: ['planId'], clientPlanFetchDelivery: ['planId'],
  clientPlanApprove: ['planId', 'phase', 'scopeDigest', 'userConfirmed', 'idempotencyKey'],
  clientPlanRevoke: ['planId', 'idempotencyKey'],
  clientPlanDispatch: ['planId', 'phase', 'idempotencyKey'],
  clientPlanConfirmDelivery: ['planId', 'deliveryId', 'resultManifestDigest', 'idempotencyKey'],
})
function planArguments(method, input) {
  object(input, ['connectionId', ...PLAN_FIELDS[method]]); id(input.connectionId)
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
  if (method === 'clientPlanPropose') {
    id(input.centerConnectionId)
    if (typeof input.query !== 'string' || !input.query.trim() || input.query.length > 4096
        || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(input.query)) fail()
    if (!['temporary', 'task_pinned'].includes(input.retention)) fail()
    if (!Number.isInteger(input.validMinutes) || input.validMinutes < 5 || input.validMinutes > 1440) fail()
    if (!Array.isArray(input.inputs) || input.inputs.length > 20) fail()
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
  if (method === 'clientConnectLocal') { object(input, ['workspaceId']); id(input.workspaceId); return input }
  if (method === 'clientPairRemote') {
    object(input, ['environment', 'profile', 'label'])
    object(input.environment, ['environmentId', 'workspaceId', 'authorityNodeId', 'endpoint'])
    object(input.profile, ['profileId', 'issuer', 'subject'])
    for (const key of ['environmentId', 'workspaceId', 'authorityNodeId']) id(input.environment[key], 128)
    for (const key of ['profileId', 'issuer', 'subject']) id(input.profile[key], 128)
    if (typeof input.label !== 'string' || !input.label.trim() || input.label.length > 128 || /[\x00-\x1f]/.test(input.label)) fail()
    const url = new URL(input.environment.endpoint)
    if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.search
        || input.environment.environmentId !== input.environment.authorityNodeId) fail()
    return { ...input, environment: { ...input.environment, endpoint: url.href.replace(/\/$/, '') } }
  }
  if (method === 'clientUnsubscribe') { object(input, ['subscriptionId']); id(input.subscriptionId); return input }
  if (Object.hasOwn(PLAN_FIELDS, method)) return planArguments(method, input)
  const fields = { clientWake: [], clientDisconnect: [], clientSubscribe: ['subscriptionId'],
    clientCommand: ['name', 'payload', 'idempotencyKey'], clientReceipt: ['idempotencyKey'],
    clientQuery: ['name', 'payload'], clientImportFile: ['kind', 'idempotencyKey'],
    clientExportBundle: ['versionId'], clientReadOriginal: ['versionId'], clientReadDraft: ['key'], clientSaveDraft: ['key', 'expectedRevision', 'value'] }[method]
  if (!fields) fail()
  object(input, ['connectionId', ...fields]); id(input.connectionId)
  if (fields.includes('subscriptionId')) id(input.subscriptionId)
  if (fields.includes('idempotencyKey') && (typeof input.idempotencyKey !== 'string'
      || !/^[A-Za-z0-9_-]{8,128}$/.test(input.idempotencyKey))) fail()
  if (fields.includes('key')) id(input.key)
  if (fields.includes('versionId')) id(input.versionId)
  if (method === 'clientImportFile' && !['pdf', 'bundle'].includes(input.kind)) fail()
  if (method === 'clientQuery') {
    if (input.name === 'models.list') object(input.payload, [])
    else if (['wiki.list', 'wiki.get', 'wiki.revisions'].includes(input.name)) {
      const body = input.payload
      object(body, Object.keys(body))
      const allowed = input.name === 'wiki.get' ? ['wiki_id', 'revision_id']
        : ['cursor', 'limit', ...(input.name === 'wiki.revisions' ? ['wiki_id'] : [])]
      if (Object.keys(body).some(key => !allowed.includes(key))) fail()
      if (input.name !== 'wiki.list') id(body.wiki_id)
      if (body.revision_id !== undefined) id(body.revision_id)
      if (body.limit !== undefined && (!Number.isInteger(body.limit) || body.limit < 1 || body.limit > 100)) fail()
      if (body.cursor !== undefined && (typeof body.cursor !== 'string' || !body.cursor || body.cursor.length > 4096 || /[\x00-\x1f\x7f]/.test(body.cursor))) fail()
    }
    else if (input.name === 'evidence.get') {
      object(input.payload, ['evidence_id'])
      if (typeof input.payload.evidence_id !== 'string' || !/^[A-Za-z0-9_:.\/-]{1,512}$/.test(input.payload.evidence_id)) fail()
    }
    else if (['resource.page', 'task.page'].includes(input.name)) {
      object(input.payload, ['snapshot_id', 'cursor'])
      for (const field of ['snapshot_id', 'cursor']) {
        if (typeof input.payload[field] !== 'string' || !input.payload[field] || input.payload[field].length > 4096
            || /[\x00-\x1f\x7f]/.test(input.payload[field])) fail()
      }
    }
    else if (input.name === 'corpus.search') {
      object(input.payload, Object.keys(input.payload))
      if (Object.keys(input.payload).some(key => !['query', 'version_ids', 'limit'].includes(key))) fail()
      input = { ...input, payload: { version_ids: null, limit: 10, ...input.payload } }
      if (typeof input.payload.query !== 'string' || !input.payload.query.trim() || input.payload.query.length > 4096
          || (input.payload.version_ids !== null && (!Array.isArray(input.payload.version_ids) || input.payload.version_ids.length > 100))
          || !Number.isInteger(input.payload.limit) || input.payload.limit < 1 || input.payload.limit > 100) fail()
      for (const version of input.payload.version_ids ?? []) id(version)
    } else throw new HostError('unsupported_operation')
  }
  if (method === 'clientSaveDraft') {
    if (!Number.isSafeInteger(input.expectedRevision) || input.expectedRevision < 0) fail()
    json(input.value, 1024 * 1024)
  }
  if (method === 'clientCommand') {
    const body = input.payload
    if (input.name === 'models.stop') object(body, [])
    else if (['models.install', 'models.start'].includes(input.name)) {
      object(body, ['model_id'])
      if (typeof body.model_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(body.model_id)) fail()
    }
    else if (input.name === 'task.cancel') { object(body, ['task_id']); id(body.task_id) }
    else if (['wiki.create', 'wiki.rebuild', 'wiki.edit'].includes(input.name)) {
      object(body, input.name === 'wiki.create' ? ['body'] : input.name === 'wiki.rebuild' ? ['wiki_id', 'body'] : ['wiki_id', 'page_key', 'body'])
      if (input.name !== 'wiki.create') id(body.wiki_id)
      const value = body.body
      if (input.name === 'wiki.edit') {
        id(body.page_key); object(value, ['base_revision_id', 'paragraphs']); id(value.base_revision_id)
        if (!Array.isArray(value.paragraphs) || value.paragraphs.length > 100) fail()
        for (const item of value.paragraphs) {
          object(item, ['id', 'text']); id(item.id, 64)
          if (typeof item.text !== 'string' || !item.text.trim() || item.text.length > 10000) fail()
        }
      } else {
        object(value, Object.keys(value))
        if (Object.keys(value).some(key => !['title', 'sources', 'max_pages', 'max_evidence', 'max_output_tokens', 'max_input_chars', 'execution_policy', 'allow_remote', ...(input.name === 'wiki.rebuild' ? ['base_revision_id'] : [])].includes(key))) fail()
        if (input.name === 'wiki.rebuild') id(value.base_revision_id)
        if (typeof value.title !== 'string' || !value.title.trim() || value.title.length > 255 || !Array.isArray(value.sources) || !value.sources.length || value.sources.length > 50 || value.execution_policy !== 'local_only' || value.allow_remote !== false) fail()
        for (const source of value.sources) { object(source, ['resource_id', 'source_version_id']); id(source.resource_id); id(source.source_version_id) }
        for (const [key, low, high] of [['max_pages', 1, 12], ['max_evidence', 1, 200], ['max_output_tokens', 512, 8192], ['max_input_chars', 1000, 50000]]) {
          if (value[key] !== undefined && (!Number.isInteger(value[key]) || value[key] < low || value[key] > high)) fail()
        }
      }
    }
    else if (['answer.generate', 'wiki.build'].includes(input.name)) {
      object(body, ['query', 'version_ids', 'execution_policy', 'allow_remote'])
      if (typeof body.query !== 'string' || !body.query.trim() || body.query.length > 4096
          || (body.version_ids !== null && (!Array.isArray(body.version_ids) || body.version_ids.length > 100))
          || body.execution_policy !== 'local_only' || body.allow_remote !== false) fail()
      for (const version of body.version_ids ?? []) id(version)
    } else throw new HostError('unsupported_operation')
    json(body, 65536)
  }
  return input
}
