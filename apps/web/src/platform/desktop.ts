import type { DesktopClientBridge, Result } from '../../../desktop/bridge'

export type { ClientView, ConnectionSummary, Json, Result } from '../../../desktop/bridge'
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
}
export function workspaceError(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  return reasons[code] ?? '操作未完成，请查看连接和任务状态。'
}
