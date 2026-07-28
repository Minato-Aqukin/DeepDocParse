import axios from 'axios'
import { ElMessage } from 'element-plus'

/** 统一的 axios 实例：自动带 JWT，401 直接踢回登录页。 */
export const http = axios.create({ baseURL: '/', timeout: 120_000 })

export const TOKEN_KEY = 'ddp.token'

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 后端错误体统一是 OpenAI 风格 {"error": {message, type, code}}
    const detail = error.response?.data?.error
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (location.hash !== '#/login') location.hash = '#/login'
    } else if (detail?.message) {
      ElMessage.error(detail.message)
    }
    return Promise.reject(error)
  },
)

export type ParseStatus = 'pending' | 'running' | 'archiving' | 'succeeded' | 'failed'
export type IndexStatus = 'none' | 'pending' | 'indexing' | 'ready' | 'failed'

export interface DocumentInfo {
  id: string
  filename: string
  doc_id: string
  origin: string
  mime: string
  size_bytes: number
  page_count: number
  status: ParseStatus
  error: string | null
  index_status: IndexStatus
  index_error: string | null
  current_job_id: string | null
  created_at: string
}

export interface JobInfo {
  id: string
  engine: string
  options: Record<string, unknown>
  status: ParseStatus
  error: string | null
  page_count: number
  is_current: boolean
  created_at: string
  archived_at: string | null
}

export interface Block {
  chunk_id: string | null
  seq: number
  page_idx: number
  bbox: [number, number, number, number] | null
  page_size: [number, number] | null
  text: string
}

export interface PageBlocks {
  page_idx: number
  page_size: [number, number] | null
  blocks: Block[]
}

export interface Citation {
  chunk_id: string
  page_idx: number
  bbox: [number, number, number, number] | null
  crop_url: string | null
  snippet: string
  score: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  verified: boolean
  degraded: string | null
  created_at: string
}

export interface KeyInfo {
  id: string
  name: string
  key_prefix: string
  quota_pages: number | null
  used_pages: number
  rate_limit_per_min: number
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
}

export const api = {
  register: (username: string, password: string) =>
    http.post('/api/auth/register', { username, password }),
  login: (username: string, password: string) =>
    http.post('/api/auth/login', { username, password }),

  listDocuments: (params: { q?: string; status?: string } = {}) =>
    http.get<DocumentInfo[]>('/api/documents', { params }),
  getDocument: (id: string) => http.get<DocumentInfo>(`/api/documents/${id}`),
  deleteDocument: (id: string) => http.delete(`/api/documents/${id}`),
  getResult: (id: string, job?: string) =>
    http.get<{ markdown: string; images: string[]; page_count: number; job_id: string }>(
      `/api/documents/${id}/result`,
      { params: job ? { job } : {} },
    ),
  getPages: (id: string, job?: string) =>
    http.get<{ page_count: number; pages: PageBlocks[]; job_id: string }>(
      `/api/documents/${id}/pages`,
      { params: job ? { job } : {} },
    ),
  getSourceUrl: (id: string) =>
    http.get<{ url: string; path: string; mime: string }>(`/api/documents/${id}/source-url`),
  listJobs: (id: string) => http.get<JobInfo[]>(`/api/documents/${id}/jobs`),
  reparse: (id: string, payload: { engine: string; options: Record<string, unknown> }) =>
    http.post<JobInfo>(`/api/documents/${id}/reparse`, payload),
  setCurrentJob: (id: string, job_id: string) =>
    http.put<DocumentInfo>(`/api/documents/${id}/current-job`, { job_id }),
  reindex: (id: string) => http.post<DocumentInfo>(`/api/documents/${id}/reindex`),
  downloadUrl: (id: string, format: 'md' | 'json' | 'zip' | 'source', job?: string) =>
    `/api/documents/${id}/download?format=${format}${job ? `&job=${job}` : ''}`,

  createConversation: (documentId: string) =>
    http.post<{ id: string; title: string }>(`/api/documents/${documentId}/conversations`),
  listConversations: (documentId: string) =>
    http.get<{ id: string; title: string; updated_at: string }[]>('/api/conversations', {
      params: { document: documentId },
    }),
  listMessages: (cid: string) => http.get<ChatMessage[]>(`/api/conversations/${cid}/messages`),
  deleteConversation: (cid: string) => http.delete(`/api/conversations/${cid}`),

  search: (q: string) =>
    http.get<{
      degraded: string | null
      groups: {
        document_id: string
        filename: string
        hits: { page_idx: number; snippet: string; score: number }[]
      }[]
    }>('/api/search', { params: { q } }),

  listKeys: () => http.get<KeyInfo[]>('/api/keys'),
  createKey: (payload: Record<string, unknown>) =>
    http.post<KeyInfo & { key: string }>('/api/keys', payload),
  revokeKey: (id: string) => http.delete(`/api/keys/${id}`),

  usage: (days = 30) =>
    http.get<{
      daily: { date: string; pages: number; requests: number }[]
      by_kind: { kind: string; pages: number; requests: number }[]
      total_pages: number
      total_requests: number
    }>(`/api/usage?days=${days}`),
}

/**
 * 问答的 SSE 流。
 *
 * 用 fetch + ReadableStream 而不是 EventSource：后者发不出 Authorization 头。
 * 返回一个 abort 函数，组件卸载时要调用，否则流会一直挂着。
 */
export function askStream(
  cid: string,
  question: string,
  handlers: {
    onMeta?: (data: { retrieval: { chunk_ids: string[] } }) => void
    onDelta: (text: string) => void
    onCitations?: (citations: Citation[]) => void
    onDone?: (data: { message_id: string; verified: boolean; degraded: string | null }) => void
    onError?: (data: { message: string; code: string }) => void
  },
): () => void {
  const controller = new AbortController()

  void (async () => {
    try {
      const resp = await fetch(`/api/conversations/${cid}/ask`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}`,
        },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => null)
        handlers.onError?.({
          message: body?.error?.message || `请求失败（${resp.status}）`,
          code: body?.error?.code || 'request_failed',
        })
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以空行分帧；最后一段可能不完整，留在 buffer 里等下一轮
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''
        for (const block of blocks) dispatch(block, handlers)
      }
      if (buffer.trim()) dispatch(buffer, handlers)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        handlers.onError?.({ message: String(error), code: 'network_error' })
      }
    }
  })()

  return () => controller.abort()
}

function dispatch(block: string, handlers: Parameters<typeof askStream>[2]) {
  const lines = block.split('\n')
  const event = lines.find((l) => l.startsWith('event: '))?.slice(7).trim()
  const raw = lines.find((l) => l.startsWith('data: '))?.slice(6)
  if (!event || !raw) return
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return // 半截帧：丢掉即可，下一轮 buffer 会补齐
  }
  if (event === 'meta') handlers.onMeta?.(data as Parameters<NonNullable<typeof handlers.onMeta>>[0])
  else if (event === 'delta') handlers.onDelta((data as { text: string }).text)
  else if (event === 'citations')
    handlers.onCitations?.((data as { citations: Citation[] }).citations)
  else if (event === 'done')
    handlers.onDone?.(data as Parameters<NonNullable<typeof handlers.onDone>>[0])
  else if (event === 'error')
    handlers.onError?.(data as Parameters<NonNullable<typeof handlers.onError>>[0])
}
