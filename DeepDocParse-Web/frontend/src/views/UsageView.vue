<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import BarChart from '@/components/BarChart.vue'

type Daily = { date: string; pages: number; requests: number }

const days = ref(30)
const daily = ref<Daily[]>([])
const byKind = ref<{ kind: string; pages: number; requests: number }[]>([])
const totalPages = ref(0)
const totalRequests = ref(0)
const loading = ref(false)

// 单序列各占一张图：页数与请求数量纲不同，不做双 Y 轴
const pagesSeries = computed(() => daily.value.map((d) => ({ date: d.date, value: d.pages })))
const requestSeries = computed(() => daily.value.map((d) => ({ date: d.date, value: d.requests })))

async function load() {
  loading.value = true
  try {
    const { data } = await api.usage(days.value)
    daily.value = data.daily
    byKind.value = data.by_kind
    totalPages.value = data.total_pages
    totalRequests.value = data.total_requests
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="bar">
    <el-radio-group v-model="days" size="small" @change="load">
      <el-radio-button :value="7">近 7 天</el-radio-button>
      <el-radio-button :value="30">近 30 天</el-radio-button>
      <el-radio-button :value="90">近 90 天</el-radio-button>
    </el-radio-group>
  </div>

  <el-row :gutter="12">
    <el-col :span="12">
      <el-card shadow="never">
        <div class="stat-label">解析页数</div>
        <div class="stat-value">{{ totalPages }}</div>
      </el-card>
    </el-col>
    <el-col :span="12">
      <el-card shadow="never">
        <div class="stat-label">API 请求数</div>
        <div class="stat-value">{{ totalRequests }}</div>
      </el-card>
    </el-col>
  </el-row>

  <el-card shadow="never" class="charts" v-loading="loading">
    <el-empty v-if="!daily.length" description="还没有用量记录" />
    <template v-else>
      <BarChart title="每日解析页数" unit="页" :data="pagesSeries" color="#2a78d6" />
      <BarChart title="每日请求数" unit="次" :data="requestSeries" color="#eb6834" />
    </template>
  </el-card>

  <el-card shadow="never" class="tables">
    <template #header>按平面汇总</template>
    <el-table :data="byKind" size="small">
      <el-table-column prop="kind" label="平面" />
      <el-table-column prop="pages" label="页数" />
      <el-table-column prop="requests" label="请求数" />
    </el-table>
  </el-card>

  <el-collapse class="tables">
    <el-collapse-item title="按天明细（表格视图）" name="daily">
      <el-table :data="daily" size="small">
        <el-table-column prop="date" label="日期" />
        <el-table-column prop="pages" label="页数" />
        <el-table-column prop="requests" label="请求数" />
      </el-table>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.bar {
  margin-bottom: 12px;
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.stat-value {
  font-size: 28px;
  font-weight: 600;
  margin-top: 4px;
}
.charts {
  margin-top: 12px;
  display: grid;
  gap: 20px;
}
.tables {
  margin-top: 12px;
}
</style>
