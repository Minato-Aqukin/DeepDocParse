import type { CreatedKey, KeyInfo } from '@/types/api'

import { http } from './http'

export interface CreateKeyPayload {
  name: string
  /** unlimited 为真时后端忽略 quota_pages */
  unlimited?: boolean
  quota_pages?: number
  rate_limit_per_min?: number
  expires_in_days?: number | null
}

export const keysApi = {
  list: () => http.get<KeyInfo[]>('/api/keys'),
  create: (payload: CreateKeyPayload) => http.post<CreatedKey>('/api/keys', payload),
  revoke: (id: string) => http.delete(`/api/keys/${id}`),
}
