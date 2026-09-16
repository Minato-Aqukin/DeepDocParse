import type { DesktopClientBridge, Result } from '../../../desktop/bridge'

export type { ClientView, ConnectionSummary, Json, PlanDetail, Result } from '../../../desktop/bridge'
export interface DesktopBridge extends DesktopClientBridge {
  hostStatus(): Promise<Result<{ secrets: { backend: string; persistentAvailable: boolean }; lifecycle: string }>>
  selectWorkspace(): Promise<Result<{ workspaceId: string; name: string } | null>>
  setCredential(input: { environmentId: string; profileId: string; secret: string; persist: boolean }): Promise<Result<unknown>>
}
declare global { interface Window { ddpDesktop?: DesktopBridge } }

export function unwrap<T>(result: Result<T>): T {
  if (!result.ok) throw new Error(result.error.code)
  return result.value
}
const reasons: Record<string, string> = {
  connection_failed: '连接暂不可用，已保留草稿。', authentication_required: '此身份需要重新认证。',
  identity_mismatch: '环境身份与已配对记录不一致。', profile_mismatch: '登录身份与已配对记录不一致。',
  protocol_incompatible: '此环境未提供所需的工作台协议。', cache_failure: '本地缓存无法写入，请检查可用空间。',
  model_unavailable: '生成模型尚不可用，可以继续检索和查看原文。', unsupported_operation: '此环境暂不支持这项操作。',
  approved_plan_required: '此操作需要先确认远端执行与外发许可。', outcome_unknown: '提交结果未确认，请查询回执后再处理。',
  receipt_required: '请查询已保存操作的回执。', disposed: '连接已切换，请在当前工作区重新操作。',
  draft_conflict: '草稿已被另一窗口更新，请重新打开后合并。', revision_conflict: '草稿已被另一窗口更新，请重新打开后合并。',
  input_too_large: '文件超过当前操作的大小限制。', not_found: '该资料不存在或当前身份无权访问。',
  wiki_response_too_large: 'Wiki 修订超过当前读取大小限制。', wiki_source_unavailable: 'Wiki 的固定来源已经不可用，请重新选择来源。',
  wiki_generation_invalid: '模型输出未通过 Wiki 格式或引用检查，此次没有发布修订。', unsupported_generation: '生成内容缺少有效的原始出处，此次没有发布。',
  out_of_memory: '本机内存不足，模型已经停止；可以查看任务后重新启动。', cursor_expired: '目录已更新或快照已失效，请重新读取首页。',
  // Remote plan flow. Each code names why nothing was sent, or what must be reconciled.
  approval_cancelled: '已取消批准，没有授予任何外发许可。', approval_unavailable: '当前宿主无法显示系统确认框，不能批准外发。',
  plan_changed: '计划内容与审阅时不一致，请重新审阅后再操作。', consent_required: '该阶段尚未批准，未发送任何内容。',
  consent_revoked: '批准已撤销；需要准备并批准新计划。', consent_expired: '计划或批准已过期；需要准备新计划。',
  budget_exceeded: '超出已批准的请求或外发字节预算，未发送。', policy_denied: '接收方、地址或数据边超出已批准范围，未发送。',
  input_changed: '本地输入与锁定摘要不一致，未发送。', local_only: '工作区处于仅本地模式，禁止外发。',
  center_not_paired: '尚未配对计划中的中心。', center_not_current: '中心连接未就绪；重新连接并核对节点身份后再操作。',
  center_identity_changed: '中心地址或身份与已审阅计划不一致，已拒绝发送。', center_binding_required: '计划没有唯一的已审阅接收方，不能派发。',
  center_unavailable: '当前连接无法取得中心凭证。', delivery_unverified: '交付结果没有通过本地摘要重算，不能确认。',
  dispatch_already_reserved: '这次发送已经占用预算，请先对账再重试。', idempotency_conflict: '操作编号已用于另一项操作，请重新发起。',
  connection_not_current: '本机工作区连接未就绪，已保留草稿。', unreachable: '中心暂时无法连接，未确认任何结果。',
  delivery_expired: '交付已过期，结果没有保存到本机。', delivery_not_found: '中心暂时没有这份交付，可以稍后再取。',
  delivery_id_missing: '中心尚未给出交付编号。', result_manifest_mismatch: '取回的结果与中心声明的摘要不一致，没有保存。',
  result_unavailable: '中心没有返回可校验的结果。', ack_not_confirmed: '中心没有确认这次交付，可以再次确认。',
  egress_denied: '中心拒绝了这次外发许可。', invalid_response: '中心返回的内容无法识别。',
}
export function workspaceError(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  return reasons[code] ?? '操作未完成，请查看连接和任务状态。'
}
