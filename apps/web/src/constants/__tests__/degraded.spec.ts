import { DEGRADED_META, DEGRADED_VALUES } from '@deepdocparse/contracts'
import { describe, expect, it } from 'vitest'

import { DEGRADED_LABEL, DEGRADED_TAG, degradedLabelOf } from '../status'

/**
 * 用例 4 的一半：**后端返回降级标记时界面必须显示原因，不许静默。**
 * 对应不变式 2「任何降级都必须可见」。
 *
 * 合仓后取值与文案都由 `packages/contracts/enums.yaml` 生成，所以
 * 「前端漏加一个降级值」这件事已经在结构上不可能了 —— 原来那条
 * 手抄 17 个值再比对的用例因此变成了**第四份复制品**，删掉。
 * 「Python 里出现契约外的取值」由 `scripts/check_enum_usage.py` 守
 * （它在合仓当天就扫出两个：`empty_query` / `answer_unavailable`）。
 *
 * 这里剩下的是**映射层**的守卫：契约到 UI 之间那一小段仍是手写的。
 */
describe('降级文案', () => {
  it('每个契约取值都有非空中文文案与标签色', () => {
    // 反哨兵：契约是空的时候下面的 forEach 一次都不跑，会假绿
    expect(DEGRADED_VALUES.length).toBeGreaterThan(15)
    for (const value of DEGRADED_VALUES) {
      expect(DEGRADED_LABEL[value], `${value} 没有文案`).toBeTruthy()
      // 文案不能就是枚举名本身 —— 那等于把英文原样丢给用户
      expect(DEGRADED_LABEL[value]).not.toBe(value)
      expect(DEGRADED_TAG[value], `${value} 没有标签色`).toBeTruthy()
    }
  })

  it('语义色映射覆盖契约里出现过的每一种 severity', () => {
    const used = new Set(Object.values(DEGRADED_META).map((m) => m.severity))
    expect(used.size).toBeGreaterThan(1)
    for (const value of DEGRADED_VALUES) {
      expect(['info', 'warning', 'danger', 'success', 'primary']).toContain(DEGRADED_TAG[value])
    }
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
})
