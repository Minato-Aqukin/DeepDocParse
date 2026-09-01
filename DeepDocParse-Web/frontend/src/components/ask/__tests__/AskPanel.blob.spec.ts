import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import { leakedObjectUrls } from '@/__tests__/setup'

// 必须在 import 组件之前打桩
vi.mock('@/api', () => ({
  askStream: vi.fn(() => () => {}),
  conversationsApi: {
    list: vi.fn(async () => ({ data: [{ id: 'c1', title: '会话' }] })),
    messages: vi.fn(async () => ({
      data: [{
        id: 'm1', role: 'assistant', content: '答案', created_at: new Date().toISOString(),
        citations: [{ crop_url: '/api/crops/1' }], degraded: null, verified: true,
      }],
    })),
    create: vi.fn(),
    remove: vi.fn(),
  },
}))

// fetchAuthedImage 故意**慢一拍**返回：模拟"请求在飞、组件被卸载"这个竞态
let releaseImage: (url: string | null) => void = () => {}
vi.mock('@/utils/markdown', () => ({
  fetchAuthedImage: vi.fn(() => new Promise((resolve) => { releaseImage = resolve })),
  renderMarkdown: (s: string) => s,
  resolveAuthedImages: async () => () => {},
}))

import AskPanel from '../AskPanel.vue'

const doc = {
  id: 'd1', filename: 'a.pdf', index_status: 'ready', index_error: null,
} as never

/**
 * 用例 2：**组件卸载后 blob URL 已 revoke。**
 *
 * `plan-v2.md` 记着这个项目出过「卸载后新建的 blob URL 永不回收」：
 * `onBeforeUnmount` 里的 revoke 只跑一次，而在途的取图请求返回后
 * 还会往 map 里塞新的 blob —— 那些再也没有人回收。
 * `ExtractionsView` 已经用 `alive` 标志修过同一个竞态（它的注释里写着），
 * 这条用例把同样的要求钉在 AskPanel 上。
 */
describe('AskPanel 的裁图 blob 生命周期', () => {
  it('卸载时已到手的 blob 会被 revoke', async () => {
    const wrapper = mount(AskPanel, { props: { document: doc } })
    await flushPromises()
    // **必须真的过一遍 URL.createObjectURL。** 早先这里写的是字面量
    // `'blob:mock/live-1'`，而 setup.ts 的账本只在 createObjectURL 被调用时登记 ——
    // 于是 leakedObjectUrls() 恒为 []，这条用例对"revoke 存在与否"完全不敏感
    // （验收实测：把 revokeCrops() 整条删掉，它照样绿）。
    releaseImage(URL.createObjectURL(new Blob()))
    await flushPromises()

    wrapper.unmount()
    await flushPromises()
    expect(leakedObjectUrls()).toEqual([])
  })

  it('卸载**之后**才到手的 blob 也不许留下', async () => {
    const wrapper = mount(AskPanel, { props: { document: doc } })
    await flushPromises()

    // 图还在路上，组件先被卸载了 —— 用户点开一条出处又立刻切走，日常操作
    wrapper.unmount()

    // 现在图到了。它要么当场被回收，要么就永远没人回收了
    releaseImage(URL.createObjectURL(new Blob()))
    await flushPromises()

    expect(leakedObjectUrls()).toEqual([])
  })
})
