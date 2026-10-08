import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DocumentFilters from '../DocumentFilters.vue'
import type { DocumentFilters as Filters } from '@/stores/documents'

const modelValue: Filters = { q: '', status: '', indexStatus: '' }

function mountFilters() {
  // el-select 自带递归更新问题时只桩掉下拉：el-input 保持真实行为，
  // setValue/回车/清空键的断言才有意义。
  return mount(DocumentFilters, {
    props: { modelValue: { ...modelValue } },
    global: { plugins: [ElementPlus], stubs: { 'el-select': true, 'el-option': true } },
  })
}

describe('DocumentFilters 关键词防抖', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('连续输入只发一次 change，值是最后一次', async () => {
    const wrapper = mountFilters()
    const input = wrapper.find('input')
    await input.setValue('a')
    await input.setValue('ab')
    await input.setValue('abc')
    expect(wrapper.emitted('change')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(250)
    expect(wrapper.emitted('change')).toEqual([[{ q: 'abc' }]])
    wrapper.unmount()
  })

  it('回车不等防抖：先同步关键词再进全文检索', async () => {
    const wrapper = mountFilters()
    await wrapper.find('input').setValue('手册')
    await wrapper.find('input').trigger('keyup.enter')
    // flushQuery 同步执行，不用推进时钟
    expect(wrapper.emitted('change')).toEqual([[{ q: '手册' }]])
    expect(wrapper.emitted('search')).toHaveLength(1)
    // 回车后防抖定时器不许再补发一次 change：推进时钟确认没有第二次 emit。
    // （flushQuery 见 pending 为 null 会自己停，所以这里钉的是"行为无重复"，
    // 不是 clearTimeout 那一行本身 —— 删 clearTimeout 也绿，但行为没变。）
    await vi.advanceTimersByTimeAsync(300)
    expect(wrapper.emitted('change')).toHaveLength(1)
    wrapper.unmount()
  })

  it('清空键立即生效', async () => {
    const wrapper = mount(DocumentFilters, {
      props: { modelValue: { ...modelValue, q: 'abc' } },
      global: { plugins: [ElementPlus], stubs: { 'el-select': true, 'el-option': true } },
    })
    // 点 × 走 clear 事件：立即 emit q:''，不等防抖
    await wrapper.find('.el-input__clear').trigger('click')
    expect(wrapper.emitted('change')).toEqual([[{ q: '' }]])
    wrapper.unmount()
  })
})
