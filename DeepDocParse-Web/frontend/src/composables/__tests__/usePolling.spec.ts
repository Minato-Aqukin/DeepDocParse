import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

import { usePolling } from '../usePolling'

/**
 * 用例 1：**组件卸载后定时器不再触发。**
 *
 * 这条不是凑数 —— `plan-v2.md` 记着这个项目真出过「轮询活过组件卸载」：
 * 定时器已经触发进了 async 回调时，卸载清掉的是那个已经跑完的 timer，
 * 回调结束后又排一个新的，于是永久轮询。类型检查抓不到这一类。
 */
function harness(fetcher: () => Promise<void> | void, shouldContinue: () => boolean) {
  let api: ReturnType<typeof usePolling>
  const Comp = defineComponent({
    setup() {
      api = usePolling(fetcher, shouldContinue, 1000)
      api.start()
      return () => h('div')
    },
  })
  const wrapper = mount(Comp)
  return { wrapper, api: api! }
}

describe('usePolling', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('卸载之后不再调用 fetcher', async () => {
    const fetcher = vi.fn()
    const { wrapper } = harness(fetcher, () => true)

    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)

    wrapper.unmount()

    // 卸载后再走十个周期。**一次都不许再调** ——
    // 这正是变异测试要打的地方：把 onUnmounted(stop) 删掉，这里必须变红
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('shouldContinue 变 false 之后自己停下来（定时器也要清掉）', async () => {
    const fetcher = vi.fn()
    let active = true
    const { api } = harness(fetcher, () => active)

    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)

    active = false
    await vi.advanceTimersByTimeAsync(5000)
    // 停了就是停了，不该继续攒调用
    expect(fetcher).toHaveBeenCalledTimes(1)
    // **光断言"不再调 fetcher"不够**：把 `return stop()` 写成 `return`
    // 会让定时器永久空转（只是不再调 fetcher），而那条断言照样绿。
    // 定时器没清掉 = 电池与后端照样被耗着，正是这个 composable 要防的事
    expect(api.running.value, '轮询没有真的停下来').toBe(false)
    expect(vi.getTimerCount(), '定时器还在空转').toBe(0)
  })

  it('单次 fetcher 抛异常不终止轮询', async () => {
    // 后端重启/网络毛刺过去之后应当自己恢复 —— 一次失败就永久停摆
    // 会让用户看着一个永远"处理中"的文档，而且没有任何报错
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error('网络毛刺'))
      .mockResolvedValue(undefined)
    harness(fetcher, () => true)

    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('重复 start 不会叠加出两个定时器', async () => {
    // 叠加的话轮询频率翻倍，而且 stop 只清得掉最后一个
    const fetcher = vi.fn()
    const { api } = harness(fetcher, () => true)
    api.start()
    api.start()

    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
