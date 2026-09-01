<script setup lang="ts">
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
</script>

<template>
  <div class="filters">
    <el-input
      :model-value="modelValue.q"
      placeholder="按文件名筛选，回车进入全文检索"
      clearable
      class="keyword"
      @update:model-value="emit('change', { q: $event })"
      @keyup.enter="emit('search')"
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
