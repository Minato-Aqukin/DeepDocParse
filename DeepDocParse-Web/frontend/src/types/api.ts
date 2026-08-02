/** 后端返回体的类型。与 backend/app/routers/*.py 的 response_model 一一对应。 */

export type ParseStatus = 'pending' | 'running' | 'archiving' | 'succeeded' | 'failed'
export type IndexStatus = 'none' | 'pending' | 'indexing' | 'ready' | 'failed'
export type DownloadFormat = 'md' | 'json' | 'zip' | 'source'

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

export interface DocumentResult {
  document_id: string
  job_id: string
  filename: string
  page_count: number
  markdown: string
  images: string[]
}

export interface DocumentPages {
  document_id: string
  job_id: string
  page_count: number
  pages: PageBlocks[]
}

export interface SourceUrl {
  url: string
  path: string
  mime: string
}

export interface DocumentStats {
  documents: number
  pages: number
  askable: number
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

export interface ConversationInfo {
  id: string
  document_id: string
  title: string
  created_at: string
  updated_at: string
}

export interface SearchHit {
  chunk_id: string
  page_idx: number
  bbox: [number, number, number, number] | null
  score: number
  snippet: string
}

export interface SearchResult {
  query: string
  /** 非 null 表示这次检索降级了（如 embedding_unavailable），UI 必须显示出来 */
  degraded: string | null
  groups: { document_id: string; filename: string; hits: SearchHit[] }[]
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

/** 明文 key 只在创建时返回一次。 */
export interface CreatedKey extends KeyInfo {
  key: string
}

export interface UsageSummary {
  daily: { date: string; pages: number; requests: number }[]
  by_kind: { kind: string; pages: number; requests: number }[]
  total_pages: number
  total_requests: number
}

export interface AuthToken {
  access_token: string
  token_type: string
  user_id: string
  username: string
}

export interface Profile {
  user_id: string
  username: string
  email: string | null
  created_at: string
}

/** 解析参数：engine + 引擎自己的透传选项（schema 见 constants/engines.ts）。 */
export interface EngineChoice {
  engine: string
  options: Record<string, unknown>
}
