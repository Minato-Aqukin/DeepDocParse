import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { fakeLogin, stubApi } from './stub-api'
import { realErrors, watchErrors } from './console-guard'
import type { DocumentInfo } from '../src/types/api'

const resource = {
  id: 'resource-alice', organization_id: 'org-1', owner_id: 'u-1',
  uploader_ref: { issuer: 'center-A', subject: 'u-1' }, display_name: '控制器技术手册', publication: 'private',
  versions: [{ id: 'fixed-v2', resource_id: 'resource-alice', version_no: 2,
    document_id: 'demo-id', source_digest: 'd'.repeat(64), filename: '控制器手册.pdf',
    size_bytes: 1200, parse_job_id: 'fixed-parse-2' }],
}

test('资源列表区分本站范围、归属与固定版本，打开时保留完整证据上下文',async({page},info)=>{
  await fakeLogin(page);await stubApi(page)
  const errors=watchErrors(page)
  await page.route(url=>url.pathname==='/api/resources',route=>{
    const scope=new URL(route.request().url()).searchParams.get('scope') ?? 'mine'
    return route.fulfill({json:{items:[{...resource,publication:scope==='mine'?'private':'published'}],has_more:false}})
  })
  await page.goto('/#/resources')
  await expect(page.getByRole('heading',{name:'控制器技术手册'})).toBeVisible()
  await expect(page.locator('.identity')).toContainText('center-A / u-1')
  await expect(page.getByText('● 私有')).toBeVisible()
  await expect(page.getByRole('button',{name:'公开到本站'})).toBeVisible()
  await page.getByText('本站公开',{exact:true}).click()
  await expect(page.getByRole('radio',{name:'本站公开'})).toBeChecked()
  await expect(page.getByText('● 已公开')).toBeVisible()
  await expect(page.getByRole('button',{name:'删除资源'})).toHaveCount(0)
  await page.screenshot({path:info.outputPath('resources-public.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('button',{name:'控制器手册.pdf',exact:true}).click()
  await expect(page).toHaveURL(/resource_id=resource-alice/)
  await expect(page).toHaveURL(/version_id=fixed-v2/)
  await expect(page).toHaveURL(/job=fixed-parse-2/)
  expect(realErrors(errors)).toEqual([])
})

test('资源接口不兼容时显示可见失败，页面和范围切换仍可用',async({page})=>{
  await fakeLogin(page);await stubApi(page)
  const errors=watchErrors(page)
  await page.route(url=>url.pathname==='/api/resources',route=>route.fulfill({json:[]}))
  await page.goto('/#/resources')
  await expect(page.getByRole('alert')).toContainText('格式不兼容')
  await expect(page.getByRole('radio',{name:'本站公开'})).toBeEnabled()
  expect(realErrors(errors)).toEqual([])
})

test('同名同大小但内容不同的文件不会被上传队列合并', async ({ page }) => {
  await fakeLogin(page)
  await stubApi(page)
  await page.goto('/#/resources')
  await page.getByRole('button', { name: '上传资料', exact: true }).click()
  const input = page.locator('#upload-input')
  await input.setInputFiles({
    name: 'controller-manual.pdf', mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.7 reset delay 17'),
  })
  await input.setInputFiles({
    name: 'controller-manual.pdf', mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.7 reset delay 23'),
  })
  await expect(page.getByRole('button', { name: '上传 2', exact: true })).toBeEnabled()
  await page.locator('.files .file').first().getByRole('button', { name: '移除' }).click()
  await expect(page.getByRole('button', { name: '上传 1', exact: true })).toBeEnabled()
})

test('文档库打开共享内容的固定版本，不串到别的资源或较新的解析', async ({ page }) => {
  await fakeLogin(page)
  await stubApi(page)
  const pdf = readFileSync(new URL('../../../tests/fixtures/sample.pdf', import.meta.url))
  const document: DocumentInfo = {
    id: 'shared-doc', resource_id: 'resource-alice', source_version_id: 'fixed-v2',
    filename: 'shared-answer.pdf', doc_id: 'd'.repeat(64), origin: 'web',
    mime: 'application/pdf', size_bytes: pdf.length, page_count: 1,
    status: 'succeeded', error: null, index_status: 'ready', index_error: null,
    compile_status: 'ready', compile_degraded: [], compile_fingerprint: 'f'.repeat(64),
    layout_version: 'ddp-layout/1', code_detection: 'heuristic',
    current_job_id: 'fixed-parse-2', created_at: '2026-01-01T00:00:00Z',
    uploaders: ['e2e'], can_delete: true,
  }
  await page.route(url => url.pathname === '/api/documents',
    route => route.fulfill({ json: [document] }))
  await page.route(url => url.pathname.startsWith('/api/documents/shared-doc'), route => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('resource_id') !== document.resource_id) {
      return route.fulfill({ status: 409, json: {
        error: { code: 'resource_context_required', message: 'Choose the resource for this shared document' },
      } })
    }
    // The list is a v2 snapshot; the resource advanced to v3 before the click.
    const fixed = url.searchParams.get('version_id') === 'fixed-v2'
    const job = fixed ? 'fixed-parse-2' : 'fixed-parse-3'
    if (url.pathname === '/api/documents/shared-doc') {
      return route.fulfill({ json: { ...document,
        source_version_id: fixed ? 'fixed-v2' : 'fixed-v3', current_job_id: job } })
    }
    const requestedJob = url.searchParams.get('job') || job
    const text = requestedJob === 'fixed-parse-2' ? 'Answer: 42' : 'Answer: 43'
    if (url.pathname.endsWith('/result')) {
      return route.fulfill({ json: { document_id: document.id, job_id: requestedJob,
        filename: document.filename, page_count: 1, markdown: text, images: [] } })
    }
    if (url.pathname.endsWith('/pages')) {
      return route.fulfill({ json: { document_id: document.id, job_id: requestedJob, page_count: 1,
        pages: [{ page_idx: 0, page_size: null, blocks: [
          { page_idx: 0, page_size: null, bbox: null, seq: 0, type: 'text',
            text, chunk_id: `chunk-${requestedJob}`, evidence_id: `evidence-${requestedJob}` },
        ] }] } })
    }
    if (url.pathname.endsWith('/download-url')) {
      return route.fulfill({ json: { url: '/files/fixed-source.pdf' } })
    }
    return route.fallback()
  })
  await page.route(url => url.pathname === '/files/fixed-source.pdf',
    route => route.fulfill({ contentType: 'application/pdf', body: pdf }))

  await page.goto('/#/documents')
  await page.getByRole('button', { name: '打开', exact: true }).click()
  await expect(page.locator('.pane.result')).toContainText('Answer: 42')
  await expect(page.locator('.pane.result')).not.toContainText('Answer: 43')
  await expect(page.locator('.pane.source canvas')).toBeVisible()
  await expect(page.locator('.pane.ask textarea')).toBeEnabled()
})

test('检索命中打开实际第二页并高亮原文区域，过期命中不冒充定位成功', async ({ page }) => {
  await fakeLogin(page)
  await stubApi(page)
  const pdf = readFileSync(new URL('../../../tests/fixtures/contract.pdf', import.meta.url))
  const document: DocumentInfo = {
    id: 'goods-document', resource_id: 'goods-resource', source_version_id: 'goods-v1',
    filename: 'contract.pdf', doc_id: 'e'.repeat(64), origin: 'web',
    mime: 'application/pdf', size_bytes: pdf.length, page_count: 2,
    status: 'succeeded', error: null, index_status: 'ready', index_error: null,
    compile_status: 'ready', compile_degraded: [], compile_fingerprint: 'f'.repeat(64),
    layout_version: 'ddp-layout/1', code_detection: 'heuristic',
    current_job_id: 'goods-parse', created_at: '2026-01-01T00:00:00Z',
    uploaders: ['e2e'], can_delete: true,
  }
  const text = 'Industrial bearing 6204'
  // Native character bounds of this phrase on contract.pdf's physical second page.
  const bbox = [61.092, 121.384, 181.668, 132.628]
  const pageSize = [612, 792]
  let searchAllowance = 1
  await page.route(url => url.pathname === '/api/search', route => {
    if (searchAllowance-- <= 0) return route.fulfill({ status: 429, json: {
      error: { code: 'rate_limit_exceeded', message: 'Search allowance exhausted' },
    } })
    return route.fulfill({ json: {
    query: 'bearing', degraded: null, groups: [{
      document_id: document.id, resource_id: document.resource_id,
      source_version_id: document.source_version_id, parse_revision: document.current_job_id,
      filename: document.filename, hits: [{
        chunk_id: 'goods-2', page_idx: 1, bbox, snippet: text, score: 1, similarity: 0.9,
      }],
    }],
    } })
  })
  await page.route(url => url.pathname.startsWith('/api/documents/goods-document'), route => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('resource_id') !== document.resource_id
      || url.searchParams.get('version_id') !== document.source_version_id) {
      return route.fulfill({ status: 409, json: {
        error: { code: 'resource_context_required', message: 'Choose the fixed source' },
      } })
    }
    if (url.pathname === '/api/documents/goods-document') return route.fulfill({ json: document })
    if (url.pathname.endsWith('/result')) return route.fulfill({ json: {
      document_id: document.id, job_id: document.current_job_id, filename: document.filename,
      page_count: 2, markdown: text, images: [],
    } })
    if (url.pathname.endsWith('/pages')) return route.fulfill({ json: {
      document_id: document.id, job_id: document.current_job_id, page_count: 2,
      pages: [
        { page_idx: 0, page_size: pageSize, blocks: [] },
        { page_idx: 1, page_size: pageSize, blocks: [{
          page_idx: 1, page_size: pageSize, bbox, seq: 1, type: 'text', text,
          chunk_id: 'goods-2', evidence_id: 'goods-evidence',
        }] },
      ],
    } })
    if (url.pathname.endsWith('/download-url')) return route.fulfill({ json: {
      url: '/files/goods-source.pdf',
    } })
    return route.fallback()
  })
  await page.route(url => url.pathname === '/files/goods-source.pdf',
    route => route.fulfill({ contentType: 'application/pdf', body: pdf }))

  await page.goto('/#/search')
  await page.getByPlaceholder('在可访问的资源中检索').fill('bearing')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('link', { name: /Industrial bearing 6204/ }).click()
  await expect(page.locator('.pane.source canvas')).toBeVisible()
  await expect(page.locator('.el-pager .is-active')).toHaveText('2')
  await expect(page.locator('.pdf-canvas .box.selected')).toBeVisible()

  await page.goto(page.url().replace('chunk=goods-2', 'chunk=missing'))
  await expect(page.locator('.workbench').getByRole('alert').filter({
    hasText: '无法定位所选的原文区域',
  })).toBeVisible()
  // 失效的是命中定位，不是 PDF：重开后原件仍须渲染，而非出现 worker 生命周期错误。
  await expect(page.locator('.pane.source canvas')).toBeVisible()
  await expect.poll(() => page.locator('.pane.source canvas').evaluate(
    canvas => {
      if (!(canvas instanceof HTMLCanvasElement)) throw new Error('原件未渲染到 canvas')
      return canvas.width * canvas.height
    },
  )).not.toBe(300 * 150)
  await expect(page.locator('.pdf-canvas .el-loading-mask')).toBeHidden()
  await expect(page.locator('.pane.source').getByRole('alert')).toHaveCount(0)
  await expect(page.locator('.el-pager .is-active')).toHaveText('2')
  await expect(page.locator('.pdf-canvas .box.selected')).toHaveCount(0)
})
