import type { CapabilityReadiness, MemberExpansionState, NodeMembershipState } from '@deepdocparse/contracts'
import type { ScopeEnvelope } from '@/federation/task-model'
import { http } from './http'

/** Inline enum in ddp-scope-coverage/v1.json#ScopeManifest (not an enums.yaml enum). */
export interface DirectoryUnexpandedSubtree {
  node_id: string
  reason: 'timeout' | 'denied' | 'enumeration_unsupported' | 'budget_exhausted' | 'unknown'
}
export type DirectoryScopeEnvelope = ScopeEnvelope & {
  manifest: {
    unexpanded_subtrees: DirectoryUnexpandedSubtree[]
    registry_revision_vector: (ScopeEnvelope['manifest']['registry_revision_vector'][number] & { directory_ref?: string })[]
  }
}


/**
 * 互联公开目录浏览（`packages/contracts/openapi/discovery-v1.yaml`）。
 *
 * 只读已封存的公开快照：**不编造全网总数** —— 总数只显示当前快照窗口的
 * `visible_total`，来源/目录修订/快照水位/离线与未知分支逐项照后端原样显示。
 * 节点管理（register/approve/revoke）是管理员会话入口，本模块不提供。
 */

export interface DirectoryMember {
  node_id: string
  state: NodeMembershipState
  revision: number
  descriptor?: Record<string, unknown> | null
  route?: Record<string, unknown> | null
  configured: boolean
  health: CapabilityReadiness
  accepting_admissions: boolean
  expansion_state: MemberExpansionState
}

export interface MemberSnapshot {
  snapshot_id: string
  authority_node_id: string
  caller_scope_hash: string
  registry_revision: number
  created_at: string
  expires_at: string
  first_cursor: string
  terminal_cursor: string
}

export interface MemberSnapshotPage extends MemberSnapshot {
  members: DirectoryMember[]
  next_cursor: string | null
  complete: boolean
}

const inline = { suppressErrorToast: true } as const

export const directoryApi = {
  /** 使用同一成员快照枚举公开检索集合；不调用需要节点凭据的 peer 端点。 */
  createScope: (memberSnapshotId: string) =>
    http.post<DirectoryScopeEnvelope>('/api/v1/federation/scopes',
      { operation: 'corpus.retrieve', member_snapshot_id: memberSnapshotId }, inline),
  /** 仅本节点有会话鉴权的集合详情；远端目录没有 owner 引用时不可臆造。 */
  collection: (collectionId: string) =>
    http.get<{ collection_id: string; name: string; owner_id: string; publication: string }>(
      `/api/v1/collections/${encodeURIComponent(collectionId)}`, inline),
  /** 封存一份当前调用者可见成员快照（`createMemberSnapshot`）。 */
  createSnapshot: (body: { page_size?: number; ttl_seconds?: number } = {}) =>
    http.post<MemberSnapshot>('/api/v1/federation/member-snapshots', body, inline),
  /** 读快照的一页。终端页 `complete=true` 时成员为空且 `next_cursor=null`。 */
  snapshotPage: (snapshotId: string, cursor?: string) =>
    http.get<MemberSnapshotPage>(
      `/api/v1/federation/member-snapshots/${encodeURIComponent(snapshotId)}/members`,
      { params: cursor ? { cursor } : {}, ...inline }),
  /** 管理员目录（含配置、健康与接单状态分离）。非管理员 403。 */
  nodes: () => http.get<{ members: DirectoryMember[] }>('/api/v1/federation/nodes', inline),
  /** 本节点公开身份（无 workspace/用户；challenge 可选要 60 秒持有证明）。 */
  node: (challenge?: string) =>
    http.get<Record<string, unknown>>('/api/v1/federation/node',
      { params: challenge ? { challenge } : {}, ...inline }),
}
