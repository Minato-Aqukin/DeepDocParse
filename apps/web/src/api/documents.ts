import type {
  DocumentInfo,
  DocumentPages,
  DocumentResult,
  DocumentStats,
  DownloadFormat,
  DownloadUrl,
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

  // **没有 upload。** 上传走 `@/api/uploads` 的直传链路：
  // 拿预签名 -> 分片 PUT 到对象存储 -> finalize。字节流不经过任何应用进程
  // （不变式 6）。旧的 multipart 端点已经从服务端删掉了。

  result: (id: string, job?: string) =>
    http.get<DocumentResult>(`/api/documents/${id}/result`, { params: job ? { job } : {} }),
  pages: (id: string, job?: string) =>
    http.get<DocumentPages>(`/api/documents/${id}/pages`, { params: job ? { job } : {} }),
  layout: (id: string, job?: string) =>
    http.get<Record<string, unknown>>(`/api/documents/${id}/layout`, {
      params: job ? { job } : {},
    }),
  /**
   * 浏览器用的**短期**下载/预览 URL。
   *
   * 与网关用的稳定 URL（`/files/{token}`）**刻意分开**：那条的路径必须永远
   * 不变（文档身份 doc_hash 在没有 doc_id 时会回退成 sha256(file_url)，
   * URL 一变，幂等复用与向量索引分块键全部失效，ADR #11/#12 踩过两次）。
   * 这条每次返回一个新签名，因此能收紧 TTL、能按 disposition 变化，
   * 而且**由对象存储直供**——支持 HTTP Range，PDF.js 看第 200 页不用下整份。
   */
  sourceViewUrl: (id: string, disposition: 'inline' | 'attachment' = 'inline') =>
    http.get<DownloadUrl>(`/api/documents/${id}/download-url`, { params: { disposition } }),

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

  /** 解析产物（markdown / json / zip）的下载路径。与原件预览是两回事。 */
  exportUrl: (id: string, format: DownloadFormat, job?: string) =>
    `/api/documents/${id}/download?format=${format}${job ? `&job=${job}` : ''}`,
}
