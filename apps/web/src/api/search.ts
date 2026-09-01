import type { SearchResult } from '@/types/api'

import { http } from './http'

export const searchApi = {
  /** doc 为空 = 跨全部文档检索 */
  query: (q: string, doc?: string) =>
    http.get<SearchResult>('/api/search', { params: doc ? { q, doc } : { q } }),
}
