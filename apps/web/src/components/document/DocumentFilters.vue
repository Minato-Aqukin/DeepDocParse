<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

import { INDEX_STATUS_OPTIONS, PARSE_STATUS_OPTIONS } from '@/constants/status'
import type { DocumentFilters } from '@/stores/documents'

/**
 * 筛选器容器。
 *
 * 加筛选维度（标签、文件夹、时间范围…）= 这里加一个控件 + store 的 filters 加一个字段，
 * 页面与表格都不用改。
 */
defineProps<{ modelValue: DocumentFilters; loading?: boolean }>()
const emit = defineEmits<{
  (e: 'change', patch: Partial<DocumentFilters>): void
  (e: 'search'): void
}>()

/**
 * 关键词输入 250ms 防抖：每个按键都直接 fetchList 的话，打 "abc" 发 3 个请求，
 * 叠加 store 那边修之前的竞态，最慢（最宽泛）的响应赢。回车/清空立即触发，
 * 不让用户等那 250ms。
 */
let debounceTimer: number | undefined
const pendingQuery = ref<string | null>(null)

function flushQuery() {
  if (pendingQuery.value === null) return
  const q = pendingQuery.value
  pendingQuery.value = null
  emit('change', { q })
}

function onKeywordInput(value: string) {
  pendingQuery.value = value
  window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(flushQuery, 250)
}

function onKeywordEnter() {
  // 回车不等防抖：先把未发出的关键词同步出去，再进全文检索。
  window.clearTimeout(debounceTimer)
  flushQuery()
  emit('search')
}

function onKeywordClear() {
  // 清空键（clearable 的 ×）：立即生效，不等防抖。
  // 注意 el-input 点 × 时 update:model-value 也会以 '' 触发一次（顺序不定），
  // 那一次同样进了防抖但值相同（q: ''），只是多刷一次列表，不会错。
  window.clearTimeout(debounceTimer)
  pendingQuery.value = null
  emit('change', { q: '' })
}

onBeforeUnmount(() => window.clearTimeout(debounceTimer))
</script>

<template>
  <div class="filters">
    <el-input
      :model-value="modelValue.q"
      placeholder="按文件名筛选，回车进入全文检索"
      clearable
      class="keyword"
      @update:model-value="onKeywordInput($event ?? '')"
      @clear="onKeywordClear"
      @keyup.enter="onKeywordEnter"
    >
      <template #prefix><el-icon><component is="Search" /></el-icon></template>
    </el-input>

    <el-select
      :model-value="modelValue.status"
      placeholder="解析状态"
      clearable
      class="picker"
      @update:model-value="emit('change', { status: $event ?? '' })"
    >
      <el-option v-for="o in PARSE_STATUS_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
    </el-select>

    <el-select
      :model-value="modelValue.indexStatus"
      placeholder="问答状态"
      clearable
      class="picker"
      @update:model-value="emit('change', { indexStatus: $event ?? '' })"
    >
      <el-option v-for="o in INDEX_STATUS_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
    </el-select>

    <slot name="extra" />
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.keyword {
  max-width: 360px;
}
.picker {
  width: 150px;
}
</style>
