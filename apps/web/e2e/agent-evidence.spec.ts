import { expect, test, type Page } from '@playwright/test'

import { fakeLogin, stubApi } from './stub-api'

const citation = {
  evidence_id: 'evidence-1', source_type: 'source', chunk_id: 'chunk-1',
  parse_job_id: 'job-1', seq: 7, page_idx: 0, bbox: [10, 20, 110, 220],
  page_size: [612, 792],
  crop_url: null, snippet: '额定电压为 24V', score: 0.02, similarity: 0.91, resolved: true,
}

async function stubAgent(page: Page) {
  await fakeLogin(page)
  await stubApi(page)
  await page.route(
    (url) => url.pathname === '/api/conversations' && url.searchParams.has('document'),
    (route) => route.fulfill({ json: [{ id: 'conversation-1', title: '额定电压' }] }),
  )
  await page.route(
    (url) => url.pathname === '/api/conversations/conversation-1/messages',
    (route) => route.fulfill({ json: [{
      id: 'answer-1', role: 'assistant', content: '额定电压为 24V。另一个猜测。',
      citations: [citation], verified: true, degraded: null,
      created_at: new Date().toISOString(),
      assertions: [
        {
          id: 'assertion-1', position: 0, text: '额定电压为 24V。',
          evidence_ids: ['evidence-1'], unsupported: false,
          verification: { state: 'passed', mode: 'auto' }, citations: [citation],
        },
        {
          id: 'assertion-2', position: 1, text: '另一个猜测。', evidence_ids: [],
          unsupported: true, verification: { state: 'unverified', mode: null }, citations: [],
        },
      ],
      query_decision: {
        need_retrieval: true, reason: 'fresh_question',
        inherited_evidence_ids: [], degraded: null,
      },
      retrieval: { candidates: [
        {
          evidence_id: 'evidence-1', document_id: 'demo-id', chunk_id: 'chunk-1',
          rank: 0, score: 0.02, similarity: 0.91, accepted: true,
          reason: 'document_gate_passed',
        },
        {
          evidence_id: 'evidence-2', document_id: 'other-doc', chunk_id: 'chunk-2',
          rank: 1, score: 0.01, similarity: 0.20, accepted: false,
          reason: 'document_below_similarity',
        },
      ] },
    }] }),
  )
  await page.route(
    (url) => url.pathname === '/api/evidence/evidence-1',
    (route) => route.fulfill({ json: {
      id: 'evidence-1', document: { id: 'demo-id', filename: 'demo.pdf' },
      page_idx: 0, seq: 7, parse_job_id: 'job-1', doc_version: 2,
      bbox: [10, 20, 110, 220], page_size: [612, 792], kind: 'table',
      content: '额定电压为 24V', source_type: 'source', derived_from: null,
      crop_url: '/api/documents/demo-id/crops/job-1/0_bbox.png', review_state: 'unreviewed',
      chunk_id: 'chunk-1', verifications: [],
    } }),
  )
  await page.route(
    (url) => url.pathname === '/api/documents/demo-id/crops/job-1/0_bbox.png',
    (route) => route.fulfill({
      contentType: 'image/png',
      body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAABLAAAAAUCAIAAAASgVNzAAAAjUlEQVR4nO3XMQEAIAzAMMC/5yFjRxMFfXtn5gAAANDztgMAAADYYQgBAACiDCEAAECUIQQAAIgyhAAAAFGGEAAAIMoQAgAARBlCAACAKEMIAAAQZQgBAACiDCEAAECUIQQAAIgyhAAAAFGGEAAAIMoQAgAARBlCAACAKEMIAAAQZQgBAACiDCEAAMBp+vB/AyXYZBkFAAAAAElFTkSuQmCC',
        'base64'),
    }),
  )
}

test('断言、拒绝候选与四层 1:1 证据预览都可复核', async ({ page }) => {
  await stubAgent(page)
  await page.goto('/#/documents/demo-id')

  await expect(page.locator('.ask-panel')).toContainText('额定电压为 24V。')
  await expect(page.locator('.ask-panel')).toContainText('无证据支持')
  await expect(page.locator('.ask-panel')).toContainText('本轮执行检索')
  await expect(page.locator('.candidate-trace')).toContainText('1 条通过 · 1 条拒绝')
  await page.locator('.candidate-trace summary').click()
  await expect(page.locator('.candidate-trace li').filter({
    hasText: 'document_below_similarity',
  })).toBeVisible()

  await page.getByText('[1] 第 1 页').click()
  const preview = page.locator('.evidence-preview')
  await expect(preview).toContainText('文档demo.pdf')
  await expect(preview).toContainText('页第 1 页')
  await expect(preview).toContainText('块#7')
  await expect(preview).toContainText('原子table · 原文')
  await expect(preview).toContainText('1:1 原始像素')
  const crop = preview.locator('.crop-scroll img')
  await expect(crop).toBeVisible()
  expect(await crop.evaluate((image: HTMLImageElement) => ({
    natural: image.naturalWidth,
    shown: image.getBoundingClientRect().width,
    scrollWidth: image.parentElement!.scrollWidth,
    clientWidth: image.parentElement!.clientWidth,
  }))).toEqual(expect.objectContaining({ natural: 1200, shown: 1200 }))
  expect(await crop.evaluate((image: HTMLImageElement) =>
    image.parentElement!.scrollWidth > image.parentElement!.clientWidth)).toBe(true)
  await expect(preview).toContainText('只做标注，不修改内容')
  await expect(preview.getByRole('button', { name: '通过', exact: true })).toBeVisible()
  await expect(preview.getByRole('button', { name: '标疑', exact: true })).toBeVisible()
  await expect(preview.getByRole('button', { name: '驳回', exact: true })).toBeVisible()
})
