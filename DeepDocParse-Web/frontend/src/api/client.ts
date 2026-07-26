import axios from 'axios'
import { ElMessage } from 'element-plus'

/** 统一的 axios 实例：自动带 JWT，401 直接踢回登录页。 */
export const http = axios.create({ baseURL: '/', timeout: 60_000 })

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

export interface TaskInfo {
  id: string
  filename: string
  doc_id: string
  status: 'pending' | 'running' | 'archiving' | 'succeeded' | 'failed'
  error: string | null
  page_count: number
  size_bytes: number
  mime: string
  engine: string
  created_at: string
  archived_at: string | null
}

export interface KeyInfo {
  id: string
  name: string
  key_prefix: string
  quota_pages: number | null
  used_pages: number
  rate_limit_per_min: number
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
}

export const api = {
  register: (username: string, password: string) =>
    http.post('/api/auth/register', { username, password }),
  login: (username: string, password: string) =>
    http.post('/api/auth/login', { username, password }),

  listTasks: () => http.get<TaskInfo[]>('/api/tasks'),
  getTask: (id: string) => http.get<TaskInfo>(`/api/tasks/${id}`),
  getResult: (id: string) =>
    http.get<{ markdown: string; images: string[]; page_count: number; filename: string }>(
      `/api/tasks/${id}/result`,
    ),
  deleteTask: (id: string) => http.delete(`/api/tasks/${id}`),
  downloadUrl: (id: string, format: 'md' | 'json' | 'source') =>
    `/api/tasks/${id}/download?format=${format}`,

  listKeys: () => http.get<KeyInfo[]>('/api/keys'),
  createKey: (payload: Record<string, unknown>) =>
    http.post<KeyInfo & { key: string }>('/api/keys', payload),
  revokeKey: (id: string) => http.delete(`/api/keys/${id}`),

  usage: (days = 30) =>
    http.get<{
      daily: { date: string; pages: number; requests: number }[]
      by_kind: { kind: string; pages: number; requests: number }[]
      total_pages: number
      total_requests: number
    }>(`/api/usage?days=${days}`),
}
