import type { BundleReplicaAvailability, IndexStatus } from '@deepdocparse/contracts'

import type { ParseStatus } from '@/types/api'
import { http } from './http'

export interface ResourceVersion {
  id: string
  resource_id: string
  version_no: number
  document_id: string
  source_digest: string
  /** 原文字节是否已验证（后端 version_out.source_digest_verified）。缺席按未验证处理。 */
  source_digest_verified?: boolean
  filename: string
  size_bytes: number
  parse_job_id: string | null
  parse_status?: ParseStatus | null
  /** 该固定版本解析任务的索引状态投影；检索需要 ready。缺席/null = 尚无解析任务 */
  index_status?: IndexStatus | null
  /** 本机来源策略是否允许此版本作为联邦任务输入；中心可省略。 */
  federation_input_allowed?: boolean
}
export interface Resource {
  id: string
  organization_id: string
  owner_id: string
  uploader_ref: { issuer: string; subject: string }
  display_name: string
  publication: 'private' | 'draft' | 'published' | 'withdrawn'
  copied_from?: string | null
  versions: ResourceVersion[]
}
export interface BundleEvidenceItem {
  evidence: Record<string, unknown>
  excerpt: string
}
export interface BundleEvidence {
  source: Record<string, unknown>
  evidence: BundleEvidenceItem[]
  structural_validation: string
  semantic_review: string
}
export interface BundleReplica {
  replica_id: string
  availability: BundleReplicaAvailability
  source_digest: string
  valid_until: string | null
  [key: string]: unknown
}
export interface LicensedSource {
  blob: Blob
  /** `X-DDP-Source-Availability`：online | offline_snapshot；缺席为 null（老中心）。 */
  availability: 'online' | 'offline_snapshot' | null
  filename: string
  sourceDigest: string | null
  validUntil: string | null
}
export const resourcesApi = {
  list: (scope: 'mine' | 'site_public', offset = 0) => http.get<{ items: Resource[]; has_more: boolean }>(
    '/api/resources', { params: { scope, offset, limit: 50 } }),
  publish: (id: string, publication: Resource['publication']) => http.patch<Resource>(
    `/api/resources/${encodeURIComponent(id)}`, { publication }),
  remove: (id: string) => http.delete(`/api/resources/${encodeURIComponent(id)}`),
  bundleUrl: (resource: string, version: string) =>
    `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle`,
  /** 冻结的原始证据信封与原文缺失状态（bundle-format.md）。导入证据不能当成已复核证据。 */
  bundleEvidence: (resource: string, version: string) =>
    http.get<BundleEvidence>(
      `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle/evidence`),
  /**
   * 授权副本目录。
   * 404 = 中心尚未提供该接口，调用方显示“尚未提供”，不伪造副本。
   */
  replicas: (resource: string, version: string) =>
    http.get<{ replicas: BundleReplica[] }>(
      `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle/replicas`),
  /**
   * 许可来源字节。可用性只认响应头 `X-DDP-Source-Availability`；
   * 撤销/过期后端回 410，调用方不得显示“已保存/在线”。
   * 原文摘要及来源许可期限来自 `X-DDP-Source-Digest` / `X-DDP-Source-Licence-Valid-Until`。
   */
  licensedSource: async (resource: string, version: string): Promise<LicensedSource> => {
    const response = await http.get(
      `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle/licensed-source`,
      { responseType: 'blob' })
    const raw = String(response.headers?.['x-ddp-source-availability'] ?? '')
    const availability = raw === 'online' || raw === 'offline_snapshot' ? raw : null
    return {
      blob: response.data as Blob, availability, filename: `${resource}-${version}.source`,
      sourceDigest: response.headers?.['x-ddp-source-digest'] ?? null,
      validUntil: response.headers?.['x-ddp-source-licence-valid-until'] ?? null,
    }
  },
  /** 撤销一个授权副本。写操作自带幂等键，丢响应后调用方复用同一键对账，不换键重发。 */
  revokeReplica: (resource: string, version: string, replicaId: string, idempotencyKey: string) =>
    http.post(
      `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle/replicas/${encodeURIComponent(replicaId)}/revoke`,
      {}, { headers: { 'Idempotency-Key': idempotencyKey } }),
}
