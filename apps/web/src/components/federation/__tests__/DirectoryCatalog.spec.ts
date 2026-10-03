import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import type { DirectoryMember, DirectoryUnexpandedSubtree } from '@/api/directory'

const api = vi.hoisted(() => ({ createSnapshot: vi.fn(), snapshotPage: vi.fn(), createScope: vi.fn(), collection: vi.fn(), scopeTargets: vi.fn(), list: vi.fn() }))
vi.mock('@/api/directory', () => ({ directoryApi: api }))
vi.mock('@/api/tasks', () => ({ tasksApi: api }))
vi.mock('@/api/resources', () => ({ resourcesApi: api }))
import DirectoryBrowser from '@/components/federation/DirectoryBrowser.vue'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  api.createSnapshot.mockResolvedValue({ data: { snapshot_id: 'members-A', authority_node_id: 'node-a', registry_revision: 3, expires_at: '2026-10-03T15:00:00Z' } })
  const members = [
    { node_id: 'node-offline', health: 'unhealthy', state: 'approved', expansion_state: 'unexpanded_subtree', revision: 4, configured: true, accepting_admissions: false },
    { node_id: 'node-unknown', health: 'unknown', state: 'approved', expansion_state: 'unexpanded_subtree', revision: 1, configured: false, accepting_admissions: false },
    { node_id: 'node-configured', health: 'configured', state: 'approved', expansion_state: 'not_requested', revision: 1, configured: true, accepting_admissions: false },
    { node_id: 'node-draining', health: 'draining', state: 'approved', expansion_state: 'not_requested', revision: 1, configured: true, accepting_admissions: false },
  ] satisfies DirectoryMember[]
  const unexpanded = [{ node_id: 'node-offline', reason: 'timeout' }, { node_id: 'node-denied', reason: 'denied' }] satisfies DirectoryUnexpandedSubtree[]
  api.snapshotPage.mockResolvedValue({ data: { members, complete: true, next_cursor: null } })
  api.createScope.mockResolvedValue({ data: { manifest: { scope_id: 'scope-a', registry_revision_vector: [{ node_id: 'node-a', registry_revision: 3, fetched_at: '2026-10-03T12:00:00Z', directory_ref: 'members' }, { node_id: 'node-b', registry_revision: 7, fetched_at: '2026-10-03T12:01:00Z', directory_ref: 'collections' }], unexpanded_subtrees: unexpanded, enumeration_state: 'partial', valid_until: '2026-10-03T15:00:00Z' }, effective_enumeration_state: 'partial', expired: false, total_targets: 2 } })
  api.scopeTargets.mockResolvedValueOnce({ data: { targets: [{ target_key: { origin_node_id: 'node-a', collection_id: 'local-col', operation: 'corpus.retrieve' }, state: 'planned' }, { target_key: { origin_node_id: 'node-b', collection_id: 'remote-col', operation: 'corpus.retrieve' }, state: 'planned' }], complete: false, next_cursor: 'terminal', total_targets: 2, expired: false } }).mockResolvedValue({ data: { targets: [], complete: true, next_cursor: null, total_targets: 2, expired: false } })
  api.collection.mockResolvedValue({ data: { collection_id: 'local-col', name: '本站手册集合', owner_id: 'owner-a', publication: 'published' } })
  api.list.mockResolvedValue({ data: { items: [{ id: 'public-resource', display_name: '本站公开手册', publication: 'published', owner_id: 'owner-a', uploader_ref: { issuer: 'center-A', subject: 'alice' }, versions: [{ id: 'version-a' }] }, { id: 'temporary', display_name: '临时计算文件', publication: 'private', versions: [{ id: 'temp-v' }] }, { id: 'empty', display_name: '无固定版本的计算任务', publication: 'published', versions: [] }], has_more: false } })
})

async function openDirectory() {
  const wrapper = mount(DirectoryBrowser, { global: { plugins: [ElementPlus] } })
  await wrapper.findAll('button').find(button => button.text() === '封存当前可见目录')!.trigger('click')
  await flushPromises()
  return wrapper
}

it('分开本站与远端公开集合，显示来源节点和已有上传者归属，临时计算不入目录', async () => {
  const wrapper = await openDirectory()
  expect(wrapper.get('[aria-label="本站公开资源"]').text()).toContain('本站公开手册')
  expect(wrapper.get('[aria-label="本站公开资源"]').text()).toContain('center-A / alice')
  expect(wrapper.get('[aria-label="本站公开集合"]').text()).toContain('node-a')
  expect(wrapper.get('[aria-label="本站公开集合"]').text()).toContain('owner-a')
  expect(wrapper.get('[aria-label="远端公开集合"]').text()).toContain('node-b')
  expect(wrapper.get('[aria-label="远端公开集合"]').text()).toContain('remote-col')
  expect(wrapper.get('[aria-label="远端公开集合"]').text()).toContain('远端目录未提供归属引用')
  expect(wrapper.text()).not.toContain('临时计算文件')
  expect(wrapper.text()).not.toContain('无固定版本的计算任务')
})

it('部分枚举显示逐节点水位和未展开原因，读到终止页也不宣称全网查全', async () => {
  const wrapper = await openDirectory()
  const coverage = wrapper.get('[aria-label="目录覆盖与水位"]')
  expect(coverage.text()).toContain('partial')
  expect(coverage.text()).toContain('node-b')
  expect(coverage.text()).toContain('7')
  expect(coverage.text()).toContain('2026-10-03T12:01:00Z')
  expect(coverage.text()).toContain('node-offline')
  expect(coverage.text()).toContain('timeout')
  expect(coverage.text()).toContain('node-denied')
  expect(coverage.text()).toContain('denied')
  await wrapper.findAll('button').find(button => button.text() === '继续读集合下一页')!.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('已读到本范围终止页')
  expect(coverage.text()).toContain('partial')
  expect(coverage.text()).toContain('本范围已观测 2 个公开集合目标')
})

it('重新封存失败不继续显示上一份范围和公开资源', async () => {
  const wrapper = await openDirectory()
  expect(wrapper.get('[aria-label="本站公开资源"]').text()).toContain('本站公开手册')
  api.createSnapshot.mockRejectedValueOnce(new Error('directory unavailable'))
  await wrapper.findAll('button').find(button => button.text() === '封存当前可见目录')!.trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('directory unavailable')
  expect(wrapper.find('[aria-label="目录覆盖与水位"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('本站公开手册')
})

it('不同来源节点的同名集合编号不能复用本站名称或归属', async () => {
  api.scopeTargets.mockReset().mockResolvedValue({ data: { targets: [
    { target_key: { origin_node_id: 'node-a', collection_id: 'local-col', operation: 'corpus.retrieve' }, state: 'planned' },
    { target_key: { origin_node_id: 'node-b', collection_id: 'local-col', operation: 'corpus.retrieve' }, state: 'planned' },
  ], complete: true, next_cursor: null, total_targets: 2, expired: false } })
  const wrapper = await openDirectory()
  expect(wrapper.get('[aria-label="本站公开集合"]').text()).toContain('本站手册集合')
  const remote = wrapper.get('[aria-label="远端公开集合"]').text()
  expect(remote).toContain('node-b / local-col')
  expect(remote).not.toContain('本站手册集合')
  expect(remote).not.toContain('owner-a')
})

it('健康失败计入不可用，未知、已配置与排空分别显示，不混入同一健康计数', async () => {
  const wrapper = await openDirectory()
  const statistics = wrapper.findAll('p').find(paragraph => paragraph.text().includes('已撤销'))!
  expect(statistics.text()).toContain('不可用 1')
  expect(wrapper.get('[aria-label="节点健康统计"]').text()).toContain('能力状态未知 1')
  expect(wrapper.get('[aria-label="节点健康统计"]').text()).toContain('已配置（未验证可用） 1')
  expect(wrapper.get('[aria-label="节点健康统计"]').text()).toContain('正在排空 1')
})
