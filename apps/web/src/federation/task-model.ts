/**
 * 联邦任务的前端模型 —— 形状对照 `packages/contracts/openapi/federation-tasks-v1.yaml`
 * 与 `schemas/ddp-{task-probe,plan-admission,scope-coverage,evidence}/v1.json`。
 *
 * 这里只有类型与**纯函数**（放在组件外面才能单测）。前端不在这里另写一份枚举：
 * 取值类型全部来自 `@deepdocparse/contracts` 的生成物。
 */
import { TASK_STATUS_META } from '@deepdocparse/contracts'
import type {
  FederationTaskOperation,
  CoverageTargetState,
  DeliveryState,
  EnumerationState,
  EvidenceConflictBasis,
  EvidenceSufficiency,
  PlanningState,
  RetentionClass,
  RetrievalCompleteness,
  SearchMode,
  TaskStatus as TaskStatusValue,
  ValidationState,
} from '@deepdocparse/contracts'

export type ScopeKind = 'site_public' | 'federation_public' | 'fixed_resources'
/** 协调者受理的 operation 闭集，取值来自契约生成物，不在前端另写一份。 */
export type TaskOperation = FederationTaskOperation
export type ProbePayload = 'query_text' | 'subquery_text' | 'entity_names' | 'resource_names'
  | 'collection_filters' | 'evidence_excerpts' | 'source_files'

export interface TargetKey { origin_node_id: string; collection_id: string; operation: string }

export interface ScopeManifest {
  schema: 'ddp-scope-coverage/1#ScopeManifest'
  scope_id: string
  caller_scope_hash: string
  created_at: string
  valid_until: string
  registry_revision_vector: { node_id: string; registry_revision: number; fetched_at: string }[]
  expanded_members: TargetKey[]
  unexpanded_subtrees: { node_id: string; reason: string }[]
  enumeration_state: EnumerationState
  manifest_digest: string
  child_manifests?: { node_id: string; scope_ref: string; enumeration_state: EnumerationState }[]
}

/** `POST /api/v1/federation/scopes` 的响应（scope-v1.yaml#ScopeEnvelope）。 */
export interface ScopeEnvelope {
  manifest: ScopeManifest
  first_cursor: string
  terminal_cursor: string
  total_targets: number
  content_snapshot: string
  expired: boolean
  effective_enumeration_state: EnumerationState
}

export interface TaskSpec {
  schema: 'ddp-task-probe/1#TaskSpec'
  protocol: 'ddp-task/1'
  operation: TaskOperation
  workspace_ref: string
  query: string
  resource_scope: { kind: ScopeKind; scope_ref?: string; resource_refs?: string[] }
  search_policy: { mode: SearchMode; ordering: 'local_first' }
  execution_policy: { mode: 'local_only' | 'trusted_federation'; coordinator_ref?: string }
  consent_refs: { exploration: string; execution: null }
  requirements?: { citations: 'required' | 'not_required' }
  budget_ref: string
}

export interface ExplorationConsent {
  schema: 'ddp-task-probe/1#ExplorationConsent'
  consent_id: string
  granted_by: string
  granted_at: string
  valid_until: string
  egress_mode: 'local_only' | 'listed_nodes'
  allowed_payload: ProbePayload[]
  allowed_recipients: string[]
  budget: { max_probe_requests: number; max_egress_bytes: number; max_discovery_requests?: number }
}

export interface PlanStep {
  step_id: string
  operation: string
  executor_node_id: string
  depends_on: string[]
  fixed_inputs?: string[]
  probe_refs?: string[]
}

export interface DataEdge {
  edge_id: string
  from_node_id: string
  to_node_id: string
  payload_kind: string
  relay_via?: string[]
  retention: RetentionClass
  authorised_by: string
}

export interface TaskPlan {
  schema: 'ddp-plan-admission/1#TaskPlan'
  plan_id: string
  revision: number
  plan_digest: string
  task_spec_digest: string
  root_coordinator_node_id: string
  planning_state: PlanningState
  steps: PlanStep[]
  data_edges: DataEdge[]
  execution_consent_ref?: string | null
  budget: { max_requests: number; max_bytes: number; max_generation_tokens?: number; max_hops: number; deadline: string }
  final_result_writer: string
  valid_until: string
}

export interface ExecutionConsent {
  schema: 'ddp-plan-admission/1#ExecutionConsent'
  consent_id: string
  plan_digest: string
  granted_by: string
  granted_at: string
  valid_until: string
  allowed_recipients: string[]
  allowed_edges: string[]
  retention: RetentionClass
}

export interface TaskIntent {
  root_task_id: string
  task_spec_digest: string
  planning_state: PlanningState
  status: TaskStatusValue
  task_spec: TaskSpec
  exploration_consent: ExplorationConsent
  created_at: string
  updated_at: string | null
}

export interface Locator {
  kind: string
  physical_page_index?: number
  seq?: number
  bbox?: number[]
  page_size?: { width: number; height: number }
}

export interface FederatedEvidence {
  evidence_id: string
  origin_node_id: string
  authority_node_id: string
  resource_id: string
  source_version_id: string
  parse_revision: string
  source_digest: string
  excerpt_digest: string
  locator: Locator
  source_type: string
  block_type?: string
  policy_revision: string
}

export interface ClaimBinding {
  claim_id: string
  claim_text: string
  evidence_refs: string[]
  structural_validation: ValidationState
  semantic_review?: ValidationState
}

export interface EvidenceConflict {
  basis: EvidenceConflictBasis
  evidence_refs: string[]
  semantic_review: ValidationState
}

export interface UnretrievedTarget { target_key: TargetKey; state: CoverageTargetState; last_error: string | null }

/** 结果文档（交付字节的规范原文 + 摘要）。字段集随协调者结果走，界面只读已知字段。 */
export interface TaskResult {
  answer: string | null
  answer_reason: string | null
  claim_evidence_bindings: ClaimBinding[]
  conflicts: EvidenceConflict[]
  provider: { model: string; endpoint?: string | null; location: string } | null
  disclosure: { remote: boolean; payload: string[] }
  validation_state: ValidationState
  operation?: string
  search_mode?: SearchMode
  retrieval_completeness: RetrievalCompleteness
  evidence_sufficiency: EvidenceSufficiency
  counts?: CoverageCounts
  evidence: FederatedEvidence[]
  unretrieved_targets: UnretrievedTarget[]
  result_manifest_digest?: string
}

export interface TaskStatus {
  root_task_id: string
  status: TaskStatusValue
  planning_state: PlanningState
  plan_revision: number
  plan_digest: string | null
  task_spec_digest: string
  search_mode: SearchMode
  retrieval_completeness: RetrievalCompleteness
  evidence_sufficiency: EvidenceSufficiency
  execution_consent_ref: string | null
  coverage_ref: string | null
  delivery_id: string | null
  delivery_state: DeliveryState
  result: TaskResult | null
  error: string | null
  scope_ref?: string | null
  created_at: string
  updated_at: string
}

export interface CoverageCounts {
  total_targets: number
  applicable_targets: number
  succeeded: number
  excluded: number
  incomplete: number
}

export interface CoverageEntry {
  target_key: TargetKey
  scope_ref: string
  state: CoverageTargetState
  attempts: number
  probe_receipts?: string[]
  actual_index_revision?: string | null
  search_profile?: string | null
  last_error?: string | null
  exclusion_basis?: string | null
}

export interface CoverageLedger {
  root_task_id: string
  scope_ref: string
  search_mode: SearchMode
  enumeration_state: EnumerationState
  retrieval_completeness: RetrievalCompleteness
  evidence_sufficiency: EvidenceSufficiency
  entries: CoverageEntry[]
  counts: CoverageCounts
  conflicts?: EvidenceConflict[]
}

export interface TaskEvent { seq: number; type: string; at: string; payload?: Record<string, unknown> | null }
export interface EventPage { root_task_id: string; events: TaskEvent[]; next_seq: number; complete: boolean }

export interface TaskListItem {
  root_task_id: string
  query: string
  operation: string
  scope_kind: ScopeKind | 'local_only'
  search_mode: SearchMode
  status: TaskStatusValue
  planning_state: PlanningState
  retrieval_completeness: RetrievalCompleteness
  evidence_sufficiency: EvidenceSufficiency
  delivery_state: DeliveryState
  created_at: string
  updated_at: string
}
export interface TaskListPage { items: TaskListItem[]; next_cursor: string | null }

// ---------------------------------------------------------------- 读取辅助

/**
 * 任务是否落定（不再需要轮询）。**以契约的 `active` 标记为准**，不在这里手写终态清单 ——
 * 契约加一个进行中状态时，手写清单会把它当成终态、轮询提前停下。
 * 契约里没有的状态值按落定处理：界面显示"未知取值"，不对一个认不出的状态无限轮询。
 */
export function isSettled(status: Pick<TaskStatus, 'status'>): boolean {
  const meta = TASK_STATUS_META[status.status as TaskStatusValue] as { active?: boolean } | undefined
  return meta ? !meta.active : true
}

/** `receipt_binding_mismatch:plan_digest` → 代码 + 细节。 */
export function splitReason(reason: string): { code: string; detail: string | null } {
  const index = reason.indexOf(':')
  return index < 0 ? { code: reason, detail: null } : { code: reason.slice(0, index), detail: reason.slice(index + 1) }
}

/**
 * 续读事件：从 `after` 开始按页拿，直到追平（`complete`）或这一页是空的。
 *
 * - **按 seq 去重**：重连后服务端可能把同一段再给一次，界面不能出现两条同号事件。
 * - **一次最多 `cap` 页**：事件异常多时下个轮询周期接着读，不在一次刷新里无界循环。
 * - 形状不对当场抛：`next_seq` 不是数字时继续读会变成 `after=undefined`，那等于每轮
 *   都从头拉一遍（读起来像"事件在重复"）。
 */
export async function collectEvents(
  read: (after: number) => Promise<EventPage>,
  from: number,
  known: Iterable<number>,
  options: {
    cap?: number
    /** 每页之间问一次"这次续读还算数吗"：路由切走后立刻停，不再白打最多 cap 次请求。 */
    stale?: () => boolean
  } = {},
): Promise<{ events: TaskEvent[]; next: number }> {
  const { cap = 20, stale } = options
  const seen = new Set(known)
  const collected: TaskEvent[] = []
  let next = from
  for (let page = 0; page < cap; page++) {
    const body = await read(next)
    if (!Array.isArray(body?.events) || typeof body.next_seq !== 'number') {
      throw new Error('事件流格式不兼容')
    }
    for (const event of body.events) {
      if (seen.has(event.seq)) continue
      seen.add(event.seq)
      collected.push(event)
    }
    next = body.next_seq
    if (body.complete || !body.events.length || stale?.()) break
  }
  return { events: collected, next }
}

/** 答案里的 `[n]` 按证据列表位置对应（生成时的编号域就是 result.evidence 的顺序）。 */
export function citationIndex(evidence: FederatedEvidence[]): Map<string, number> {
  return new Map(evidence.map((item, index) => [item.evidence_id, index + 1]))
}

// ---------------------------------------------------------------- 组装

export interface TaskDraft {
  query: string
  operation: TaskOperation
  scopeKind: ScopeKind
  /** federation_public 必须先取得的范围清单；其余范围由协调者本地枚举 */
  scope?: ScopeEnvelope
  resourceRefs: string[]
  mode: SearchMode
  /** 允许发给远端的内容。**问题本身也是外发**（ExplorationConsent.allowed_payload） */
  payload: ProbePayload[]
  maxProbeRequests: number
  maxEgressBytes: number
}

const MINUTE = 60_000

function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** 范围里不属于本节点的来源节点（排序去重）。本地目标不走外发许可门。 */
export function remoteNodesOf(scope: ScopeEnvelope | undefined, localNodeId: string): string[] {
  if (!scope) return []
  const nodes = new Set(scope.manifest.expanded_members.map((member) => member.origin_node_id))
  nodes.delete(localNodeId)
  return [...nodes].sort()
}

/** 穷查必须绑定一个已封存的范围引用（TaskSpec allOf）：只有联邦范围清单能给出分母。 */
export function exhaustiveAllowed(draft: Pick<TaskDraft, 'scopeKind' | 'scope'>): boolean {
  return draft.scopeKind === 'federation_public' && !!draft.scope
}

export interface IntentBody {
  task_spec: TaskSpec
  exploration_consent: ExplorationConsent
  scope_manifest?: ScopeManifest
}

/**
 * 按用户在界面上的选择生成 TaskIntentInput。
 *
 * 两个"看起来可以合并、实际必须分开"的判断：
 *
 * - **执行策略看范围种类，不看有没有远端目标。** `local_only` 执行禁止
 *   `federation_public` 范围（`plans.validate_spec`），而联邦范围完全可能一个
 *   远端成员都没有（清单里只有本节点）。按"有没有远端"选执行策略的话，这种范围
 *   会被协调者当场拒掉。
 * - **外发许可看有没有远端接收方，不看范围种类。** 没有远端接收方就必须
 *   `local_only` + 载荷/接收方/预算全空（契约 allOf，I03）。
 *
 * 用户不勾"问题原文"时照样生成，协调者会把远端目标如实记成 denied ——
 * 界面不替用户偷偷补上外发项。
 */
export function buildIntent(draft: TaskDraft, options: {
  localNodeId: string
  grantedBy: string
  workspaceRef: string
  nonce: string
  now: number
  validMinutes?: number
}): IntentBody {
  const query = draft.query.trim()
  if (!query) throw new Error('问题不能为空')
  if (!options.localNodeId) throw new Error('还没拿到本节点身份，请稍后重试')
  if (!options.grantedBy || !options.workspaceRef) throw new Error('还没拿到账号信息，请稍后重试')
  if (draft.scopeKind === 'federation_public' && !draft.scope) {
    throw new Error('联邦范围需要先生成范围清单')
  }
  if (draft.scopeKind === 'fixed_resources' && draft.resourceRefs.length === 0) {
    throw new Error('至少选择一份资源')
  }
  if (draft.mode === 'exhaustive_scope' && !exhaustiveAllowed(draft)) {
    throw new Error('穷查只能绑定已生成的联邦范围清单')
  }
  const federated = draft.scopeKind === 'federation_public'
  const remote = federated ? remoteNodesOf(draft.scope, options.localNodeId) : []
  const explorationId = `explore-${options.nonce}`
  const resourceScope: TaskSpec['resource_scope'] = { kind: draft.scopeKind }
  if (federated && draft.scope) resourceScope.scope_ref = draft.scope.manifest.scope_id
  if (draft.scopeKind === 'fixed_resources') resourceScope.resource_refs = [...new Set(draft.resourceRefs)].sort()
  const spec: TaskSpec = {
    schema: 'ddp-task-probe/1#TaskSpec',
    protocol: 'ddp-task/1',
    operation: draft.operation,
    workspace_ref: options.workspaceRef,
    query,
    resource_scope: resourceScope,
    search_policy: { mode: draft.mode, ordering: 'local_first' },
    execution_policy: federated
      ? { mode: 'trusted_federation', coordinator_ref: options.localNodeId }
      : { mode: 'local_only' },
    consent_refs: { exploration: explorationId, execution: null },
    requirements: { citations: draft.operation === 'rag.answer.cited' ? 'required' : 'not_required' },
    budget_ref: `budget-${options.nonce}`,
  }
  const validUntil = options.now + (options.validMinutes ?? 30) * MINUTE
  const scopeValid = draft.scope ? Date.parse(draft.scope.manifest.valid_until) : Number.POSITIVE_INFINITY
  const consent: ExplorationConsent = {
    schema: 'ddp-task-probe/1#ExplorationConsent',
    consent_id: explorationId,
    granted_by: options.grantedBy,
    granted_at: iso(options.now),
    valid_until: iso(Math.min(validUntil, scopeValid)),
    egress_mode: remote.length ? 'listed_nodes' : 'local_only',
    allowed_payload: remote.length ? [...new Set(draft.payload)].sort() : [],
    allowed_recipients: remote,
    budget: remote.length
      ? { max_probe_requests: Math.max(0, Math.floor(draft.maxProbeRequests)),
          max_egress_bytes: Math.max(0, Math.floor(draft.maxEgressBytes)) }
      : { max_probe_requests: 0, max_egress_bytes: 0 },
  }
  const body: IntentBody = { task_spec: spec, exploration_consent: consent }
  if (federated && draft.scope) body.scope_manifest = draft.scope.manifest
  return body
}

/**
 * 执行许可必须覆盖计划里的**每一个**端点：执行者、数据边两端、以及中继
 * （中继也是数据接收方，DataEdge.relay_via 的描述）。与协调者 `approve` 的判据
 * 逐项一致 —— 少一个就 egress_denied；多一个就是用户批准了计划里没有的外发。
 * 所以只从计划推，不接受额外输入。
 */
export function recipientsOf(plan: Pick<TaskPlan, 'steps' | 'data_edges'>): string[] {
  const nodes = new Set<string>()
  for (const step of plan.steps) nodes.add(step.executor_node_id)
  for (const edge of plan.data_edges) {
    nodes.add(edge.from_node_id)
    nodes.add(edge.to_node_id)
    for (const relay of edge.relay_via ?? []) nodes.add(relay)
  }
  return [...nodes].sort()
}

/**
 * 许可只有一个保留类别，而协调者要求它与**每一条**数据边的保留类别相同。
 * 计划里各边不一致时一份许可批不了 —— 显式报出来，不替用户挑一个。
 * 没有数据边（纯本地计划）时没有外发，用最保守的 temporary。
 */
export function retentionOf(plan: Pick<TaskPlan, 'data_edges'>): RetentionClass {
  const classes = new Set(plan.data_edges.map((edge) => edge.retention))
  if (classes.size > 1) throw new Error('计划里的数据边保留策略不一致，无法用一份执行许可批准')
  return [...classes][0] ?? 'temporary'
}

export function buildExecutionConsent(plan: TaskPlan, options: {
  rootTaskId: string
  grantedBy: string
  now: number
}): ExecutionConsent {
  if (!options.grantedBy) throw new Error('还没拿到账号信息，请稍后重试')
  const planValid = Date.parse(plan.valid_until)
  if (!(planValid > options.now)) throw new Error('计划已过期，请重新规划')
  return {
    schema: 'ddp-plan-admission/1#ExecutionConsent',
    consent_id: `execute-${options.rootTaskId}-r${plan.revision}`,
    plan_digest: plan.plan_digest,
    granted_by: options.grantedBy,
    granted_at: iso(options.now),
    valid_until: plan.valid_until,
    allowed_recipients: recipientsOf(plan),
    allowed_edges: plan.data_edges.map((edge) => edge.edge_id).sort(),
    retention: retentionOf(plan),
  }
}

/**
 * 取消只对**还没落定**的任务有意义，判据与轮询用的是同一个 —— 契约的 `active` 标记。
 * 手写一份 `queued|claimed|running` 的话，契约新增一个进行中状态时这里会把它
 * 当成终态、按钮消失，而那个任务其实还在跑。
 */
export function canCancel(status: Pick<TaskStatus, 'status'>): boolean {
  return !isSettled(status)
}

/**
 * 续跑的判据要与协调者 `resume` 一致，否则按钮点下去必定 403。
 *
 * 协调者要求：**有一份已批准的计划**（`planning_state === 'approved'`），
 * 且没被取消。在此之上界面只在"还有东西可补"时才给按钮：失败的任务，
 * 或者成功但没查全的任务。取消是显式终态，重跑必须走一条新任务。
 */
export function canResume(
  status: Pick<TaskStatus, 'status' | 'planning_state' | 'retrieval_completeness'>,
): boolean {
  if (status.planning_state !== 'approved') return false
  if (status.status === 'failed') return true
  return status.status === 'succeeded' && status.retrieval_completeness !== 'complete'
}
