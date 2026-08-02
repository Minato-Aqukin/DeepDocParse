/**
 * 解析引擎与其可调参数的 schema。
 *
 * **加一个引擎 = 在这里加一条配置**，上传对话框与重解析表单会自动长出对应的字段——
 * 它们共用 EngineOptionsForm，按 schema 渲染，没有任何一处写死 mineru。
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
]

export const DEFAULT_ENGINE = ENGINES[0]!.engine

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
