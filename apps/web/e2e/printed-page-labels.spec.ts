import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { fakeLogin, stubApi } from './stub-api'

// This is a real PDF with /PageLabels: i–iv, then 1–2. The bounds below are
// PDFium character bounds of the introduction sentence on physical page four.
const bbox = [60.168, 43.384, 381.096, 52.168]
const pageSize = [612, 792]
const text = 'This section discusses introduction in the labelled document.'

test('printed roman citation opens and highlights physical page four of the fixed version', async ({ page }, info) => {
  await fakeLogin(page)
  await stubApi(page)
  const pdf = readFileSync(new URL('../../../tests/fixtures/page-labels.pdf', import.meta.url))
  const document = {
    id: 'labelled-document', resource_id: 'labelled-resource', source_version_id: 'labelled-v1',
    filename: 'page-labels.pdf', doc_id: 'a'.repeat(64), origin: 'web', mime: 'application/pdf',
    size_bytes: pdf.length, page_count: 6, status: 'succeeded', error: null,
    index_status: 'ready', index_error: null, compile_status: 'ready', compile_degraded: [],
    layout_version: 'ddp-layout/1', code_detection: 'heuristic', current_job_id: 'labelled-parse',
    created_at: '2026-01-01T00:00:00Z', uploaders: ['e2e'], can_delete: true,
  }
  const citation = {
    evidence_id: 'labelled-evidence', source_type: 'source', derived_from: null,
    chunk_id: 'introduction', parse_job_id: 'labelled-parse', seq: 3, page_idx: 3,
    printed_page_label: 'iv', bbox, page_size: pageSize, crop_url: null,
    snippet: text, score: 0.02, similarity: 0.9, resolved: true,
  }
  await page.route(url => url.pathname.startsWith('/api/documents/labelled-document'), route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/result')) return route.fulfill({ json: {
      document_id: document.id, job_id: document.current_job_id, filename: document.filename,
      page_count: 6, markdown: text, images: [],
    } })
    if (url.pathname.endsWith('/pages')) return route.fulfill({ json: {
      document_id: document.id, job_id: document.current_job_id, page_count: 6,
      pages: Array.from({ length: 6 }, (_, page_idx) => ({ page_idx, page_size: pageSize,
        blocks: page_idx === 3 ? [{ page_idx, page_size: pageSize, bbox, seq: 3,
          type: 'text', text, chunk_id: 'introduction', evidence_id: citation.evidence_id }] : [],
      })),
    } })
    if (url.pathname.endsWith('/download-url')) return route.fulfill({ json: { url: '/files/labelled.pdf' } })
    if (url.pathname === '/api/documents/labelled-document') return route.fulfill({ json: document })
    return route.fallback()
  })
  await page.route(url => url.pathname === '/files/labelled.pdf', route => route.fulfill({
    contentType: 'application/pdf', body: pdf,
  }))
  await page.route(url => url.pathname === '/api/conversations' && url.searchParams.has('document'),
    route => route.fulfill({ json: [{ id: 'labelled-conversation', title: 'Introduction' }] }))
  await page.route(url => url.pathname === '/api/conversations/labelled-conversation/messages',
    route => route.fulfill({ json: [{ id: 'answer', role: 'assistant', content: text,
      citations: [citation], verified: false, degraded: null, created_at: '2026-01-01T00:00:00Z',
    }] }))
  await page.route(url => url.pathname === '/api/evidence/labelled-evidence', route => route.fulfill({ json: {
    id: citation.evidence_id, resource_id: document.resource_id, source_version_id: document.source_version_id,
    document: { id: document.id, filename: document.filename }, page_idx: 3, printed_page_label: 'iv',
    seq: 3, parse_job_id: document.current_job_id, doc_version: 1, bbox, page_size: pageSize,
    kind: 'text', content: text, source_type: 'source', derived_from: null, crop_url: null,
    review_state: 'unreviewed', chunk_id: 'introduction', verifications: [],
  } }))
  await page.goto('/#/documents/labelled-document?resource_id=labelled-resource&version_id=labelled-v1&job=labelled-parse')
  await page.getByText('[1] 印刷页 iv · PDF 第 4 页', { exact: true }).click()
  await expect(page.locator('.evidence-preview .layers')).toContainText('印刷页 iv · PDF 第 4 页')
  await expect(page.locator('.el-pager .is-active')).toHaveText('4')
  await expect(page.locator(".pdf-canvas .box.citation")).toBeVisible()
  await expect.poll(() => page.locator('.pdf-canvas canvas').evaluate((canvas, bounds) => {
    if (!(canvas instanceof HTMLCanvasElement) || canvas.width === 300) return 0
    const scale = canvas.width / 612
    const pixels = canvas.getContext('2d')!.getImageData(
      Math.floor(bounds[0]! * scale), Math.floor(bounds[1]! * scale),
      Math.ceil((bounds[2]! - bounds[0]!) * scale), Math.ceil((bounds[3]! - bounds[1]!) * scale),
    ).data
    let dark = 0
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3]! > 0 && pixels[i]! < 100) dark++
    }
    return dark
  }, bbox)).toBeGreaterThan(20)
  await page.screenshot({ path: info.outputPath('printed-label-physical-page.png'), fullPage: true })
  await page.getByRole('link', { name: '打开固定版本原文' }).click()
  await expect(page).toHaveURL(/version_id=labelled-v1/)
  await expect(page).toHaveURL(/job=labelled-parse/)
  await expect(page).toHaveURL(/page=4/)
  await expect(page.locator('.el-pager .is-active')).toHaveText('4')
  await expect(page.locator('.pdf-canvas .box.selected')).toBeVisible()
  await expect(page.locator('.pdf-canvas .el-loading-mask')).toBeHidden()
  await page.screenshot({ path: info.outputPath('fixed-source-link-physical-page.png'), fullPage: true })
})
