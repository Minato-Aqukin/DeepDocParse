import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'

const { get, bundleDownload } = vi.hoisted(() => ({ get: vi.fn(), bundleDownload: vi.fn() }))
vi.mock('@/api/http', () => ({ http: { get }, downloadAs: bundleDownload }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import ResourcesView from '@/views/ResourcesView.vue'
import { bootSource } from '@/platform/desktop'

const digest = `sha256:${'a'.repeat(64)}`
const validUntil = '2027-02-03T04:05:06+08:00'
const resource = {
  id: 'resource-1', organization_id: 'org-1', owner_id: 'u-1',
  uploader_ref: { issuer: 'source-A', subject: 'u-1' }, display_name: '许可技术手册', publication: 'private',
  versions: [{ id: 'version-1', resource_id: 'resource-1', version_no: 1,
    document_id: 'document-1', source_digest: `sha256:${'b'.repeat(64)}`, filename: 'manual.pdf',
    size_bytes: 1200, parse_job_id: null }],
}
let replicas: { replica_id: string; availability: string; source_digest: string; valid_until: string | null }[]

beforeEach(() => {
  setActivePinia(createPinia())
  bootSource.value = null
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
  replicas = [{ replica_id: 'replica-1', availability: 'licensed_copy', source_digest: digest, valid_until: validUntil }]
  get.mockImplementation(async (path: string) => {
    if (path === '/api/resources') return { data: { items: [resource], has_more: false } }
    if (path.endsWith('/replicas')) return { data: { replicas } }
    throw new Error(`Unexpected request: ${path}`)
  })
})

it('shows the source refusal and removes original download actions when Bundle export returns 410', async () => {
  const wrapper = await render()
  bundleDownload.mockRejectedValueOnce({ response: { status: 410 } })
  await wrapper.findAll('button').find(button => button.text() === '导出 Bundle')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('来源已撤销或过期（410），停止新授权，不显示在线。')
  expect(wrapper.findAll('button').some(button => button.text() === '许可来源')).toBe(false)
  expect(wrapper.findAll('button').some(button => button.text() === '导出 Bundle')).toBe(false)
  expect(wrapper.findAll('button').some(button => button.text() === '证据信封')).toBe(true)
})

async function render() {
  const wrapper = mount(ResourcesView, { global: { plugins: [ElementPlus], stubs: { UploadDialog: true } } })
  await flushPromises()
  return wrapper
}

it('identifies a licensed directory copy as an offline snapshot with its own digest and source term', async () => {
  const wrapper = await render()
  await wrapper.findAll('button').find(button => button.text() === '授权副本')!.trigger('click')
  await flushPromises()
  const directory = wrapper.get('[aria-label="授权副本"]')
  expect(directory.text()).toContain('离线快照（不是源节点在线，也不是重新授权）')
  expect(directory.text()).not.toContain('licensed_copy')
  expect(directory.text()).toContain(digest)
  expect(directory.text()).toContain(validUntil)
  expect(wrapper.findAll('button').some(button => button.text() === '许可来源')).toBe(true)
})

it('does not offer licensed-source bytes when the directory marks the copy unavailable', async () => {
  replicas[0]!.availability = 'unavailable'
  const wrapper = await render()
  await wrapper.findAll('button').find(button => button.text() === '授权副本')!.trigger('click')
  await flushPromises()
  expect(wrapper.findAll('button').some(button => button.text() === '许可来源')).toBe(false)
  expect(wrapper.findAll('button').some(button => button.text() === '导出 Bundle')).toBe(false)
  expect(wrapper.get('[aria-label="授权副本"]').text()).toContain('来源已撤销或过期（410），停止新授权，不显示在线。')
  expect(wrapper.findAll('button').some(button => button.text() === '证据信封')).toBe(true)
})

it('keeps an unlimited directory snapshot readable without inventing a source term', async () => {
  replicas[0]!.valid_until = null
  const wrapper = await render()
  await wrapper.findAll('button').find(button => button.text() === '授权副本')!.trigger('click')
  await flushPromises()
  const directory = wrapper.get('[aria-label="授权副本"]')
  expect(directory.text()).toContain('离线快照（不是源节点在线，也不是重新授权）')
  expect(directory.text()).toContain(digest)
  expect(directory.text()).not.toContain('许可有效至')
})

it.each([validUntil, null])('labels downloaded offline source bytes with their response digest and term %s', async term => {
  const wrapper = await render()
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  get.mockResolvedValueOnce({ data: new Blob(['licensed source']), headers: {
    'x-ddp-source-availability': 'offline_snapshot', 'x-ddp-source-digest': digest,
    ...(term ? { 'x-ddp-source-licence-valid-until': term } : {}),
  } })
  await wrapper.findAll('button').find(button => button.text() === '许可来源')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('离线快照（不是源节点在线，也不是重新授权）')
  expect(wrapper.text()).toContain(digest)
  if (term) expect(wrapper.text()).toContain(term)
  else expect(wrapper.text()).not.toContain('许可有效至')
  expect(download).toHaveBeenCalledOnce()
})

it('removes the licensed-source download after 410 while retaining the evidence envelope action', async () => {
  const wrapper = await render()
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  get.mockRejectedValueOnce({ response: { status: 410 } })
  await wrapper.findAll('button').find(button => button.text() === '许可来源')!.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('来源已撤销或过期（410），停止新授权，不显示在线。')
  expect(wrapper.findAll('button').some(button => button.text() === '许可来源')).toBe(false)
  expect(wrapper.findAll('button').some(button => button.text() === '导出 Bundle')).toBe(false)
  expect(download).not.toHaveBeenCalled()
  expect(wrapper.findAll('button').some(button => button.text() === '证据信封')).toBe(true)
  await wrapper.findAll('button').find(button => button.text() === '授权副本')!.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('来源已撤销或过期（410），停止新授权，不显示在线。')
  expect(wrapper.findAll('button').some(button => button.text() === '许可来源')).toBe(false)
  expect(wrapper.findAll('button').some(button => button.text() === '导出 Bundle')).toBe(false)
})

it.each([undefined, 'future_availability'])('preserves unknown availability fallback for source header %s', async availability => {
  const wrapper = await render()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  get.mockResolvedValueOnce({ data: new Blob(['source']), headers: {
    ...(availability ? { 'x-ddp-source-availability': availability } : {}),
  } })
  await wrapper.findAll('button').find(button => button.text() === '许可来源')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('来源可用性未知（老中心，未返回可用性头）')
  expect(wrapper.text()).not.toContain('离线快照')
  expect(wrapper.text()).not.toContain('在线来源')
})
