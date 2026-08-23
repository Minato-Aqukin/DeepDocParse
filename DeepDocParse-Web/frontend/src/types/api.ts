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
  /** 稳定定位键：chunk_id 每次 reindex 都会重铸，(parse_job_id, seq) 不会 */
  parse_job_id: string | null
  seq: number | null
  page_idx: number
  bbox: [number, number, number, number] | null
  crop_url: string | null
  snippet: string
  /** RRF 融合分：只用来排序，**不是**相关度（上限约 0.033，由名次决定） */
  score: number
  /** 余弦相似度：有校准量纲，这才是"有多相关"。关键词路命中/向量化挂了时为 null */
  similarity: number | null
  /** 这条出处还能不能接回当前索引里的原文（reindex/换引擎重解析后可能接不回） */
  resolved: boolean
}

/** 这一组出处有多可信。后端算好给前端，校准值只在 backend/app/config.py 一处。 */
export interface RetrievalConfidence {
  level: 'high' | 'low' | 'unknown'
  top_similarity: number | null
  warn_below: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  verified: boolean
  degraded: string | null
  /** 这一轮用的模型与检索参数快照，换模型后靠它分组对比历史 */
  model_meta?: Record<string, unknown>
  confidence?: RetrievalConfidence
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
  /** RRF 名次分，排序用 */
  score: number
  /** 余弦相似度，"有多相关"看它 */
  similarity: number | null
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


/* ---------- 结构化抽取（DDP-Extract v1） ---------- */

export type FieldStatus = 'found' | 'not_found' | 'error'
export type RunStatus = 'pending' | 'running' | 'succeeded' | 'partial' | 'failed'
export type SchemaKind = 'object' | 'array'
export type LeafType = 'string' | 'number' | 'integer' | 'boolean'

/** schema 里的一个叶子字段。**description 是必填的** —— 它同时是这个字段的检索 query。 */
export interface SchemaField {
  name: string
  type: LeafType
  description: string
  format?: string
  enum?: string[]
  required?: boolean
}

/**
 * 一个字段的抽取结果。
 *
 * **三态必须分开显示**：not_found 是"文档里确实没有"（一个正确答案），
 * error 是"我们没能可靠地抽出来"。混在一起的话，空值看起来都像结论 ——
 * 那是抽取里最危险的输出。
 */
export interface FieldResult {
  status: FieldStatus
  value: string | number | boolean | null
  citations: Citation[]
  verified: boolean
  degraded: string | null
  confidence: RetrievalConfidence
}

/** 结果表格的一行：一份文档的一条记录（顶层 object 时每份文档只有一行）。 */
export interface ExtractionItem {
  id: string
  document_id: string
  filename: string
  parse_job_id: string | null
  record_index: number
  status: 'ok' | 'partial' | 'failed'
  degraded: string | null
  error: string | null
  fields: Record<string, FieldResult>
}

export interface ExtractionTemplate {
  id: string
  name: string
  description: string
  schema_json: Record<string, unknown>
  field_count: number
  kind: SchemaKind
  created_at: string
  updated_at: string
}

export interface ExtractionRun {
  id: string
  name: string
  template_id: string | null
  kind: SchemaKind
  status: RunStatus
  document_count: number
  done_count: number
  error: string | null
  /** 结果表格的列顺序，来自 run 落库时的 schema **快照** */
  field_names: string[]
  schema_json: Record<string, unknown>
  model_meta: Record<string, unknown>
  created_at: string
}

export interface ExtractionRunDetail {
  run: ExtractionRun
  items: ExtractionItem[]
}
