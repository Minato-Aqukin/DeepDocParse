import axios from 'axios'
import { ElMessage } from 'element-plus'
import { selectedResource, selectedVersion } from './resource-context'

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

/** 统一的 axios 实例：自动带 JWT，401 直接踢回登录页。 */
export const http = axios.create({ baseURL: '/', timeout: 120_000 })

export const TOKEN_KEY = 'ddp.token'

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  const resource = selectedResource(config.url || '', location.hash, config.params)
  if (resource && !config.params?.resource_id) config.params = { ...config.params, resource_id: resource }
  const version = selectedVersion(config.url || '', location.hash, config.params)
  if (version && !config.params?.version_id) config.params = { ...config.params, version_id: version }
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 后端错误体统一是 OpenAI 风格 {"error": {message, type, code}}
    const detail = error.response?.data?.error
    if (error.config?.suppressErrorToast) {
      // 调用方负责展示；401 仍然要踢回登录页（那是全局行为，不是某个页面的错误态）。
      if (error.response?.status === 401) {
        localStorage.removeItem(TOKEN_KEY)
        if (location.hash !== '#/login') location.hash = '#/login'
      }
      return Promise.reject(error)
    }
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (location.hash !== '#/login') location.hash = '#/login'
    } else if (detail?.message) {
      ElMessage.error(detail.message)
    }
    return Promise.reject(error)
  },
)

/**
 * 取受 JWT 保护的二进制内容并触发浏览器下载。`<a download>` 发不出 Authorization 头。
 *
 * **只用于解析产物**（markdown / json / zip）—— 它们由 corpus-api 生成，
 * 本来就在应用进程里。**原件不要走这条**：原件不进应用进程内存（不变式 6），
 * 那条走 `downloadViaSignedUrl`。
 */
export async function downloadAs(url: string, fallbackName = 'download') {
  const { data, headers } = await http.get(url, { responseType: 'blob' })
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
 */
export async function downloadViaSignedUrl(id: string, fallbackName = 'download') {
  const { data } = await http.get<{ url: string }>(`/api/documents/${id}/download-url`, {
    params: { disposition: 'attachment' },
  })
  const a = document.createElement('a')
  a.href = data.url
  a.download = fallbackName
  a.rel = 'noopener'
  a.click()
}
