import { expect, test, type Page } from '@playwright/test'

import { fakeLogin } from './stub-api'

const entityA = {
  id: 'entity-a', canonical_name: 'DeepDocParse', normalized_name: 'deepdocparse',
  entity_type: 'system', aliases: ['DDP'], merged_by: 'alias', merge_confidence: 0.45,
  entity_merge_uncertain: true, split_from_id: null, review_state: 'unreviewed', provider: {},
}
const entityB = { ...entityA, id: 'entity-b', canonical_name: 'Qwen3-VL',
  normalized_name: 'qwen3vl', aliases: [], entity_merge_uncertain: false, merge_confidence: 1 }
const citation = {
  evidence_id: 'evidence-1', source_type: 'source', derived_from: null, chunk_id: 'chunk-1',
  parse_job_id: 'job-1', seq: 0, page_idx: 2, bbox: [10, 20, 110, 80], page_size: [612, 792],
  crop_url: null, snippet: 'DeepDocParse uses Qwen3-VL.', score: 0.02, similarity: 0.91, resolved: true,
}
const edge = {
  id: 'edge-1', subject_id: 'entity-a', predicate: 'uses', object_id: 'entity-b',
  confidence: 0.9, evidence_ids: ['evidence-1'], unsupported: false,
  review_state: 'unreviewed', provider: {}, citations: [citation],
}

async function stubKnowledge(page: Page) {
  // 版本化 Wiki（`/api/wikis`，见 `packages/contracts/openapi/wiki-v1.yaml`）：
  // 逐句证据、unsupported、反链四类照旧断言，stub 形状跟着版本化契约走。
  const document = {
    wiki: { id: 'wiki-1', title: 'DeepDocParse',
      current_revision_id: 'revision-1', published_revision_id: null },
    revision: {
      id: 'revision-1', wiki_id: 'wiki-1', base_revision_id: null, kind: 'generated',
      title: 'DeepDocParse', created_by: 'u-1', created_at: '2026-09-15T08:00:00Z',
      provider: {}, limits: {}, merge_conflicts: [], stale: false, stale_reasons: {},
      pages: [{ page_key: 'arch', title: '架构',
        generated_sections: [{ heading: '架构', sentences: [
          { id: 'sentence-1', text: '系统使用 Qwen3-VL。', evidence_ids: ['evidence-1'],
            unsupported: false, conflict_group: null },
          { id: 'sentence-2', text: '尚无来源的推断。', evidence_ids: [],
            unsupported: true, conflict_group: null },
        ] }],
        human_paragraphs: [], stale: false }],
      dependency_manifest: [{
        page_key: 'arch', resource_id: 'resource-1', source_version_id: 'version-1',
        document_id: 'doc-1', source_digest: 'a'.repeat(64), parse_revision: 'job-1',
        evidence_id: 'evidence-1', excerpt_digest: `sha256:${'b'.repeat(64)}`,
        locator: { kind: 'text', page_idx: 2, seq: 0, bbox: [10, 20, 110, 80] },
      }],
    },
  }
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/wikis') return route.fulfill({ json: [document] })
    if (path === '/api/wikis/wiki-1') return route.fulfill({ json: document })
    if (path === '/api/v1/capabilities') return route.fulfill({ json: {
      protocol_version: 'ddp-client/1',
      identity: { environment_id: 'node-center-a', authority_node_id: 'node-center-a', workspace_id: 'org-1' },
      profile: { issuer: 'node-center-a', subject: 'u-1' },
    } })
    if (path === '/api/knowledge/graph') return route.fulfill({ json: {
      graph_version: 'ddp-graph/1', entities: [entityA, entityB], edges: [edge],
    } })
    if (path === '/api/reviews') return route.fulfill({ json: { items: [] } })
    if (path === '/api/evidence/evidence-1/backlinks') return route.fulfill({ json: {
      evidence_id: 'evidence-1', backlinks: [
        { source_kind: 'assertion', source_id: 'a-1', role: 'primary', label: '回答断言' },
        { source_kind: 'extract_field', source_id: 'x-1:field', role: 'primary', label: 'field' },
        { source_kind: 'graph_edge', source_id: 'edge-1', role: 'primary', label: 'uses' },
        { source_kind: 'wiki_sentence', source_id: 'sentence-1', role: 'primary', label: '系统使用 Qwen3-VL。' },
      ],
    } })
    if (path === '/api/evidence/evidence-1') return route.fulfill({ json: {
      id: 'evidence-1', document: { id: 'doc-1', filename: 'manual.pdf' }, page_idx: 2,
      seq: 0, parse_job_id: 'job-1', doc_version: 1, bbox: [10, 20, 110, 80],
      page_size: [612, 792], kind: 'text', content: 'DeepDocParse uses Qwen3-VL.',
      source_type: 'source', derived_from: null, crop_url: null, review_state: 'unreviewed',
      chunk_id: 'chunk-1', verifications: [],
    } })
    return route.fulfill({ json: [] })
  })
}

test.beforeEach(async ({ page }) => { await fakeLogin(page); await stubKnowledge(page) })

test('Wiki 逐句点回证据、unsupported 可见、反链四类齐全', async ({ page }) => {
  await page.goto('/#/wiki')
  await expect(page.getByText('尚无来源的推断。')).toBeVisible()
  await expect(page.getByText('unsupported · 无法指回 bbox')).toBeVisible()
  await page.getByRole('button', { name: '出处 1' }).click()
  await expect(page.getByText('证据预览')).toBeVisible()
  await expect(page.locator('.evidence-preview').getByText('第 3 页')).toBeVisible()
  await expect(page.locator('.backlinks article')).toHaveCount(4)
})

test('图谱 canvas 渲染并可用键盘边选择打开 bbox 证据', async ({ page }) => {
  await page.goto('/#/graph')
  const canvas = page.getByLabel('实体关系图谱；可拖拽节点、滚轮缩放、点击边查看证据')
  await expect(canvas).toBeVisible()
  await page.getByLabel('键盘选边').selectOption('edge-1')
  await expect(page.getByText('证据预览')).toBeVisible()
  await expect(page.getByText('bbox 10, 20, 110, 80')).toBeVisible()
})

test('千节点图谱在 canvas 首屏门限内完成且不创建千个 DOM 节点', async ({ page }) => {
  const entities = Array.from({ length: 1000 }, (_, index) => ({
    ...entityB, id: `n-${index}`, canonical_name: `Node ${index}`,
    normalized_name: `node${index}`,
  }))
  const edges = Array.from({ length: 999 }, (_, index) => ({
    ...edge, id: `l-${index}`, subject_id: `n-${index}`, object_id: `n-${index + 1}`,
  }))
  await page.route((url) => url.pathname === '/api/knowledge/graph',
    (route) => route.fulfill({ json: { graph_version: 'ddp-graph/1', entities, edges } }))
  // 固定图谱负载回归：千节点首屏不画标签，外部字体下载不属于必要输入。
  // 仅隔离本例的字体 CSS；生产环境的字体网络首屏延迟仍需单独验证。
  await page.route((url) => url.hostname === 'fonts.googleapis.com',
    (route) => route.fulfill({ status: 200, contentType: 'text/css', body: '' }))
  const started = Date.now()
  await page.goto('/#/graph')
  await expect(page.getByLabel('实体关系图谱；可拖拽节点、滚轮缩放、点击边查看证据')).toBeVisible()
  await expect(page.getByText('1000 节点 · 999 条边')).toBeVisible()
  expect(Date.now() - started).toBeLessThan(8000)
  expect(await page.locator('canvas').count()).toBe(1)
  expect(await page.locator('.graph-host svg').count()).toBe(0)
})
