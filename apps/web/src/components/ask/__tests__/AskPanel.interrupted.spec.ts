import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

type Handlers = {
  onDelta: (piece: string) => void
  onError?: (data: { message: string; code: string }) => void
  onSettled?: () => void
}
let handlers: Handlers | undefined
const question = {
  id: 'u1', role: 'user', content: '复位延时是多少？', citations: [],
  verified: false, degraded: null, created_at: '2026-09-24T12:00:00Z',
}
const reply = { ...question, id: 'a1', role: 'assistant', content: '17 ms' }
const stored = vi.fn(async () => ({ data: [] as unknown[] }))
const listed = vi.fn(async () => ({ data: [{ id: 'c1', title: '会话' }] }))

vi.mock('@/api', () => ({
  askStream: vi.fn((_cid: string, _text: string, h: Handlers) => {
    handlers = h
    return () => {}
  }),
  conversationsApi: {
    list: () => listed(),
    messages: () => stored(),
    create: vi.fn(async () => ({ data: { id: 'c2', title: '新会话' } })),
    remove: vi.fn(),
  },
}))

vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(async () => null),
  renderMarkdown: (value: string) => value,
  resolveAuthedImages: async () => () => {},
}))

import AskPanel from '../AskPanel.vue'

const document = { id: 'd1', filename: 'manual.pdf', index_status: 'ready', index_error: null } as never
const stubs = {
  'el-input': {
    props: ['modelValue'], emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}

describe('AskPanel 回答中途断开', () => {
  it('流在 done 之前断开后以会话记录为准重读，没落下回答的问题标"没有回答"', async () => {
    const wrapper = mount(AskPanel, { props: { document }, global: { stubs } })
    await flushPromises()
    await wrapper.find('textarea').setValue('复位延时是多少？')
    await wrapper.findAll('el-button').find((b) => b.text() === '发送')!.trigger('click')
    await flushPromises()
    handlers!.onDelta('复位延时')
    expect(wrapper.text()).not.toContain('没有回答')      // 还在生成，不算没有回答

    stored.mockResolvedValue({ data: [question] })        // 服务端只落下了问题
    const reloads = stored.mock.calls.length
    handlers!.onError!({ message: '回答在完成前中断', code: 'stream_incomplete' })
    handlers!.onSettled!()
    await flushPromises()
    expect(stored.mock.calls.length).toBe(reloads + 1)
    expect(wrapper.text()).toContain('没有回答')
  })

  it('有回答跟着的问题不标', async () => {
    stored.mockResolvedValue({ data: [question, reply] })
    const wrapper = mount(AskPanel, { props: { document }, global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('17 ms')
    expect(wrapper.text()).not.toContain('没有回答')
  })

  it('新会话的第一问在回答中途断开、服务端又读不到时，问题留在面板上并标"没有回答"', async () => {
    listed.mockResolvedValueOnce({ data: [] })          // 文档上还没有会话：发送时新建
    stored.mockResolvedValue({ data: [] })
    const wrapper = mount(AskPanel, { props: { document }, global: { stubs } })
    await flushPromises()
    await wrapper.find('textarea').setValue('复位延时是多少？')
    await wrapper.findAll('el-button').find((b) => b.text() === '发送')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('复位延时是多少？')

    stored.mockRejectedValue(new Error('upstream_unreachable'))
    handlers!.onError!({ message: '回答在完成前中断', code: 'stream_incomplete' })
    handlers!.onSettled!()
    await flushPromises()
    expect(wrapper.text()).toContain('复位延时是多少？')
    expect(wrapper.text()).toContain('没有回答')
  })
})
