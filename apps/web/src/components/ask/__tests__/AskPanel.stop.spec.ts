import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

type Handlers = { onDelta: (piece: string) => void }
let handlers: Handlers | undefined
const abort = vi.fn()

vi.mock('@/api', () => ({
  askStream: vi.fn((_cid: string, _text: string, h: Handlers) => {
    handlers = h
    return abort
  }),
  conversationsApi: {
    list: vi.fn(async () => ({ data: [{ id: 'c1', title: '会话' }] })),
    messages: vi.fn(async () => ({ data: [] })),
    create: vi.fn(), remove: vi.fn(),
  },
}))

vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(async () => null),
  renderMarkdown: (value: string) => value,
  resolveAuthedImages: async () => () => {},
}))

import AskPanel from '../AskPanel.vue'

const document = {
  id: 'd1', filename: 'manual.pdf', index_status: 'ready', index_error: null,
} as never

// Element Plus 在单测里没注册（见 src/__tests__/setup.ts）：el-button 渲染成原样标签、
// click 照常触发；el-input 没有 v-model 行为，换一个最小的 textarea 桩
const stubs = {
  'el-input': {
    props: ['modelValue'], emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}

describe('AskPanel 停止回答', () => {
  it('停止后立刻显示"回答被中断"并留下已产出的文字，与服务端落库的状态一致', async () => {
    const wrapper = mount(AskPanel, { props: { document }, global: { stubs } })
    await flushPromises()
    await wrapper.find('textarea').setValue('复位延时是多少？')
    await wrapper.findAll('el-button').find((b) => b.text() === '发送')!.trigger('click')
    await flushPromises()
    handlers!.onDelta('复位延时是 17')
    await flushPromises()

    await wrapper.findAll('el-button').find((b) => b.text() === '停止')!.trigger('click')
    await flushPromises()
    expect(abort).toHaveBeenCalled()
    expect(wrapper.text()).toContain('回答被中断')
    expect(wrapper.text()).toContain('复位延时是 17')
  })
})
