import { http } from './http'

/**
 * 版本化 Wiki（`packages/contracts/openapi/wiki-v1.yaml` + `ddp/wiki-format.md`）。
 *
 * 中心 `/api/wikis` 工作流与旧 `/api/wiki` 实体页是两套东西：这里只走版本化那套。
 * 写操作全部带 `Idempotency-Key`（同键同体复用固定修订，同键异体 409，不消费键留半稿）。
 * 写用 `current_revision_id` 做 compare-and-swap：409 `revision_conflict` 时调用方必须重读。
 */

export interface WikiSourceRef {
  resource_id: string
  source_version_id: string
}

export interface WikiBuildBody {
  title: string
  sources: WikiSourceRef[]
  max_pages?: number
  max_evidence?: number
  max_output_tokens?: number
  max_input_chars?: number
}

export interface WikiRevisionDocument {
  wiki: {
    id: string
    title: string
    current_revision_id: string
    published_revision_id: string | null
  }
  revision: {
    id: string
    wiki_id: string
    base_revision_id: string | null
    kind: string
    title: string
    created_by: string
    created_at: string
    provider: Record<string, unknown>
    limits: Record<string, unknown>
    merge_conflicts: unknown[]
    stale: boolean
    stale_reasons: Record<string, string[]>
    pages: WikiVersionedPage[]
    dependency_manifest: WikiDependency[]
    relations?: WikiRelation[]
  }
}

export interface WikiVersionedPage {
  page_key: string
  title: string
  generated_sections: {
    heading: string
    sentences: {
      id: string
      text: string
      evidence_ids: string[]
      unsupported: boolean
      conflict_group?: string | null
    }[]
  }[]
  /** 服务端存储时加上 `kind: 'human'`、`unsupported: true`；写回（PATCH）只收 `{id, text}` */
  human_paragraphs: { id: string; text: string; kind?: 'human'; unsupported?: boolean }[]
  stale: boolean
}

export interface WikiDependency {
  page_key: string
  resource_id: string
  source_version_id: string
  document_id: string
  source_digest: string
  parse_revision: string
  evidence_id: string
  excerpt_digest: string
  locator: Record<string, unknown>
  /** 联邦行才有：`evidence_id` 是 `source:+…` 稳定引用，真实本地 ID 在这里，原样保留不改写。 */
  source_evidence_id?: string | null
  /** 无 origin = 旧本地依赖，保持既有本地行为；有 origin 才做权威比对。 */
  origin_node_id?: string | null
  authority_node_id?: string | null
  source_publication?: string | null
  policy_revision?: string | null
  derivative_grant?: string | null
  retrieval_receipt_ref?: string | null
}

/**
 * `revision.relations` 的合理类型化：内核 `edge_result` 落库形状
 *（`subject_id/object_id/predicate/evidence_ids`），联邦重校验后还带
 * `source_type/confidence_kind/relation_profile/direction_semantics` 等附加字段。
 * 旧修订可能为空数组；未知附加字段用索引签名透传，不丢弃。
 */
export interface WikiRelation {
  subject_id?: string
  object_id?: string
  predicate: string
  evidence_ids: string[]
  unsupported?: boolean
  [key: string]: unknown
}

/**
 * Wiki 引用解析：claim/relation/`dependency_manifest` 的 `evidence_id`
 * 可能是 `source:+…` qualified 引用，绝不能直接送本地证据接口。
 *
 * 先按 `dependency_manifest` join：无 origin 的旧本地依赖保持既有行为；
 * 带 origin 的必须明确等于当前实际权威才允许用 `source_evidence_id` 走本地接口；
 * 跨源或权威未知一律判 foreign，不猜成 local，也不在此向任意 origin 发请求。
 */
export type WikiEvidenceResolution =
  | { kind: 'local'; evidenceId: string; dependency: WikiDependency | null }
  | { kind: 'foreign'; dependency: WikiDependency; reason: 'origin_mismatch' | 'authority_unknown' }
  | { kind: 'missing'; ref: string }

export function resolveWikiEvidenceRef(
  manifest: readonly WikiDependency[] | undefined | null,
  ref: string,
  authorityNodeId: string | null | undefined,
): WikiEvidenceResolution {
  if (!ref) return { kind: 'missing', ref }
  const dep = (manifest ?? []).find((item) => item?.evidence_id === ref) ?? null
  if (!dep) return { kind: 'missing', ref }
  const origin = dep.origin_node_id ?? null
  if (!origin) {
    return ref.startsWith('source:')
      ? { kind: 'missing', ref }
      : { kind: 'local', evidenceId: ref, dependency: dep }
  }
  if (!authorityNodeId) return { kind: 'foreign', dependency: dep, reason: 'authority_unknown' }
  if (origin === authorityNodeId) {
    const evidenceId = dep.source_evidence_id || dep.evidence_id
    return evidenceId.startsWith('source:')
      ? { kind: 'missing', ref }
      : { kind: 'local', evidenceId, dependency: dep }
  }
  return { kind: 'foreign', dependency: dep, reason: 'origin_mismatch' }
}

/** 本地 frozen 与联邦 envelope 两种 locator 形状都按固定定位原文展示，不编造。 */
export function formatWikiLocator(locator: Record<string, unknown> | undefined | null): string {
  if (!locator || typeof locator !== 'object') return '定位缺失'
  const rec = locator as Record<string, unknown>
  const parts: string[] = []
  if (typeof rec.kind === 'string' && rec.kind) parts.push(rec.kind)
  const page = typeof rec.physical_page_index === 'number'
    ? rec.physical_page_index
    : typeof rec.page_idx === 'number' ? rec.page_idx : null
  if (typeof page === 'number') parts.push(typeof rec.printed_page_label === 'string' && rec.printed_page_label
    ? `印刷页 ${rec.printed_page_label} · PDF 第 ${page + 1} 页` : `第 ${page + 1} 页`)
  if (typeof rec.seq === 'number') parts.push(`块 ${rec.seq}`)
  if (Array.isArray(rec.bbox) && rec.bbox.length === 4
    && (rec.bbox as unknown[]).every((n) => typeof n === 'number')) {
    parts.push(`bbox ${(rec.bbox as number[]).map((n) => Math.round(n)).join(', ')}`)
  }
  return parts.join(' · ') || '定位缺失'
}

const key = (idempotencyKey: string) => ({ headers: { 'Idempotency-Key': idempotencyKey } })
const inline = { suppressErrorToast: true } as const

export const versionedWikiApi = {
  list: () => http.get<WikiRevisionDocument[]>('/api/wikis', inline),
  read: (wikiId: string) =>
    http.get<WikiRevisionDocument>(`/api/wikis/${encodeURIComponent(wikiId)}`, inline),
  readRevision: (wikiId: string, revisionId: string) =>
    http.get<WikiRevisionDocument>(
      `/api/wikis/${encodeURIComponent(wikiId)}/revisions/${encodeURIComponent(revisionId)}`, inline),
  /** 新 Wiki 主题：选定**固定版本**来源，不静默替换新版本。 */
  create: (body: WikiBuildBody, idempotencyKey: string) =>
    http.post<WikiRevisionDocument>('/api/wikis', body, { ...inline, ...key(idempotencyKey) }),
  /** 用当前资源生成新修订：保留上一修订的人工段落，缺席页面进 `merge_conflicts`。 */
  rebuild: (wikiId: string, body: WikiBuildBody & { base_revision_id: string }, idempotencyKey: string) =>
    http.post<WikiRevisionDocument>(`/api/wikis/${encodeURIComponent(wikiId)}/revisions`, body,
      { ...inline, ...key(idempotencyKey) }),
  /** 人工编辑：只写 `human_paragraphs`，不改生成文本与基准修订。用户文本标 human/unsupported。
   *  读回的段落带服务端标注（kind/unsupported），而 PATCH 只收 `{id, text}`、多一个字段就 422：
   *  在这里统一投影，调用方可以直接把读到的段落连同新段落传进来（D 阶段浏览器实测：
   *  页上有了一段人工补充之后，第二段永远存不进去）。 */
  editPage: (wikiId: string, pageKey: string,
    body: { base_revision_id: string; paragraphs: { id: string; text: string }[] }, idempotencyKey: string) =>
    http.patch<WikiRevisionDocument>(
      `/api/wikis/${encodeURIComponent(wikiId)}/pages/${encodeURIComponent(pageKey)}`,
      { base_revision_id: body.base_revision_id,
        paragraphs: body.paragraphs.map(({ id, text }) => ({ id, text })) },
      { ...inline, ...key(idempotencyKey) }),
  /** 发布：来源私有/撤销/不可用/过期/unsupported/未解决冲突时 409/403，不许发布。 */
  publish: (wikiId: string, baseRevisionId: string) =>
    http.post<WikiRevisionDocument>(`/api/wikis/${encodeURIComponent(wikiId)}/publish`,
      { base_revision_id: baseRevisionId }, inline),
}
