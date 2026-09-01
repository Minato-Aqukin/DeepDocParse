import type {
  DocumentInfo,
  DocumentPages,
  DocumentResult,
  DocumentStats,
  DownloadFormat,
  EngineChoice,
  JobInfo,
  IndexValidation,
  SourceUrl,
} from '@/types/api'

import { http } from './http'

export interface DocumentQuery {
  q?: string
  /** 解析状态，后端按 current/latest job 的状态过滤 */
  status?: string
  limit?: number
  offset?: number
}

export const documentsApi = {
  list: (params: DocumentQuery = {}) => http.get<DocumentInfo[]>('/api/documents', { params }),
  get: (id: string) => http.get<DocumentInfo>(`/api/documents/${id}`),
  stats: () => http.get<DocumentStats>('/api/documents/stats/summary'),
  remove: (id: string) => http.delete(`/api/documents/${id}`),

  /** 上传。engine/options 走 multipart 的普通字段，options 是 JSON 字符串。 */
  upload: (file: File, choice: EngineChoice, onProgress?: (percent: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    form.append('engine', choice.engine)
    form.append('options', JSON.stringify(choice.options ?? {}))
    return http.post<DocumentInfo>('/api/documents', form, {
      onUploadProgress: (e) => {
        if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100))
      },
    })
  },

  result: (id: string, job?: string) =>
    http.get<DocumentResult>(`/api/documents/${id}/result`, { params: job ? { job } : {} }),
  pages: (id: string, job?: string) =>
    http.get<DocumentPages>(`/api/documents/${id}/pages`, { params: job ? { job } : {} }),
  layout: (id: string, job?: string) =>
    http.get<Record<string, unknown>>(`/api/documents/${id}/layout`, {
      params: job ? { job } : {},
    }),
  sourceUrl: (id: string) => http.get<SourceUrl>(`/api/documents/${id}/source-url`),

  listJobs: (id: string) => http.get<JobInfo[]>(`/api/documents/${id}/jobs`),
  reparse: (id: string, choice: EngineChoice) =>
    http.post<JobInfo>(`/api/documents/${id}/reparse`, choice),
  setCurrentJob: (id: string, job_id: string, acknowledgeInvalidations = false) =>
    http.put<DocumentInfo>(`/api/documents/${id}/current-job`, {
      job_id,
      acknowledge_invalidations: acknowledgeInvalidations,
    }),
  validateIndex: (id: string, jobId?: string) =>
    http.post<IndexValidation>(`/api/documents/${id}/validate-index`, null, {
      params: jobId ? { job_id: jobId } : {},
    }),
  reindex: (id: string, acknowledgeInvalidations = false) =>
    http.post<DocumentInfo>(`/api/documents/${id}/reindex`, null, {
      params: acknowledgeInvalidations ? { acknowledge_invalidations: true } : {},
    }),

  downloadUrl: (id: string, format: DownloadFormat, job?: string) =>
    `/api/documents/${id}/download?format=${format}${job ? `&job=${job}` : ''}`,
}
