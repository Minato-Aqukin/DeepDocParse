import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  documentsApi: {
    get: vi.fn(),
    listJobs: vi.fn(),
    reparse: vi.fn(),
    setCurrentJob: vi.fn(),
  },
}))

vi.mock('@/api/resource-context', () => ({
  documentContext: vi.fn(() => ({})),
  selectedResource: vi.fn(() => null),
  selectedVersion: vi.fn(() => null),
}))

import { documentsApi } from '@/api'
import VersionsView from '@/views/VersionsView.vue'
import type { JobInfo } from '@/types/api'

function job(id: string, status: JobInfo['status']): JobInfo {
  return {
    id, document_version: 1, engine: 'mineru', options: {}, status,
    error: null, page_count: 3, is_current: true, created_at: new Date().toISOString(),
    archived_at: null,
  }
}

async function mountView() {
  setActivePinia(createPinia())
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/documents/:id/versions', name: 'versions', component: VersionsView }],
  })
  await router.push('/documents/d1/versions')
  await router.isReady()
  return mount(VersionsView, { global: { plugins: [router, ElementPlus] } })
}

describe('VersionsView 轮询韧性', () => {
  beforeEach(() => {
    vi.mocked(documentsApi.get).mockReset()
    vi.mocked(documentsApi.listJobs).mockReset()
    vi.mocked(documentsApi.get).mockResolvedValue({ data: { id: 'd1', filename: 'a.pdf' } } as never)
  })

  it('单次 transient 失败行内展示并继续轮询：恢复后 jobs 刷新、错误消失', async () => {
    vi.useFakeTimers()
    try {
      const listJobs = vi.mocked(documentsApi.listJobs)
      listJobs.mockRejectedValueOnce(new Error('boom-500'))
      listJobs.mockResolvedValue({ data: [job('j1', 'succeeded')] } as never)

      const wrapper = await mountView()
      await flushPromises()
      // 第一次失败：行内错误出现，但 jobs 为空、轮询链没断
      expect(wrapper.text()).toContain('版本状态刷新失败')
      expect(listJobs).toHaveBeenCalledTimes(1)

      // 推进 2s：重排的那次成功，错误消失、jobs 落定且不再轮询
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(wrapper.text()).not.toContain('版本状态刷新失败')
      expect(wrapper.text()).toContain('v1')
      const callsAfterSettle = listJobs.mock.calls.length
      await vi.advanceTimersByTimeAsync(10000)
      await flushPromises()
      expect(listJobs.mock.calls.length).toBe(callsAfterSettle)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('pending 任务遇到失败也继续轮询而不是停死', async () => {
    vi.useFakeTimers()
    try {
      const listJobs = vi.mocked(documentsApi.listJobs)
      listJobs.mockResolvedValueOnce({ data: [job('j1', 'running')] } as never)
      listJobs.mockRejectedValueOnce(new Error('flaky'))
      listJobs.mockResolvedValue({ data: [job('j1', 'succeeded')] } as never)

      const wrapper = await mountView()
      await flushPromises()
      expect(listJobs).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      // 第二次（失败）：错误行内可见
      expect(wrapper.text()).toContain('版本状态刷新失败')
      const callsAfterFailure = listJobs.mock.calls.length
      // 失败后仍重排：推进后第三次成功
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(listJobs.mock.calls.length).toBeGreaterThan(callsAfterFailure)
      expect(wrapper.text()).not.toContain('版本状态刷新失败')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
