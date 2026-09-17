import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

import {
  EVIDENCE_SUFFICIENCY_VALUES,
  FEDERATED_ANSWER_REASON_VALUES,
  TASK_EVENT_TYPE_VALUES,
} from '@deepdocparse/contracts'
import { describe, expect, it } from 'vitest'

import {
  EVIDENCE_SUFFICIENCY,
  PAYLOAD_KIND_LABEL,
  PROBE_PAYLOAD_LABEL,
  TASK_EVENT_TYPE,
  answerReason,
  metaOf,
  payloadKindLabel,
  probePayloadLabel,
} from '../federation'

describe('answerReason', () => {
  it('代码查契约文案，细节附在括号里', () => {
    expect(answerReason('peer_unavailable:http_503')?.label).toBe('远端生成节点不可用（http_503）')
    expect(answerReason('insufficient_evidence')?.label).toBe('证据不足，未生成答案')
    expect(answerReason('insufficient_evidence')?.type).toBe('warning')
  })

  it('契约里没有的代码显示原始值，不给空白；没有原因就是 null', () => {
    expect(answerReason('model_sleeping')).toEqual({ label: '未知原因（model_sleeping）', type: 'danger' })
    expect(answerReason(null)).toBeNull()
  })

  it('契约里的每个答案原因都有文案', () => {
    for (const value of FEDERATED_ANSWER_REASON_VALUES) {
      expect(answerReason(value)?.label).not.toBe(`未知原因（${value}）`)
    }
  })
})

describe('metaOf', () => {
  it('契约里的每个取值都有文案；未知取值显示原始代码', () => {
    for (const value of EVIDENCE_SUFFICIENCY_VALUES) expect(metaOf(EVIDENCE_SUFFICIENCY, value).label).not.toBe(`未知取值（${value}）`)
    for (const value of TASK_EVENT_TYPE_VALUES) expect(metaOf(TASK_EVENT_TYPE, value).label).not.toBe(`未知取值（${value}）`)
    expect(metaOf(EVIDENCE_SUFFICIENCY, 'vibes').label).toBe('未知取值（vibes）')
    expect(metaOf(EVIDENCE_SUFFICIENCY, null).label).toBe('—')
  })
})

/**
 * 两张载荷文案表抄自 JSON schema 的内联枚举（它们不在 `enums.yaml` 里，所以没有生成物）。
 * **抄的东西会漂**，而且两个方向都是静默的：
 *
 * - schema 多一类而这里少一条 → 用户在外发许可上勾不到它，对应远端目标被记成
 *   `denied`，看起来像"对方拒绝了"，其实是界面没给授权的机会；
 * - 这里多一条 schema 没有的 → 请求带着非法取值打过去，403。
 *
 * 所以逐字比对**真正的 schema 文件**，不比对另一份手抄。
 */
/** 从 cwd 往上找到 `packages/contracts/schemas` —— 从仓库根还是从 apps/web 起测都一样。 */
function schemasDir(): string {
  for (let dir = process.cwd(); ; dir = dirname(dir)) {
    const candidate = resolve(dir, 'packages/contracts/schemas')
    if (existsSync(candidate)) return candidate
    if (dirname(dir) === dir) throw new Error('找不到 packages/contracts/schemas')
  }
}

/** 取 `$defs.<定义名>` 里某个内联 enum；缺了就抛，不静默当成空集合让断言变成恒真。 */
function schemaEnum(file: string, definition: string, at: (node: Record<string, unknown>) => unknown): string[] {
  const defs = JSON.parse(readFileSync(resolve(schemasDir(), file), 'utf8')).$defs as Record<string, unknown>
  const node = defs[definition]
  if (!node) throw new Error(`${file} 里没有 ${definition}`)
  const values = at(node as Record<string, unknown>)
  if (!Array.isArray(values) || !values.length) throw new Error(`${file}#${definition} 的取值集合是空的`)
  return values as string[]
}

describe('载荷文案表与契约 schema 一致', () => {
  it('外发许可可勾选的载荷类别 = ExplorationConsent.allowed_payload 的取值', () => {
    const declared = schemaEnum('ddp-task-probe/v1.json', 'ExplorationConsent',
      (node) => (node as { properties: { allowed_payload: { items: { enum: string[] } } } })
        .properties.allowed_payload.items.enum)
    expect(Object.keys(PROBE_PAYLOAD_LABEL).sort()).toEqual([...declared].sort())
  })

  it('数据边上的外发内容 = DataEdge.payload_kind 的取值', () => {
    const declared = schemaEnum('ddp-plan-admission/v1.json', 'DataEdge',
      (node) => (node as { properties: { payload_kind: { enum: string[] } } }).properties.payload_kind.enum)
    expect(Object.keys(PAYLOAD_KIND_LABEL).sort()).toEqual([...declared].sort())
  })

  it('认不出的取值显示原始代码，不给空白', () => {
    expect(probePayloadLabel('query_text')).toBe('问题原文')
    expect(probePayloadLabel('brain_scan')).toBe('brain_scan')
    expect(payloadKindLabel('embeddings')).toBe('向量')
    expect(payloadKindLabel('brain_scan')).toBe('brain_scan')
  })
})
