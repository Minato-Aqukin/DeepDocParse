import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'

import { documentsApi } from '@/api'
import { INDEX_STATUS, PARSE_STATUS } from '@/constants/status'
import type { DocumentInfo, DocumentStats, IndexStatus, ParseStatus } from '@/types/api'

/**
 * 文档库的列表状态。
 *
 * filters 刻意做成一个对象而不是散落的 ref：将来加标签/文件夹等筛选维度时，
 * 只需往这个对象加字段 + 在 DocumentFilters 里加一个控件，调用方一行都不用改。
 */
export interface DocumentFilters {
  q: string
  status: ParseStatus | ''
  indexStatus: IndexStatus | ''
}

const PAGE_SIZE = 20

export const useDocumentsStore = defineStore('documents', () => {
  const items = ref<DocumentInfo[]>([])
  const stats = ref<DocumentStats>({ documents: 0, pages: 0, askable: 0 })
  const loading = ref(false)
  const page = ref(1)
  const pageSize = ref(PAGE_SIZE)
  /** 后端按 limit/offset 返回，没有总数：多取一条判断还有没有下一页 */
  const hasMore = ref(false)

  const filters = reactive<DocumentFilters>({ q: '', status: '', indexStatus: '' })

  /** 还有任务在动就继续轮询；全落定就停（active 标记来自状态文案表） */
  const hasActive = computed(() =>
    items.value.some(
      (d) => PARSE_STATUS[d.status]?.active || INDEX_STATUS[d.index_status]?.active,
    ),
  )

  async function fetchList() {
    loading.value = true
    try {
      const { data } = await documentsApi.list({
        q: filters.q || undefined,
        status: filters.status || undefined,
        limit: pageSize.value + 1,
        offset: (page.value - 1) * pageSize.value,
      })
      hasMore.value = data.length > pageSize.value
      const rows = data.slice(0, pageSize.value)
      // 索引状态后端不支持过滤，在前端补上（数据量小，且能立刻可用）
      items.value = filters.indexStatus
        ? rows.filter((d) => d.index_status === filters.indexStatus)
        : rows
    } finally {
      loading.value = false
    }
  }

  async function fetchStats() {
    const { data } = await documentsApi.stats()
    stats.value = data
  }

  async function refresh() {
    await Promise.all([fetchList(), fetchStats()])
  }

  /** 改筛选条件要回到第一页，否则会停在一个空页上 */
  async function applyFilters(patch: Partial<DocumentFilters>) {
    Object.assign(filters, patch)
    page.value = 1
    await fetchList()
  }

  async function goPage(next: number) {
    page.value = Math.max(1, next)
    await fetchList()
  }

  function reset() {
    items.value = []
    page.value = 1
    Object.assign(filters, { q: '', status: '', indexStatus: '' })
  }

  return {
    items, stats, loading, page, pageSize, hasMore, filters, hasActive,
    fetchList, fetchStats, refresh, applyFilters, goPage, reset,
  }
})
