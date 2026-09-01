import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 包住 forceSimulation 才能观察到 stop() —— jsdom 没有 canvas 上下文，
// draw() 一进门就 return，"没抛错"那种断言在这里是恒真的，不能拿来当守卫。
const simulations: Array<{ stop: ReturnType<typeof vi.fn> }> = []
vi.mock('d3-force', async (importOriginal) => {
  const actual = await importOriginal<typeof import('d3-force')>()
  return {
    ...actual,
    forceSimulation: (...args: unknown[]) => {
      // @ts-expect-error 转发给真实实现
      const simulation = actual.forceSimulation(...args)
      simulation.stop = vi.fn(simulation.stop.bind(simulation))
      simulations.push(simulation as unknown as { stop: ReturnType<typeof vi.fn> })
      return simulation
    },
  }
})

import type { KnowledgeEdge, KnowledgeEntity } from '@/types/api'

import GraphCanvas from '../GraphCanvas.vue'

const observers: Array<{ disconnect: ReturnType<typeof vi.fn> }> = []
class FakeResizeObserver {
  disconnect = vi.fn()
  observe = vi.fn()
  unobserve = vi.fn()
  constructor() { observers.push(this) }
}

function entity(id: string): KnowledgeEntity {
  return {
    id, canonical_name: id, normalized_name: id, entity_type: 'system', aliases: [],
    merged_by: 'none', merge_confidence: 1, entity_merge_uncertain: false,
    split_from_id: null, review_state: 'unreviewed', provider: {},
  }
}

const edges: KnowledgeEdge[] = [{
  id: 'edge-1', subject_id: 'a', predicate: 'uses', object_id: 'b', confidence: 0.9,
  evidence_ids: ['e1'], unsupported: false, review_state: 'unreviewed', provider: {},
  citations: [],
}]

beforeEach(() => {
  observers.length = 0
  simulations.length = 0
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
})

describe('GraphCanvas 卸载后不留活的东西', () => {
  it('卸载时断开 ResizeObserver 并停掉力导仿真', async () => {
    const wrapper = mount(GraphCanvas, {
      props: { entities: [entity('a'), entity('b')], edges },
      attachTo: document.body,
    })
    await flushPromises()
    expect(observers).toHaveLength(1)
    expect(simulations.length).toBeGreaterThan(0)
    const running = simulations.at(-1)!
    running.stop.mockClear()

    // 仿真还在跑的时候拔掉组件 —— 这正是本项目已知 bug 的形状
    // （轮询活过组件卸载 / blob URL 永不回收，都是漏了卸载钩子）
    wrapper.unmount()
    expect(observers[0]!.disconnect).toHaveBeenCalled()
    expect(running.stop).toHaveBeenCalled()
  })

  it('千节点只画进一张 canvas，不为每个节点建 DOM', async () => {
    const entities = Array.from({ length: 1000 }, (_, index) => entity(`n-${index}`))
    const wrapper = mount(GraphCanvas, {
      props: { entities, edges: [] }, attachTo: document.body,
    })
    await flushPromises()
    expect(wrapper.findAll('canvas')).toHaveLength(1)
    expect(wrapper.findAll('svg')).toHaveLength(0)
    wrapper.unmount()
  })
})
