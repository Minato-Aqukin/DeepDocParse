import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { apiUrl, getActiveSourceId, goDesktopSources, isDesktop } from '@/platform/desktop'
import { selectedResource, selectedVersion } from './resource-context'
import type { ResourceContext } from './resource-context'

declare module 'axios' {
  interface AxiosRequestConfig {
    /**
     * 这一次请求的失败由调用方自己在页面上说清楚，不要再弹全局 toast。
     *
     * 两条路径同时报同一个错有两个坏处：① 页面上出现两个 role="alert"；
     * ② toast 显示的是后端原文，而页面文案可能是**刻意模糊**的（例如 404 与无权限
     * 合并成一句，不给出"这个任务存在不存在"的探测口）—— 两者不一致就等于把
     * 模糊化绕过去了。
     */
    suppressErrorToast?: boolean
  }
}

/**
 * 统一的 axios 实例。
 *
 * 浏览器：自动带 JWT，401 直接踢回登录页（原有行为，不变）。
 * 桌面：不带任何令牌（令牌只存在宿主主进程，页面拿不到），请求指回宿主
 * `ddp://app`；每个代理响应带 `X-DDP-Source`，与启动时的当前源不一致就丢弃
 * （切换瞬间在途的请求不能串源显示）；桌面 401 去数据源页（带上过期原因），
 * 绝不去 Web 登录页。
 */
// 桌面走 fetch 适配器：axios 的 XHR 适配器只认 http/https/file/blob/url/data，
// 请求 `ddp://app/...` 会在 send 之前直接拒绝（2026-09-25 真窗口冒烟里页面一个 /api 请求都发不出去）。
export const http = axios.create({ baseURL: '/', timeout: 120_000,
  ...(isDesktop() ? { adapter: 'fetch' as const } : {}) })

export const TOKEN_KEY = 'ddp.token'

export function expireRejectedSession(authorization: unknown): void {
  const auth = useAuthStore()
  if (isDesktop()) {
    // 桌面没有 Web 会话：任何 401 都是当前源的登录过期。
    auth.markSourceSignedOut()
    goDesktopSources('source_signed_out')
    return
  }
  // An old request must not invalidate a newer login.
  if (!auth.token || authorization !== `Bearer ${auth.token}`) return
  auth.logout()
  if (location.hash !== '#/login') location.hash = '#/login'
}

/**
 * 响应来源校验（桌面）：`X-DDP-Source` 与启动时的当前源不一致 → 丢弃。
 * 返回 true = 来源正确（或非桌面/缺头：缺头只在旧宿主或单测里出现，按正确处理）。
 */
export function isCurrentSourceResponse(headers: unknown): boolean {
  if (!isDesktop()) return true
  const boot = getActiveSourceId()
  if (!boot) return true
  if (!headers || typeof headers !== 'object') return true
  const record = headers as Record<string, unknown>
  const seen = record['x-ddp-source'] ?? record['X-DDP-Source']
  if (seen == null) return true
  return String(seen) === boot
}

http.interceptors.request.use((config) => {
  // 资源上下文按页面相对路径判定，必须在改写成 `ddp://app/...` 之前算。
  const path = config.url || ''
  const resource = selectedResource(path, location.hash, config.params)
  if (resource && !config.params?.resource_id) config.params = { ...config.params, resource_id: resource }
  const version = selectedVersion(path, location.hash, config.params)
  if (version && !config.params?.version_id) config.params = { ...config.params, version_id: version }
  if (config.url && !/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(config.url)) {
    config.url = apiUrl(config.url)
  }
  // 桌面下页面没有任何令牌可带：宿主按当前源附进程令牌或中心 JWT。
  if (!isDesktop()) {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) config.headers.Authorization = `Bearer ${token}`
  } else {
    delete config.headers.Authorization
  }
  return config
})

http.interceptors.response.use(
  (resp) => {
    if (!isCurrentSourceResponse(resp.headers)) {
      throw axios.AxiosError.from(
        { code: 'source_changed', message: '数据源已切换，此结果已丢弃' },
        'source_changed',
        resp.config,
        undefined,
        { ...resp, status: 409, statusText: 'Source Changed' },
      )
    }
    return resp
  },
  (error: unknown) => {
    if (!axios.isAxiosError(error)) throw error
    if (!isCurrentSourceResponse(error.response?.headers)) {
      const changed = axios.AxiosError.from(
        { code: 'source_changed', message: '数据源已切换，此结果已丢弃' },
        'source_changed',
        error.config,
        error.request,
        error.response,
      )
      throw changed
    }
    if (error.response?.status === 401) {
      expireRejectedSession(error.config?.headers.get('Authorization'))
    } else if (!error.config?.suppressErrorToast) {
      const data = error.response?.data
      const detail = data && typeof data === 'object' && 'error' in data ? data.error : null
      if (detail && typeof detail === 'object' && 'message' in detail
        && typeof detail.message === 'string') ElMessage.error(detail.message)
    }
    throw error
  },
)

/**
 * 取受 JWT 保护的二进制内容并触发浏览器下载。`<a download>` 发不出 Authorization 头。
 *
 * **只用于解析产物**（markdown / json / zip）—— 它们由 corpus-api 生成，
 * 本来就在应用进程里。**原件不要走这条**：原件不进应用进程内存（不变式 6），
 * 那条走 `downloadViaSignedUrl`。
 */
export async function downloadAs(url: string, fallbackName = 'download', context?: ResourceContext) {
  const { data, headers } = await http.get(url, { responseType: 'blob', params: context })
  const objectUrl = URL.createObjectURL(data as Blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download =
    /filename="([^"]+)"/.exec(String(headers['content-disposition'] || ''))?.[1] || fallbackName
  a.click()
  URL.revokeObjectURL(objectUrl)
}

/**
 * 原件下载：先要一条短期直读地址，再让浏览器直接去对象存储取。
 *
 * **不能用 `downloadAs` + 让 XHR 跟 302** —— 跨源跳转时 Authorization 头的
 * 处理各家实现不一致，而带着它打到对象存储的结果是一个看起来像"签名错误"
 * 的 400。直传上传那条路踩过同一个坑（见 `uploads.ts` 的注释）。
 *
 * 走 `<a>` 导航还有一个好处：字节完全不经过 JS，大文件不吃浏览器内存。
 *
 * 桌面：宿主把中心返回的预签名地址改写成 `ddp://app/_object/...`，
 * `data.url` 已经是可直接打开的地址，原样用（见 `apiUrl` 注释）。
 */
export async function downloadViaSignedUrl(id: string, fallbackName = 'download', context?: ResourceContext) {
  const { data } = await http.get<{ url: string }>(`/api/documents/${id}/download-url`, {
    params: { ...context, disposition: 'attachment' },
  })
  const a = document.createElement('a')
  a.href = apiUrl(data.url)
  a.download = fallbackName
  a.rel = 'noopener'
  a.click()
}
