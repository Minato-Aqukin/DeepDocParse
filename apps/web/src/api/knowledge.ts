import type {
  EvidenceBacklink,
  KnowledgeEntity,
  KnowledgeGraph,
  KnowledgeReviewItem,
  WikiDetail,
  WikiSummary,
} from '@/types/api'

import { http } from './http'

export const knowledgeApi = {
  entities: (params: { q?: string; entity_type?: string; uncertain?: boolean } = {}) =>
    http.get<{ graph_version: string; entities: KnowledgeEntity[] }>('/api/knowledge/entities', { params }),
  graph: (entity = '', depth = 1) =>
    http.get<KnowledgeGraph>('/api/knowledge/graph', { params: entity ? { entity, depth } : { depth } }),
  wikiList: () => http.get<WikiSummary[]>('/api/wiki'),
  wiki: (idOrTitle: string) => http.get<WikiDetail>(`/api/wiki/${encodeURIComponent(idOrTitle)}`),
  backlinks: (evidenceId: string) =>
    http.get<{ evidence_id: string; backlinks: EvidenceBacklink[] }>(
      `/api/evidence/${evidenceId}/backlinks`,
    ),
  reviews: () => http.get<{ items: KnowledgeReviewItem[]; truncated: boolean; limit: number }>('/api/reviews'),
  review: (
    targetKind: KnowledgeReviewItem['target_kind'],
    targetId: string,
    data: { action: 'pass' | 'reject' | 'question'; reason_code?: string; reason_text?: string },
  ) => http.post(`/api/reviews/${targetKind}/${encodeURIComponent(targetId)}`, data),
  split: (entityId: string, alias: string) =>
    http.post<KnowledgeEntity>(`/api/knowledge/entities/${entityId}/split`, { alias }),
  build: (evidenceIds: string[] = []) =>
    http.post<{ status: string; entities: number; edges: number; relation_status: 'ok' | 'not_found'; wiki_entries: number }>(
      '/api/knowledge/build', { evidence_ids: evidenceIds },
    ),
}
