import axios from 'axios'
import { ElMessage } from 'element-plus'

/** 统一的 axios 实例：自动带 JWT，401 直接踢回登录页。 */
export const http = axios.create({ baseURL: '/', timeout: 120_000 })

export const TOKEN_KEY = 'ddp.token'

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 后端错误体统一是 OpenAI 风格 {"error": {message, type, code}}
    const detail = error.response?.data?.error
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
