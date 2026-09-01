import { http } from './http'
import type {
  ExtractionRun,
  ExtractionRunDetail,
  ExtractionTemplate,
  SchemaField,
  SchemaKind,
} from '@/types/api'

/**
 * 结构化抽取的 API。
 *
 * 后端只收**受限 JSON Schema**（顶层 object 或 array，叶子必须带 description，
 * 不支持嵌套/oneOf/$ref）—— 边界与理由见 ../DeepDocParse/docs/extract-format.md。
 * `buildSchema` / `readSchema` 是编辑器那套扁平字段列表与 schema 之间的双向转换，
 * **收在这一处**：两边各写一份迟早会漂，而漂了的表现是"保存后字段少了一个"。
 */
export const extractionsApi = {
  listTemplates: () =>
    http.get<ExtractionTemplate[]>('/api/extractions/templates').then((r) => r.data),

  createTemplate: (payload: { name: string; description: string; schema_json: unknown }) =>
    http.post<ExtractionTemplate>('/api/extractions/templates', payload).then((r) => r.data),

  updateTemplate: (
    id: string,
    payload: { name: string; description: string; schema_json: unknown },
  ) => http.put<ExtractionTemplate>(`/api/extractions/templates/${id}`, payload).then((r) => r.data),

  deleteTemplate: (id: string) => http.delete(`/api/extractions/templates/${id}`),

  listRuns: () => http.get<ExtractionRun[]>('/api/extractions/runs').then((r) => r.data),

  createRun: (payload: {
    document_ids: string[]
    template_id?: string
    schema_json?: unknown
    name?: string
    verify?: boolean
  }) => http.post<ExtractionRun>('/api/extractions/runs', payload).then((r) => r.data),

  getRun: (id: string) =>
    http.get<ExtractionRunDetail>(`/api/extractions/runs/${id}`).then((r) => r.data),

  deleteRun: (id: string) => http.delete(`/api/extractions/runs/${id}`),

  exportUrl: (id: string) => `/api/extractions/runs/${id}/export.csv`,
}

/** 扁平字段列表 -> 受限 JSON Schema。 */
export function buildSchema(fields: SchemaField[], kind: SchemaKind): Record<string, unknown> {
  const properties: Record<string, unknown> = {}
  const required: string[] = []
  for (const field of fields) {
    const name = field.name.trim()
    if (!name) continue
    const prop: Record<string, unknown> = {
      type: field.type,
      description: field.description.trim(),
    }
    if (field.format) prop.format = field.format
    // 空数组会被后端判成"enum 必须是非空数组"，这里直接不写这个键
    if (field.enum?.length) prop.enum = field.enum
    properties[name] = prop
    if (field.required) required.push(name)
  }
  const node: Record<string, unknown> = { type: 'object', properties }
  if (required.length) node.required = required
  return kind === 'array' ? { type: 'array', items: node } : node
}

/** 受限 JSON Schema -> 扁平字段列表（编辑已有模板时用）。 */
export function readSchema(schema: Record<string, unknown> | undefined): {
  fields: SchemaField[]
  kind: SchemaKind
} {
  const root = (schema ?? {}) as Record<string, unknown>
  const kind: SchemaKind = root.type === 'array' ? 'array' : 'object'
  const node = (kind === 'array' ? root.items : root) as Record<string, unknown> | undefined
  const properties = (node?.properties ?? {}) as Record<string, Record<string, unknown>>
  const required = new Set((node?.required as string[] | undefined) ?? [])
  const fields: SchemaField[] = Object.entries(properties).map(([name, prop]) => ({
    name,
    type: (prop.type as SchemaField['type']) ?? 'string',
    description: String(prop.description ?? ''),
    format: (prop.format as string | undefined) ?? '',
    enum: (prop.enum as string[] | undefined) ?? [],
    required: required.has(name),
  }))
  return { fields, kind }
}
