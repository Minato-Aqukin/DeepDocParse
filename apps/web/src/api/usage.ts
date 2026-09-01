import type { UsageSummary } from '@/types/api'

import { http } from './http'

export const usageApi = {
  summary: (days = 30) => http.get<UsageSummary>('/api/usage', { params: { days } }),
}
