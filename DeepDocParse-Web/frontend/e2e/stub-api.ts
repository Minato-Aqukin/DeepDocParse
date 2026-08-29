import type { Page } from '@playwright/test'

/**
 * 把后端整个挡在网络层之外。
 *
 * **为什么必须打桩**：路由冒烟这一组要验的是"这一页渲染得出来、没报错"。
 * 不打桩的话，后端没起时每一页都会因为 ECONNREFUSED 变红 ——
 * 那时这组用例测的其实是"后端在不在"，而不是前端。CI 上更是必然红。
 *
 * 打桩返回的是**形状合法的空数据**，不是随便一个 `{}`：
 * 形状不对会让组件在渲染期炸，那种红看起来像"页面坏了"，一样会误导。
 */
export async function stubApi(page: Page): Promise<void> {
  // **必须用谓词而不是 `'**/api/**'` 这个 glob。** dev server 是 vite，
  // 它按源码路径提供模块 —— 本项目的 API 客户端就住在 `/src/api/*.ts`，
  // 正好被那个 glob 命中。拦下来返回 JSON 的话模块图直接断，
  // 表现是**整个应用不挂载、`#app` 空着**，看起来像"每一页都白屏"，
  // 而真正的原因只是打桩打得太宽。这一脚已经踩过一次了。
  const isApiCall = (url: URL) => url.pathname.startsWith('/api/')

  await page.route(
    (url) => isApiCall(url),
    async (route) => {
      const url = new URL(route.request().url())
      const path = url.pathname

      // 顺序有讲究：更具体的路径要排在前面
      if (path.endsWith('/documents/stats/summary')) {
        return route.fulfill({ json: { documents: 0, pages: 0, askable: 0 } })
      }
      // 形状必须与 types/api.ts 的 UsageSummary 一致。给错形状的话组件会在
      // 渲染期抛 `Cannot read properties of undefined (reading 'length')` ——
      // 那种红看起来像"用量页坏了"，实际只是打桩形状对不上，一样会误导
      if (path.startsWith('/api/usage')) {
        return route.fulfill({
          json: { daily: [], by_kind: [], total_pages: 0, total_requests: 0 },
        })
      }
      if (path === '/api/knowledge/graph') {
        return route.fulfill({ json: { graph_version: 'ddp-graph/1', entities: [], edges: [] } })
      }
      if (path === '/api/knowledge/entities') {
        return route.fulfill({ json: { graph_version: 'ddp-graph/1', entities: [] } })
      }
      if (path === '/api/reviews') return route.fulfill({ json: { items: [] } })
      // **形状必须是 SearchResult，不能落到下面那个 `[]` 兜底。** 落下去的话
      // `data.groups` 是 undefined，模板里 `!groups.length` 当场抛，
      // 而**只访问 /search 不搜索是发现不了的**（q 为空时 run() 直接 return）——
      // 一条静默降低覆盖的打桩，正是阶段 8 门禁要盯的那类。
      if (path === '/api/search') {
        return route.fulfill({ json: { query: url.searchParams.get('q') || '', degraded: null, groups: [] } })
      }
      if (path === '/api/wiki') return route.fulfill({ json: [] })
      if (/\/documents\/[^/]+\/result$/.test(path)) {
        return route.fulfill({
          json: {
            document_id: 'demo-id', job_id: 'job-1', filename: 'demo.pdf',
            page_count: 1, markdown: '# 示例\n\n正文', images: [],
          },
        })
      }
      if (/\/documents\/[^/]+\/pages$/.test(path)) {
        return route.fulfill({
          json: { document_id: 'demo-id', job_id: 'job-1', page_count: 1, pages: [] },
        })
      }
      // SourceUrl 要带 path/mime：WorkbenchView 读的是 `source.data.path`，
      // 缺了它 sourcePath 恒为空字符串，**pdfjs 原件预览那一整块在 e2e 里从不渲染**
      if (/\/documents\/[^/]+\/source[-_]?url$/.test(path) || path.endsWith('/source')) {
        return route.fulfill({
          json: {
            url: '/files/demo-token', path: '/files/demo-token',
            mime: 'application/pdf',
          },
        })
      }
      // 形状照 types/api.ts 的 Profile 来。给错不会崩，但设置页会渲染出
      // `new Date(undefined)` = "Invalid Date"，用例照样绿 ——
      // **静默地把覆盖率降下去**，比直接红更难发现
      if (path.endsWith('/auth/me')) {
        return route.fulfill({
          json: {
            user_id: 'u-1', username: 'e2e', email: 'e2e@example.com',
            created_at: new Date().toISOString(),
          },
        })
      }
      if (/\/documents\/[^/]+\/versions$/.test(path)) return route.fulfill({ json: [] })
      if (/\/documents\/[^/]+$/.test(path)) {
        return route.fulfill({
          json: {
            id: 'demo-id', filename: 'demo.pdf', doc_id: 'd'.repeat(64), origin: 'upload',
            mime: 'application/pdf', size_bytes: 1, page_count: 1, status: 'succeeded',
            error: null, index_status: 'ready', index_error: null,
            compile_status: 'ready', compile_degraded: [],
            compile_fingerprint: 'f'.repeat(64), layout_version: 'ddp-layout/1',
            code_detection: 'heuristic',
            current_job_id: 'job-1', created_at: new Date().toISOString(),
            // 形状要跟 types/api.ts 的 DocumentInfo 一致 —— 少字段不会红，
            // 但组件会走进"没人传过/不能删"的分支，静默把覆盖降下去
            uploaders: ['e2e'], can_delete: true,
          },
        })
      }
      // 其余一律给一个空列表；有对象形状的接口必须在上面显式列出。
      return route.fulfill({ json: [] })
    },
  )

  // 稳定文件 URL（原件预览）。同样用谓词，避免误伤源码路径
  await page.route(
    (url) => url.pathname.startsWith('/files/'),
    (route) => route.fulfill({ status: 204, body: '' }),
  )
}

/** 塞一个假 token，让路由守卫放行。这一组只验渲染，不验鉴权。 */
export async function fakeLogin(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('ddp.token', 'e2e-fake-token')
    localStorage.setItem('ddp.username', 'e2e')
  })
}
