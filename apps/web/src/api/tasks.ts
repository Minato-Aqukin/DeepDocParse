/**
 * 联邦任务 API：`packages/contracts/openapi/federation-tasks-v1.yaml` 的协调者端点，
 * 经 control-api 会话鉴权转发到 corpus-api。
 */
import type {
  CoverageLedger, EventPage, ExecutionConsent, IntentBody, ScopeEnvelope,
  TaskIntent, TaskListPage, TaskPlan, TaskStatus,
} from '@/federation/task-model'

import { http } from './http'

const task = (id: string) => `/api/v1/tasks/${encodeURIComponent(id)}`

/**
 * 受理锚。契约给 `POST /task-intents` 与 `POST /tasks` 都标了必填
 * `Idempotency-Key`：丢响应后的重试不能变成第二个任务（T80/T81），
 * 所以键由调用方持有并跨重试复用，不在这里现生成。
 */
const key = (idempotencyKey: string) => ({ headers: { 'Idempotency-Key': idempotencyKey } })

/**
 * 任务页的失败一律由页面自己内联说明（哪一块读不到就在哪一块说），
 * 所以这些请求不走全局 toast —— 否则同一个错误会报两遍，而且 toast 那份
 * 是后端原文，会把页面刻意合并的 404/无权限文案绕过去。
 */
const inline = { suppressErrorToast: true } as const

export interface DeliveryRead {
  delivery_id: string
  root_task_id: string
  state: string
  result_manifest_digest: string | null
  /** 可交付结果文档（规范原文，不含摘要字段本身）。null = 超界未持久化，不许确认。 */
  result: Record<string, unknown> | null
  expires_at: string | null
}
export interface DeliveryReceipt {
  schema: string
  delivery_id: string
  root_task_id: string
  step_id: string
  state: string
  result_manifest_digest: string | null
  verified_at: string | null
  idempotency_key: string
  retention: string
}
export const tasksApi = {
  /** 本人任务列表（`listTasks`）：只带状态轴，结果按 id 读。 */
  list: (params: { limit?: number; cursor?: string } = {}) =>
    http.get<TaskListPage>('/api/v1/tasks', { params, ...inline }),
  read: (rootTaskId: string) => http.get<TaskStatus>(task(rootTaskId), inline),
  coverage: (rootTaskId: string) => http.get<CoverageLedger>(`${task(rootTaskId)}/coverage`, inline),
  events: (rootTaskId: string, after: number) =>
    http.get<EventPage>(`${task(rootTaskId)}/events`, { params: { after }, ...inline }),
  /**
   * 读当前计划修订。契约没有单独的 GET：`createTaskPlan` 对已是 ready/approved 的任务
   * 是**幂等重放**，原样返回存着的计划，不会重新规划、不会再发 Probe。
   */
  plan: (rootTaskId: string) => http.post<TaskPlan>('/api/v1/task-plans', { root_task_id: rootTaskId }, inline),
  /**
   * 读取暂存中的可交付结果字节（有界 JSON，不含源文件字节）。
   * 读取不是确认；未确认且过 TTL 后端回 410 `delivery_expired`，调用方不得再显示“已保存本地”。
   */
  delivery: (deliveryId: string) =>
    http.get<DeliveryRead>(`/api/v1/deliveries/${encodeURIComponent(deliveryId)}`, inline),
  /**
   * 本地校验并持久提交后的幂等确认。**调用方必须先用 `contentDigest(canonical result)`
   * 重算 `result_manifest_digest` 并对上才调**；不一致后端回 409 `input_not_verified`。
   * 同一 delivery 的重复确认复用同一幂等键，不换键重发。
   */
  ackDelivery: (deliveryId: string, resultManifestDigest: string, idempotencyKey: string) =>
    http.post<DeliveryReceipt>(`/api/v1/deliveries/${encodeURIComponent(deliveryId)}/ack`,
      { result_manifest_digest: resultManifestDigest }, { ...inline, ...key(idempotencyKey) }),

  /**
   * 已认证的身份握手（`authenticatedCapabilities`）：一次拿全签许可要用的三样 ——
   * 本节点（`coordinator_ref`）、工作区（`workspace_ref`）、以及**服务端认定的用户**
   * （`granted_by`）。
   *
   * 不从 auth store 取：那份 profile 只在设置页加载过，任务页上多半是空的；
   * 而且许可上写的"谁批准的"应该是服务端认定的身份，不是客户端缓存。
   */
  identity: () => http.get<{
    identity: { environment_id: string; authority_node_id: string; workspace_id: string }
    profile: { issuer: string; subject: string }
  }>('/api/v1/capabilities', inline),

  /** 封存一份调用方范围清单（`createScopeManifest`）—— 穷查的分母只能来自它。 */
  createScope: (operation: string) =>
    http.post<ScopeEnvelope>('/api/v1/federation/scopes', { operation }, inline),

  /** 读已封存范围的目标分页与实时撤销覆盖（`getScopeTargets`）。 */
  scopeTargets: (scopeId: string, cursor?: string) =>
    http.get<{ scope_id: string; manifest_digest: string;
      targets: { target_key: { origin_node_id: string; collection_id: string; operation: string }; state: string }[];
      next_cursor: string | null; complete: boolean; total_targets: number; expired: boolean }>(
      `/api/v1/federation/scopes/${encodeURIComponent(scopeId)}/targets`,
      { params: cursor ? { cursor } : {}, ...inline }),

  createIntent: (body: IntentBody, idempotencyKey: string) =>
    http.post<TaskIntent>('/api/v1/task-intents', body, { ...inline, ...key(idempotencyKey) }),

  readPlan: (rootTaskId: string) =>
    http.get<TaskPlan>(`/api/v1/task-plans/${encodeURIComponent(rootTaskId)}`, inline),

  approve: (rootTaskId: string, planDigest: string, consent: ExecutionConsent) =>
    http.post<TaskPlan>(`/api/v1/task-plans/${encodeURIComponent(rootTaskId)}/approve`,
      { plan_digest: planDigest, execution_consent: consent }, inline),

  /** 受理已批准的计划。**202 是新受理，200 是同键重放** —— 调用方据此知道要不要等。 */
  submit: (rootTaskId: string, planDigest: string, idempotencyKey: string) =>
    http.post<TaskStatus>('/api/v1/tasks',
      { root_task_id: rootTaskId, plan_digest: planDigest }, { ...inline, ...key(idempotencyKey) }),

  cancel: (rootTaskId: string) => http.post<TaskStatus>(`${task(rootTaskId)}/cancel`, {}, inline),
  resume: (rootTaskId: string) => http.post<TaskStatus>(`${task(rootTaskId)}/resume`, {}, inline),
}
