import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import LocalTaskDetail from '../LocalTaskDetail.vue'
import { bootSource } from '@/platform/desktop'

afterEach(() => {
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true })
  bootSource.value = null
})

it('上传回执尚未确认完整摘要时不声称中心仍在校验', async () => {
  const value = {
    plan: { plan_id: 'file-1', planning_state: 'approved', consents: {},
      scope: { task_spec: { operation: 'corpus.parse' } } },
    federation: { state: 'content_verifying', remote_compute_id: 'compute-1', delivery: null },
    transfer: { state: 'verifying', uploadedBytes: 802, totalBytes: 802 },
    verification: { state: 'unavailable', expected: null, actual: null },
  }
  Object.defineProperty(window, 'ddpDesktop', { value: {
    clientPlanGet: async () => ({ ok: true, value }),
  }, configurable: true })
  bootSource.value = { sourceId: 'local-1', kind: 'local', state: 'ready', active: true } as never
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/tasks/:planId', component: LocalTaskDetail },
  ] })
  await router.push('/tasks/file-1')
  const wrapper = mount(LocalTaskDetail, { global: { plugins: [router, ElementPlus] } })
  await flushPromises()
  const upload = wrapper.get('[aria-live="polite"]').text()
  expect(upload).toContain('802 / 802 字节')
  expect(upload).toContain('等待确认服务端全量校验结果')
  expect(upload).not.toContain('服务端全量校验中')
  expect(upload).toContain('上传不是解析完成')
})
