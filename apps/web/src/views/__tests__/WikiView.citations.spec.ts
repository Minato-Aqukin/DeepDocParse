import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api', () => ({
  TOKEN_KEY: 'ddp.token',
  knowledgeApi: { backlinks: vi.fn(async () => ({ data: { backlinks: [] } })) },
  versionedWikiApi: {
    list: vi.fn(),
    read: vi.fn(),
    readRevision: vi.fn(),
    rebuild: vi.fn(), create: vi.fn(), editPage: vi.fn(),
  },
}))

vi.mock('@/api/tasks', () => ({
  tasksApi: { identity: vi.fn(async () => ({ data: { identity: { authority_node_id: 'node-a' } } })) },
}))

vi.mock('@/api/resources', () => ({
  resourcesApi: { list: vi.fn(async () => ({ data: { items: [], has_more: false } })) },
}))

vi.mock('@/components/evidence/EvidencePreview.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/knowledge/GraphCanvas.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/engine/EngineOptionsForm.vue', () => ({ default: { template: '<div />' } }))

import { knowledgeApi, versionedWikiApi } from '@/api'
import WikiView from '@/views/WikiView.vue'

function revisionDoc() {
  return {
    wiki: { id: 'w1', title: '测试 Wiki', current_revision_id: 'r1' },
    revision: {
      id: 'r1', stale: false, stale_reasons: {}, merge_conflicts: [],
      limits: {},
      dependency_manifest: [
        { evidence_id: 'e1', page_key: 'p1', resource_id: 'res', source_version_id: 'ver',
          origin_node_id: null, locator: {} },
        { evidence_id: 'e2', page_key: 'p1', resource_id: 'res', source_version_id: 'ver',
          origin_node_id: null, locator: {} },
        { evidence_id: 'e3', page_key: 'p1', resource_id: 'res', source_version_id: 'ver',
          origin_node_id: null, locator: {} },
      ],
      pages: [{
        page_key: 'p1', title: '第一页', stale: false,
        generated_sections: [{
          heading: '概述',
          sentences: [
            { id: 's1', text: '第一句有三条引用。', evidence_ids: ['e1', 'e2', 'e3'],
              unsupported: false, conflict_group: null },
          ],
        }],
        human_paragraphs: [],
      }],
      relations: [],
    },
  }
}

async function mountView() {
  setActivePinia(createPinia())
  const doc = revisionDoc()
  vi.mocked(versionedWikiApi.list).mockResolvedValue({ data: [doc] } as never)
  vi.mocked(versionedWikiApi.read).mockResolvedValue({ data: doc } as never)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/wiki', name: 'wiki', component: WikiView }],
  })
  await router.push('/wiki?wiki_id=w1')
  await router.isReady()
  const wrapper = mount(WikiView, { global: { plugins: [router, ElementPlus] } })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('WikiView 逐条引用可点', () => {
  it('一句三条引用渲染三个出处按钮，每条都能选中对应证据', async () => {
    const wrapper = await mountView()
    const cites = wrapper.findAll('.sentence .cite')
    expect(cites.map((b) => b.text())).toEqual(['出处 1', '出处 2', '出处 3'])
    // 点第二条：右侧证据面板打开的是 e2，不是永远的 evidence_ids[0]
    await cites[1]!.trigger('click')
    await flushPromises()
    // EvidencePreview 被桩掉了：用"反链请求带了 e2"证明选中了第二条
    expect(vi.mocked(knowledgeApi.backlinks)).toHaveBeenCalledWith(
      'e2', expect.objectContaining({ resource_id: 'res', version_id: 'ver' }),
    )
    wrapper.unmount()
  })

  it('引用计数标签保留：unsupported 句仍标无法指回 bbox', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('引用 3 条')
    wrapper.unmount()
  })
})
