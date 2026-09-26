<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'

import StatusTag from '@/components/common/StatusTag.vue'
import {
  DELIVERY_STATE,
  LOCAL_DISPATCH_STATE,
  PLANNING_STATE,
  metaOf,
} from '@/constants/federation'
import { getActiveSource, unwrap, workspaceError, type DesktopBridge, type Json } from '@/platform/desktop'

type Row = Record<string, Json>

const rowOf = (value: unknown): Row =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Row) : {}
const rowsOf = (value: unknown): Row[] => (Array.isArray(value) ? value.map(rowOf) : [])
const textOf = (value: unknown): string => (typeof value === 'string' ? value : '')

/**
 * 桌面本机源的联邦任务列表（`/tasks` 在 local 源下的形态）。
 *
 * 读的是本机账本（`clientPlanList`），不是中心的 `GET /api/v1/tasks`。
 * `connectionId` = 当前 local 源的 `sourceId`（HostProxy 绑定：两者逐字相同）。
 * 状态按契约轴摆：规划态走 `planning_state`，派发进展走本机 `local_dispatch_state`，
 * 交付走 `delivery_state` —— 不压成一个"完成"。
 */
const bridge = window.ddpDesktop as DesktopBridge | undefined
const router = useRouter()

const sourceId = computed(() => getActiveSource()?.sourceId ?? '')
const listing = shallowRef<Row>({})
const loading = ref(true)
const error = ref('')
let alive = true

const items = computed(() => rowsOf(listing.value.items))

async function load() {
  if (!bridge || !sourceId.value) {
    loading.value = false
    return
  }
  loading.value = true
  error.value = ''
  try {
    listing.value = rowOf(unwrap(await bridge.clientPlanList({ connectionId: sourceId.value })))
  } catch (cause) {
    if (alive) error.value = workspaceError(cause)
  } finally {
    if (alive) loading.value = false
  }
}

function open(planId: string) {
  if (planId) void router.push({ name: 'federation-task-local', params: { planId } })
}

function propose() {
  void router.push({ name: 'federation-task-new' })
}

onMounted(load)
onBeforeUnmount(() => {
  alive = false
})
</script>

<template>
  <section class="local-tasks" aria-label="本机联邦任务">
    <header>
      <div>
        <h1>联邦任务</h1>
        <p>从本机发起的跨节点检索、回答与 Wiki 构建。计划、批准与对账记录保存在本机。</p>
      </div>
      <div class="header-actions">
        <el-button :disabled="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="propose">发起联邦任务</el-button>
      </div>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取本机任务…</p>
    <p v-else-if="!error && !items.length" class="muted">尚无联邦任务。</p>
    <div v-if="items.length" class="scroll">
      <table>
        <thead>
          <tr><th>任务</th><th>规划</th><th>派发进展</th><th>交付</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="textOf(item.plan_id)">
            <td class="query">
              <el-button link @click="open(textOf(item.plan_id))">{{ textOf(item.plan_id) }}</el-button>
            </td>
            <td><StatusTag :meta="metaOf(PLANNING_STATE, textOf(item.planning_state))" /></td>
            <td>
              <StatusTag
                v-if="item.federation"
                :meta="metaOf(LOCAL_DISPATCH_STATE, textOf(rowOf(item.federation).state))" />
              <span v-else class="muted">尚未派发</span>
            </td>
            <td>
              <StatusTag
                v-if="textOf(rowOf(item.federation).delivery_state)"
                :meta="metaOf(DELIVERY_STATE, textOf(rowOf(item.federation).delivery_state))" />
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="muted">下列状态来自本机镜像；中心的最新状态以任务详情里的「对账」结果为准。</p>
  </section>
</template>

<style scoped>
.local-tasks { max-width: 1120px; margin: auto; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
.header-actions { display: flex; gap: 12px; flex-shrink: 0; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
header p { color: var(--ddp-ink-2); margin: 8px 0 0; }
.muted { color: var(--ddp-ink-3); }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); }
.scroll { overflow-x: auto; }
table { width: 100%; min-width: 640px; border-collapse: collapse; }
th, td { padding: 10px 12px 10px 0; text-align: left; border-bottom: var(--ddp-bw) solid var(--ddp-line); font-size: 13.5px; vertical-align: top; }
th { color: var(--ddp-ink-3); font-weight: 500; }
.query { max-width: 360px; }
@media (max-width: 640px) { header { flex-direction: column; } }
</style>
