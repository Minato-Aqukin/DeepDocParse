/**
 * 解析引擎与其可调参数的 schema。
 *
 * **加一个引擎 = 在这里加一条配置**，上传对话框与重解析表单会自动长出对应的字段——
 * 它们共用 EngineOptionsForm，按 schema 渲染，表单本身不认识任何具体引擎。
 *
 * 但**这张表自己就是一处硬编码**：它是手工维护的，与后端 DEFAULT_PARSE_ENGINE、
 * service 的 models.yaml 三处必须人工对齐，对不上就是上传时的 404 unknown_engine。
 * 真正的解法是让 service 的 /v1/models 把 parse_engines 也列出来、由前端动态生成，
 * 那是向后兼容的新增，但契约目前没有这个能力（见 openapi.yaml）。
 *
 * 参数名要与 service 侧 models.yaml 的引擎透传选项对齐：后端只是原样转发 options。
 */
export type FieldType = 'select' | 'text' | 'switch' | 'number'

export interface EngineField {
  key: string
  label: string
  type: FieldType
  /** select 用 */
  choices?: { value: string; label: string }[]
  default?: unknown
  placeholder?: string
  hint?: string
}

export interface EngineSchema {
  engine: string
  label: string
  description?: string
  fields: EngineField[]
}

export const ENGINES: EngineSchema[] = [
  {
    engine: 'mineru',
    label: 'MinerU',
    description: '版面解析 + OCR，输出 Markdown 与带页码/坐标的版面结构',
    fields: [
      {
        key: 'backend',
        label: '解析后端',
        type: 'select',
        default: 'pipeline',
        choices: [
          { value: 'pipeline', label: 'pipeline（小模型流水线，省显存）' },
          { value: 'vlm', label: 'vlm（多模态大模型，效果更好）' },
        ],
        hint: 'dev 机显存有限时用 pipeline',
      },
      {
        key: 'lang',
        label: '语言',
        type: 'text',
        default: '',
        placeholder: '留空自动识别，如 ch / en',
      },
    ],
  },
  {
    engine: 'borndigital',
    label: 'Born-digital（无 GPU）',
    description: '直接抽 PDF 文字层与坐标，出处三件套一样齐全；不处理扫描件、表格结构与公式',
    // 没有可调参数：抽的是文字层本身，没有后端/语言可选
    fields: [],
  },
]

/**
 * 缺省引擎。**必须与后端 DEFAULT_PARSE_ENGINE、service 的 models.yaml 三者对齐**——
 * 任一处对不上，上传会在 service 侧收 404 unknown_engine。
 * 无 GPU 部署（models.cpu.yaml）把它设成 borndigital。
 */
export const DEFAULT_ENGINE =
  (import.meta.env.VITE_DEFAULT_ENGINE as string | undefined) || ENGINES[0]!.engine

export function schemaOf(engine: string): EngineSchema | undefined {
  return ENGINES.find((e) => e.engine === engine)
}

/** schema 的默认值（空串视为"不传"，避免把空参数塞给引擎）。 */
export function defaultOptions(engine: string): Record<string, unknown> {
  const options: Record<string, unknown> = {}
  for (const field of schemaOf(engine)?.fields ?? []) {
    if (field.default !== undefined && field.default !== '') options[field.key] = field.default
  }
  return options
}

/** 提交前清掉空值：后端把 options 原样透传给引擎，空串会变成无意义的参数。 */
export function pruneOptions(options: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(options).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  )
}
