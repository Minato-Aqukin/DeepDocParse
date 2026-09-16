import {
  EVIDENCE_SUFFICIENCY_VALUES,
  FEDERATED_ANSWER_REASON_VALUES,
  TASK_EVENT_TYPE_VALUES,
} from '@deepdocparse/contracts'
import { describe, expect, it } from 'vitest'

import { EVIDENCE_SUFFICIENCY, TASK_EVENT_TYPE, answerReason, metaOf } from '../federation'

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
