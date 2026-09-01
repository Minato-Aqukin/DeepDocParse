/**
 * 后端返回体的类型。与 `services/corpus-api/ddp_corpus/routers/*.py` 的
 * response_model 一一对应。
 *
 * **枚举不在这里定义**：`ParseStatus` / `IndexStatus` / `Degraded` / `SourceType` …
 * 全部从 `@deepdocparse/contracts` 再导出，取值由
 * `packages/contracts/enums.yaml` 生成。以前它们在这里手写一份、
 * 在 `constants/status.ts` 手写一份文案、后端再写一份字面量 —— 三处漂开的表现是
 * 界面上出现原始英文枚举，或者某条降级在 UI 上干脆不存在。
 */
export type {
  CodeDetection,
  CompileStatus,
  Degraded,
  FieldStatus,
  IndexStatus,
  ParseStatus,
  RunStatus,
  SourceType,
} from '@deepdocparse/contracts'

// `export type { ... } from` 只是转发，不会把名字带进本文件的作用域，
// 而下面的接口定义要用它们 —— 所以再 import 一次
import type {
  CodeDetection,
  CompileStatus,
  Degraded,
  FieldStatus,
  IndexStatus,
  ParseStatus,
  RunStatus,
  SourceType,
} from '@deepdocparse/contracts'

/** 下载格式不是后端枚举，是前端拼 URL 用的字面量集合，故留在本文件。 */
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
  compile_status: CompileStatus
  compile_degraded: string[]
  compile_fingerprint: string
  layout_version: string
  code_detection: CodeDetection
  current_job_id: string | null
  created_at: string
  /** 全部上传者的用户名。语料共享后同一份文件可能好几个人先后传过 */
  uploaders: string[]
  /** 当前用户能不能删这份文档（上传者或管理员）—— 删除是全站唯一还判权限的动作 */
  can_delete: boolean
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
  document_version: number
}

export interface IndexValidation {
  status: 'current' | 'stale' | 'unresolved' | 'uncompiled'
  observed_fingerprints: string[]
  expected_fingerprint: string
  reasons: string[]
  citation_reconnectable: number
  citation_invalidations: number
  safe_to_reindex: boolean
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
  evidence_id: string | null
  source_type: SourceType
  derived_from?: string | null
  /**
   * 当前索引里对应的块。**接不回来时是 null**（阶段 3 起）——
   * 后端宁可不给，也不会回放一个失效的旧值：给了就等于让前端把高亮指到错块。
   * 点击定位时它会让 selectedChunkId 变成 null，于是没有任何块被选中，
   * 而 `resolved === false` 会让这条出处显示成失效样式。
   */
  chunk_id: string | null
  /** 稳定定位键：chunk_id 每次 reindex 都会重铸，(parse_job_id, seq) 不会 */
  parse_job_id: string | null
  seq: number | null
  page_idx: number
  bbox: [number, number, number, number] | null
  /** 引用当时 bbox 所在页面的坐标基准；缺失时不得拿当前解析版本的尺寸猜。 */
  page_size: [number, number] | null
  crop_url: string | null
  snippet: string
  /** RRF 融合分：只用来排序，**不是**相关度（上限约 0.033，由名次决定） */
  score: number
  /** 余弦相似度：有校准量纲，这才是"有多相关"。关键词路命中/向量化挂了时为 null */
  similarity: number | null
  /** 这条出处还能不能接回当前索引里的原文（reindex/换引擎重解析后可能接不回） */
  resolved: boolean
}

export interface QueryDecision {
  need_retrieval: boolean
  reason: string
  inherited_evidence_ids: string[]
  degraded: string | null
}

export interface CandidateDecision {
  evidence_id: string | null
  document_id: string
  chunk_id?: string | null
  rank: number
  score: number | null
  similarity: number | null
  accepted: boolean
  reason: string
}

export interface AssertionVerification {
  state: 'passed' | 'rejected' | 'questioned' | 'unverified'
  mode: 'auto' | 'human' | null
}

/** 回答的语义真相；无 evidence_ids 时 unsupported 必须为 true。 */
export interface AnswerAssertion {
  id: string | null
  position: number
  text: string
  evidence_ids: string[]
  verification: AssertionVerification
  unsupported: boolean
  citations: Citation[]
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
  assertions?: AnswerAssertion[]
  query_decision?: QueryDecision
  retrieval?: { candidates: CandidateDecision[] }
  verified: boolean
  degraded: string | null
  /** 这一轮用的模型与检索参数快照，换模型后靠它分组对比历史 */
  model_meta?: Record<string, unknown>
  confidence?: RetrievalConfidence
  created_at: string
}

export interface EvidenceVerification {
  id: string
  mode: 'auto' | 'human'
  verdict: 'pass' | 'reject' | 'question'
  reason_code: string | null
  reason_text: string | null
  reviewer_id: string | null
  created_at: string
}

export interface EvidenceDetail {
  id: string
  document: { id: string; filename: string }
  page_idx: number
  seq: number
  parse_job_id: string
  doc_version: number
  bbox: [number, number, number, number] | null
  page_size: [number, number] | null
  kind: string
  content: string
  source_type: SourceType
  derived_from: string | null
  crop_url: string | null
  review_state: 'unreviewed' | 'passed' | 'rejected' | 'questioned'
  chunk_id: string | null
  verifications: EvidenceVerification[]
}

export type KnowledgeReviewState = 'unreviewed' | 'passed' | 'rejected' | 'questioned'

export interface KnowledgeEntity {
  id: string
  canonical_name: string
  normalized_name: string
  entity_type: string
  aliases: string[]
  merged_by: string
  merge_confidence: number
  entity_merge_uncertain: boolean
  split_from_id: string | null
  review_state: KnowledgeReviewState
  provider: Record<string, unknown>
}

export interface KnowledgeEdge {
  id: string
  subject_id: string
  predicate: string
  object_id: string
  confidence: number
  evidence_ids: string[]
  unsupported: boolean
  review_state: KnowledgeReviewState
  provider: Record<string, unknown>
  citations: Citation[]
}

export interface KnowledgeGraph {
  graph_version: 'ddp-graph/1'
  entities: KnowledgeEntity[]
  edges: KnowledgeEdge[]
}

export interface WikiSummary {
  id: string
  entity: KnowledgeEntity
  title: string
  outline: string[]
  provider: Record<string, unknown>
}

export interface WikiSentence {
  id: string
  text: string
  evidence_ids: string[]
  unsupported: boolean
  conflict_group: string | null
  review_state: KnowledgeReviewState
  provider: Record<string, unknown>
  citations: Citation[]
}

export interface WikiDetail {
  entry: WikiSummary
  sections: { id: string; heading: string; sentences: WikiSentence[] }[]
}

export interface EvidenceBacklink {
  source_kind: 'assertion' | 'extract_field' | 'graph_edge' | 'wiki_sentence'
  source_id: string
  role: string
  label: string
}

export interface KnowledgeReviewItem {
  target_kind: 'graph_edge' | 'wiki_sentence' | 'entity_merge' | 'extract_field'
  target_id: string
  label: string
  review_state: KnowledgeReviewState
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
