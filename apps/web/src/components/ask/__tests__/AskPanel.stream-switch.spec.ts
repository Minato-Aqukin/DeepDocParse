import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { ComponentPublicInstance } from 'vue'
import { describe, expect, it, vi } from 'vitest'

type StreamHandlers = {
  onDelta: (piece: string) => void
  onError?: (data: { message: string; code: string }) => void
  onDone?: () => void
  onSettled?: () => void
}
const streams: { cid: string; handlers: StreamHandlers }[] = []
const aborts: string[] = []

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  askStream: vi.fn((cid: string, _text: string, handlers: StreamHandlers) => {
    streams.push({ cid, handlers })
    return () => { aborts.push(cid) }
  }),
  conversationsApi: {
    list: vi.fn(async () => ({ data: [{ id: 'c1', title: '会话一' }, { id: 'c2', title: '会话二' }] })),
    messages: vi.fn(async () => ({ data: [] })),
    create: vi.fn(), remove: vi.fn(),
  },
}))

vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(async () => null),
  renderMarkdown: (value: string) => value,
  resolveAuthedImages: async () => () => {},
}))

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), warning: vi.fn(), info: vi.fn(), success: vi.fn() },
}))

import { ElMessage } from 'element-plus'
import AskPanel from '../AskPanel.vue'
import { conversationsApi } from '@/api'
import type { ChatMessage } from '@/types/api'

type PanelWrapper = VueWrapper<ComponentPublicInstance>

const baseDocument: Record<string, unknown> = {
  id: 'd1', filename: 'manual.pdf', index_status: 'ready', index_error: null,
}
const docOf = (id: string) => ({ ...baseDocument, id }) as never

// 会话下拉桩：v-model 的 update:modelValue 是它与面板的唯一契约面，
// 用例经它触发"用户在下拉里选了另一个会话"，走的是真实的 activeId watcher。
const SelectStub = {
  props: ['modelValue'], emits: ['update:modelValue'],
  template: '<div class="select-stub" />',
}
const stubs = {
  'el-input': {
    props: ['modelValue'], emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  'el-select': SelectStub,
  'el-option': { props: ['value', 'label'], template: '<span />' },
}

function userMessage(id: string, content: string): ChatMessage {
  return {
    id, role: 'user', content, citations: [],
    verified: false, degraded: null, created_at: new Date().toISOString(),
  }
}

async function sendQuestion(wrapper: PanelWrapper, text: string) {
  await wrapper.find('textarea').setValue(text)
  await wrapper.findAll('el-button').find((b) => b.text() === '发送')!.trigger('click')
  await flushPromises()
}

describe('AskPanel 切换与竞态', () => {
  it('切文档先掐掉旧流：被取代的四个回调都不许碰新会话', async () => {
    streams.length = 0
    aborts.length = 0
    // 不同文档的会话 id 不重合（真实后端的形状）：切文档必换 activeId
    vi.mocked(conversationsApi.list).mockImplementation(async (docId: string) => ({
      data: docId === 'd2' ? [{ id: 'c2', title: '会话二' }] : [{ id: 'c1', title: '会话一' }],
    } as never))
    const messagesMock = vi.mocked(conversationsApi.messages)
    messagesMock.mockResolvedValue({ data: [] } as never)
    const wrapper = mount(AskPanel, { props: { document: docOf('d1') }, global: { stubs } })
    await flushPromises()
    // 初始 activeId=c1（loadConversations 选首个）。在 c1 发起流
    await sendQuestion(wrapper, '第一问')
    expect(streams.map((s) => s.cid)).toEqual(['c1'])
    // 切文档：旧流必须被 abort，流状态清零
    await wrapper.setProps({ document: docOf('d2') })
    await flushPromises()
    expect(aborts).toContain('c1')
    // 新会话立刻开始自己的流（streaming=true）：旧的 delta 重放时流气泡是可见的，
    // 断言才有意义 —— 流气泡只在 streaming 时渲染，旧文本混进去会直接画出来。
    await sendQuestion(wrapper, '第二问')
    expect(streams.map((s) => s.cid)).toEqual(['c1', 'c2'])
    const messagesCalls = messagesMock.mock.calls.length
    const errorCalls = vi.mocked(ElMessage.error).mock.calls.length

    // 切走后旧流的回调依次晚到：全部丢掉，不许碰新会话的任何状态。
    streams[0]!.handlers.onDelta('旧会话的半截回答')
    streams[0]!.handlers.onError?.({ message: '旧流的上游报错', code: 'upstream_error' })
    await streams[0]!.handlers.onDone?.()
    await flushPromises()
    streams[0]!.handlers.onSettled?.()
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧会话的半截回答')
    expect(wrapper.text()).not.toContain('旧流的上游报错')
    expect(vi.mocked(ElMessage.error).mock.calls.length).toBe(errorCalls)
    expect(messagesMock.mock.calls.length).toBe(messagesCalls)
    // 旧流的 onSettled 不许复位新流的状态：新流的流气泡还在，"发送"键没回来
    expect(wrapper.findAll('el-button').some((b) => b.text() === '发送')).toBe(false)
    expect(wrapper.findAll('el-button').some((b) => b.text() === '停止')).toBe(true)
    // 新流自己的回调仍然生效：新回答照常画出来
    streams[1]!.handlers.onDelta('新会话的回答')
    await flushPromises()
    expect(wrapper.text()).toContain('新会话的回答')
  })

  it('同文档切会话先掐掉旧流：旧回调被 guard，新会话的消息照常读', async () => {
    streams.length = 0
    aborts.length = 0
    vi.mocked(conversationsApi.list).mockResolvedValue({
      data: [{ id: 'c1', title: '会话一' }, { id: 'c2', title: '会话二' }],
    } as never)
    vi.mocked(conversationsApi.messages).mockImplementation(async (cid: string) => ({
      data: cid === 'c2' ? [userMessage('m-c2', '消息-c2')] : [],
    } as never))
    const wrapper = mount(AskPanel, { props: { document: docOf('d1') }, global: { stubs } })
    await flushPromises()
    await sendQuestion(wrapper, '第一问')
    expect(streams.map((s) => s.cid)).toEqual(['c1'])
    // 同文档内经下拉切到 c2：走 activeId watcher（不是 loadConversations 的
    // created 压制路径）—— 旧流必须被 abort。
    wrapper.findComponent(SelectStub).vm.$emit('update:modelValue', 'c2')
    await flushPromises()
    expect(aborts).toContain('c1')
    expect(wrapper.text()).toContain('消息-c2')
    const errorCalls = vi.mocked(ElMessage.error).mock.calls.length
    // 旧流的回调晚到：画不出东西，也不报错、不复位（面板已不在流状态，
    // "发送"键在是正常的；关键是旧文本与旧报错都不许出现）。
    streams[0]!.handlers.onDelta('旧会话的半截回答')
    streams[0]!.handlers.onError?.({ message: '旧流的上游报错', code: 'upstream_error' })
    await streams[0]!.handlers.onDone?.()
    await flushPromises()
    streams[0]!.handlers.onSettled?.()
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧会话的半截回答')
    expect(wrapper.text()).not.toContain('旧流的上游报错')
    expect(vi.mocked(ElMessage.error).mock.calls.length).toBe(errorCalls)
    expect(wrapper.text()).toContain('消息-c2')
  })

  it('loadMessages 竞态：文档来回切换时旧响应晚到不许冲掉当前会话的消息', async () => {
    vi.mocked(conversationsApi.list).mockImplementation(async (docId: string) => ({
      data: docId === 'd2' ? [{ id: 'cB', title: 'B' }] : [{ id: 'cA', title: 'A' }],
    } as never))
    let resolveFirstA!: (value: { data: ChatMessage[] }) => void
    const firstAGate = new Promise<{ data: ChatMessage[] }>((res) => { resolveFirstA = res })
    let callsA = 0
    vi.mocked(conversationsApi.messages).mockImplementation(async (cid: string) => {
      if (cid === 'cA') {
        callsA++
        if (callsA === 1) return firstAGate as never
        return { data: [userMessage('m-cA-new', '新消息-cA')] } as never
      }
      return { data: [userMessage('m-cB', '消息-cB')] } as never
    })

    const wrapper = mount(AskPanel, { props: { document: docOf('d1') }, global: { stubs } })
    await flushPromises()
    // d1 的首个 messages(cA) 被门住；切到 d2 读到 cB 的消息
    await wrapper.setProps({ document: docOf('d2') })
    await flushPromises()
    expect(wrapper.text()).toContain('消息-cB')
    // 再切回 d1：读到 cA 的新消息
    await wrapper.setProps({ document: docOf('d1') })
    await flushPromises()
    expect(wrapper.text()).toContain('新消息-cA')
    // 最早那次 cA 请求晚到：直接丢掉，不许冲掉当前消息
    resolveFirstA({ data: [userMessage('m-cA-old', '旧消息-cA')] })
    await flushPromises()
    expect(wrapper.text()).toContain('新消息-cA')
    expect(wrapper.text()).not.toContain('旧消息-cA')
  })

  it('面板打开只读一次消息：loadConversations 自己读，watcher 那次被压住', async () => {
    const listMock = vi.mocked(conversationsApi.list)
    const messagesMock = vi.mocked(conversationsApi.messages)
    let resolveList!: (value: { data: { id: string; title: string }[] }) => void
    const listGate = new Promise<{ data: { id: string; title: string }[] }>((res) => { resolveList = res })
    listMock.mockReturnValue(listGate as never)
    messagesMock.mockResolvedValue({ data: [] } as never)

    mount(AskPanel, { props: { document: docOf('d1') }, global: { stubs } })
    resolveList({ data: [{ id: 'solo', title: '唯一会话' }] })
    await flushPromises()
    await flushPromises()
    // activeId 从 '' 变成 'solo' 只触发一次 watcher，但那次被 created 标记压住；
    // loadConversations 自己读的一次是唯一的消息请求
    expect(messagesMock).toHaveBeenCalledTimes(1)
    expect(messagesMock).toHaveBeenCalledWith('solo')
  })

  it('无 assertions 的 assistant 消息标"无逐条证据支持"；有 citations 的旧形状不标', async () => {
    const listMock = vi.mocked(conversationsApi.list)
    const messagesMock = vi.mocked(conversationsApi.messages)
    listMock.mockResolvedValue({ data: [{ id: 'c1', title: '会话' }] } as never)
    const legacy: ChatMessage = {
      id: 'm-legacy', role: 'assistant', content: '历史回答文本',
      verified: false, degraded: null, created_at: new Date().toISOString(),
      citations: [], assertions: [],
    }
    const withCites: ChatMessage = {
      id: 'm-cites', role: 'assistant', content: '带旧出处的回答',
      verified: false, degraded: null, created_at: new Date().toISOString(),
      citations: [{
        evidence_id: 'e1', source_type: 'source', chunk_id: 'ch1', parse_job_id: 'j1',
        seq: 1, page_idx: 0, bbox: [1, 2, 3, 4], page_size: [100, 100], crop_url: null,
        snippet: '原文', score: 0.02, similarity: 0.9, resolved: true,
      }],
    }
    messagesMock.mockResolvedValue({ data: [legacy, withCites] } as never)

    const wrapper = mount(AskPanel, { props: { document: docOf('d1') }, global: { stubs } })
    await flushPromises()
    const bubbles = wrapper.findAll('.bubble').map((b) => b.text())
    expect(bubbles[0]).toContain('无逐条证据支持')
    expect(bubbles[1]).not.toContain('无逐条证据支持')
  })
})
