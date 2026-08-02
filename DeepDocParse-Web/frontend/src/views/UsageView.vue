<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { usageApi } from '@/api'
import BarChart from '@/components/common/BarChart.vue'
import StatCard from '@/components/common/StatCard.vue'
import type { UsageSummary } from '@/types/api'

/**
 * 用量页做成**卡片网格**：每块内容各自独立成卡。
 * 以后要加套餐、账单、余额告警，往网格里塞一张新卡片即可，不用动页面骨架。
 */
const days = ref(30)
const summary = ref<UsageSummary>({ daily: [], by_kind: [], total_pages: 0, total_requests: 0 })
const loading = ref(false)

// 页数与请求数量纲不同，各占一张图（小倍数），不做双 Y 轴
const pagesSeries = computed(() => summary.value.daily.map((d) => ({ date: d.date, value: d.pages })))
const requestSeries = computed(() =>
  summary.value.daily.map((d) => ({ date: d.date, value: d.requests })),
)

const KIND_LABEL: Record<string, string> = {
  parse: '文档解析',
  chat: '对话/VQA',
  embeddings: '向量化（对外）',
  embed: '向量化（索引）',
  mcp: 'MCP 调用',
  qa: '文档问答',
}

async function load() {
  loading.value = true
  try {
    summary.value = (await usageApi.summary(days.value)).data
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="bar">
      <el-radio-group v-model="days" size="small" @change="load">
        <el-radio-button :value="7">近 7 天</el-radio-button>
        <el-radio-button :value="30">近 30 天</el-radio-button>
        <el-radio-button :value="90">近 90 天</el-radio-button>
      </el-radio-group>
    </div>

    <div class="grid" v-loading="loading">
      <StatCard class="cell" label="解析页数" :value="summary.total_pages" icon="Document" />
      <StatCard class="cell" label="API 请求数" :value="summary.total_requests" icon="Connection" />

      <el-card shadow="never" class="cell span-2">
        <template #header>每日趋势</template>
        <el-empty v-if="!summary.daily.length" description="还没有用量记录" />
        <template v-else>
          <BarChart title="每日解析页数" unit="页" :data="pagesSeries" color="#2a78d6" />
          <BarChart title="每日请求数" unit="次" :data="requestSeries" color="#eb6834" />
        </template>
      </el-card>

      <el-card shadow="never" class="cell span-2">
        <template #header>按平面汇总</template>
        <el-table :data="summary.by_kind" size="small">
          <el-table-column label="平面">
            <template #default="{ row }">{{ KIND_LABEL[row.kind] ?? row.kind }}</template>
          </el-table-column>
          <el-table-column prop="pages" label="页数" width="120" />
          <el-table-column prop="requests" label="请求数" width="120" />
        </el-table>
      </el-card>

      <el-collapse class="cell span-2">
        <el-collapse-item title="按天明细（表格视图）" name="daily">
          <el-table :data="summary.daily" size="small">
            <el-table-column prop="date" label="日期" />
            <el-table-column prop="pages" label="页数" width="120" />
            <el-table-column prop="requests" label="请求数" width="120" />
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </div>
  </div>
</template>

<style scoped>
.bar {
  margin-bottom: 12px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.span-2 {
  grid-column: span 2;
}
.cell :deep(.el-card__body) {
  display: grid;
  gap: 18px;
}
</style>
