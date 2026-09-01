import { describe, expect, it } from 'vitest'

import { DEGRADED_LABEL, degradedLabelOf } from '../status'

/**
 * 用例 4 的一半：**后端返回降级标记时界面必须显示原因，不许静默。**
 * 对应 plan.md §9 不变式 2「任何降级都必须可见」。
 *
 * 这条守的是文案层：**未知的降级值也要给出可读文案**。
 * 后端加一种降级、前端忘了加标签时，用户至少要看到"已降级（xxx）"，
 * 而不是一片空白 —— 空白与"一切正常"在界面上是分不出来的。
 */
describe('降级文案', () => {
  it('已知降级值给出中文说明', () => {
    // 抽取平面第十种降级（2026-08-26 加的），前端必须认得
    expect(degradedLabelOf('no_instruct_model')).toBe(DEGRADED_LABEL.no_instruct_model)
    expect(degradedLabelOf('embedding_unavailable')).toBeTruthy()
  })

  it('未知降级值也要有可读文案，绝不返回空，也不许把原始枚举丢给用户', () => {
    const raw = 'something_invented_next_year'
    const label = degradedLabelOf(raw)
    expect(label).toBeTruthy()
    // 原始枚举值要留在文案里，否则排查时无从下手
    expect(label).toContain(raw)
    // **但不能只有它。** `?? value` 这种写法（把英文枚举原样丢给用户）
    // 正是 status.ts 注释里明令禁止的，而只断言 toContain 的话它照样绿
    expect(label).not.toBe(raw)
  })

  it('没有降级才返回 null', () => {
    expect(degradedLabelOf(null)).toBeNull()
    expect(degradedLabelOf('')).toBeNull()
  })

  it('service 契约与 Web 并发路径的降级值前端全都认得', () => {
    // 前十种与 DeepDocParse/gateway/app/services/extract_format.py 的 DEGRADED_VALUES 同源；
    // index_changed_during_answer 是 Web 流式落库与重建并发时独有。
    // 漏一个的话，界面上会退化成"已降级（英文枚举）"——不算静默，但很难看懂
    const contract = [
      'no_hits', 'embedding_unavailable', 'vision_unavailable', 'crop_unsupported',
      'crop_failed', 'parse_mismatch', 'upstream_error', 'schema_violation',
      'rerank_unavailable', 'no_instruct_model', 'index_changed_during_answer',
      'decision_unavailable', 'no_evidence_in_turn', 'inherited_evidence_incomplete',
      'gate_rejected_all', 'citation_persist_failed',
      'verification_unavailable',
    ]
    const missing = contract.filter((v) => !(v in DEGRADED_LABEL))
    expect(missing).toEqual([])
  })
})
