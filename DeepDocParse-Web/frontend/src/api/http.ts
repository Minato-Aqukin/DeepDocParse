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

/** 取受 JWT 保护的二进制内容并触发浏览器下载。<a download> 发不出 Authorization 头。 */
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
