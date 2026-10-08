import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'

import RecordTable from '../RecordTable.vue'
import type { ExtractionItem } from '@/types/api'

function item(status: ExtractionItem['status'], filename: string): ExtractionItem {
  return {
    id: `item-${status}`, document_id: 'd1', filename, parse_job_id: 'job-1',
    record_index: 0, status, degraded: null, error: null, fields: {},
  }
}

describe('RecordTable 行状态文案', () => {
  it("行状态走契约文案：partial→'部分完成'、failed→'失败'，ok 不打标", async () => {
    // 手写的"部分"曾经跟契约的"部分完成"对不上：这里按契约逐字断言，
    // 改回旧字面量就红（check_enum_usage.py 只查枚举值，查不到文案漂移）。
    const wrapper = mount(RecordTable, {
      props: {
        items: [item('ok', 'ok.pdf'), item('partial', 'partial.pdf'), item('failed', 'failed.pdf')],
        fieldNames: [],
      },
      global: { plugins: [ElementPlus] },
    })
    // el-table 的行是异步画出来的：只 nextTick 一次还什么都找不到
    await wrapper.vm.$nextTick()
    await new Promise((resolve) => { setTimeout(resolve, 50) })
    await wrapper.vm.$nextTick()
    const tags = wrapper.findAll('.ddp-status').map((t) => t.text())
    expect(tags).toContain('部分完成')
    expect(tags).toContain('失败')
    expect(tags).not.toContain('部分')
    // ok 行不打 tag：三个 tag 位里只有两个有字（partial/failed 各一）
    expect(tags).toHaveLength(2)
    // ok 行的文件名仍在（行没丢，只是没标）
    expect(wrapper.text()).toContain('ok.pdf')
    wrapper.unmount()
  })
})
