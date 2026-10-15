/**
 * 已知引擎的**参数 schema 目录**（catalog），按注册表名索引。
 *
 * 注意这张表**不是**"本站有哪些引擎" —— 那个问题的唯一答案是
 * `GET /api/parse-engines`（见 `composables/useParseEngines.ts`），注册表里有
 * 什么前端就列什么。以前这张表同时干这两件事，写死的后果是无 GPU 部署
 * 也列 mineru、有 DeepSeek-OCR-2 的部署却选不到它，选了没注册的引擎
 * 在网关侧收 404 unknown_engine —— 现在存在性由服务端说了算。
 *
 * 这张表只回答"一个引擎有哪些可调参数、中文名叫什么"。服务端返回的引擎
 * 即使在这里没有 schema（注册表加了新引擎但前端还没跟上）**仍然可选**，
 * 只是按"名字本身、无参数"渲染 —— 加引擎不再需要前端发版。
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
        // 取值是 mineru-api 3.4.4 的 backend 枚举（不认识的值它直接 400 Invalid backend）
        key: 'backend',
        label: '解析后端',
        type: 'select',
        default: 'pipeline',
        choices: [
          { value: 'pipeline', label: 'pipeline（小模型流水线，省显存）' },
          { value: 'hybrid-engine', label: 'hybrid（流水线 + 多模态大模型）' },
          { value: 'vlm-engine', label: 'vlm（多模态大模型）' },
        ],
        hint: 'hybrid / vlm 要部署侧装了 MinerU 的多模态模型，没装时任务会直接失败',
      },
      {
        // mineru 的表单字段叫 lang_list；写成 lang 会被它静默忽略
        key: 'lang_list',
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
  {
    engine: 'vlm-ocr',
    label: '大模型 OCR',
    description:
      '逐页送多模态 OCR 模型识别，适合扫描件/表格/公式；每页一次模型调用、较慢、要 GPU；坐标来自模型输出',
    // 模型由注册表那一行的 options.model 决定，不是用户可调的参数
    fields: [],
  },
  // 托管 API 上的多模态模型（注册表 api_key_env 条目，见 infra/registry/models.autodl.yaml）。
  // 不占本机 GPU，无卡模式开机也能用；按页计 token 费
  {
    engine: 'vlm-ocr-deepseek',
    label: '大模型 OCR · DeepSeek API',
    description:
      '逐页把页面图发给 DeepSeek 的多模态模型识别；不占本机 GPU，按页计费；坐标来自模型输出，精度不如版面模型',
    fields: [],
  },
  {
    engine: 'vlm-ocr-kimi',
    label: '大模型 OCR · Kimi API',
    description:
      '逐页把页面图发给 Kimi 的多模态模型识别；不占本机 GPU，按页计费；坐标来自模型输出，精度不如版面模型',
    fields: [],
  },
]

/**
 * **没有前端的缺省引擎。** 服务端的 `default_engine` 才是权威的：
 * 存在性与缺省值都在 `GET /api/parse-engines` 里，前端只做"没清单时传空串
 * 让服务端决定"。以前这里有个 `DEFAULT_ENGINE`（可被 `VITE_DEFAULT_ENGINE`
 * 覆盖）与后端 `DEFAULT_PARSE_ENGINE`、models.yaml 三处人工对齐 ——
 * 对不上就是上传时的 404 unknown_engine，现在这三处只剩一处。
 */

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
