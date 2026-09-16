<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'

import { tasksApi } from '@/api/tasks'
import StatusTag from '@/components/common/StatusTag.vue'
import CoveragePanel from '@/components/federation/CoveragePanel.vue'
import EventTimeline from '@/components/federation/EventTimeline.vue'
import PlanSummary from '@/components/federation/PlanSummary.vue'
import TaskResultPanel from '@/components/federation/TaskResultPanel.vue'
import { usePolling } from '@/composables/usePolling'
import {
  DELIVERY_STATE,
  EVIDENCE_SUFFICIENCY,
  PLANNING_STATE,
  RETRIEVAL_COMPLETENESS,
  TASK_STATUS,
  metaOf,
  searchModeLabel,
} from '@/constants/federation'
import {
  collectEvents,
  isSettled,
  type CoverageLedger,
  type TaskEvent,
  type TaskPlan,
  type TaskStatus,
} from '@/federation/task-model'

/**
 * 一个联邦任务的权威状态。**各状态轴分开摆**（执行 / 规划 / 检索完成度 / 证据充分性 / 交付）——
 * 合成一个"成功"会把"查了一部分""证据矛盾"这些限定吞掉。
 *
 * 任务还在动时轮询状态并续读事件（`after=next_seq`，断线不丢）；落定后停下来。
 */
const route = useRoute()
const rootTaskId = computed(() => String(route.params.rootTaskId ?? ''))

const status = shallowRef<TaskStatus | null>(null)
const coverage = shallowRef<CoverageLedger | null>(null)
const plan = shallowRef<TaskPlan | null>(null)
const events = ref<TaskEvent[]>([])
const error = ref('')
const planError = ref('')
const coverageError = ref('')
const loading = ref(false)
let nextSeq = 0
let generation = 0

function problem(cause: unknown, fallback: string): string {
  const response = (cause as { response?: { status?: number; data?: { error?: { code?: string; message?: string } } } })?.response
  if (response?.status === 404) return '任务不存在，或你没有权限查看。'
  const detail = response?.data?.error
  if (detail?.code) return `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
  return `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

async function readEvents(current: number) {
  const page = await collectEvents(
    async (after) => (await tasksApi.events(rootTaskId.value, after)).data,
    nextSeq, events.value.map((event) => event.seq),
    { stale: () => current !== generation })
  if (current !== generation) return
  events.value = [...events.value, ...page.events]
  nextSeq = page.next
}

async function refresh() {
  const current = generation
  const { data } = await tasksApi.read(rootTaskId.value)
  if (current !== generation) return
  if (!data || typeof data.status !== 'string' || typeof data.root_task_id !== 'string') {
    throw new Error('中心返回的任务状态格式不兼容')
  }
  status.value = data
  await readEvents(current)
  if (data.coverage_ref) {
    try {
      const ledger = await tasksApi.coverage(rootTaskId.value)
      if (current !== generation) return
      coverage.value = ledger.data
      coverageError.value = ''
    } catch (cause) {
      if (current === generation) {
        coverageError.value = problem(cause, coverage.value ? '覆盖账本刷新失败，下面是上一次取得的' : '覆盖账本读取失败')
      }
    }
  }
  if (!plan.value && (data.planning_state === 'ready' || data.planning_state === 'approved')) {
    try {
      const replay = await tasksApi.plan(rootTaskId.value)
      if (current === generation) { plan.value = replay.data; planError.value = '' }
    } catch (cause) {
      if (current === generation) planError.value = problem(cause, '执行计划读取失败')
    }
  }
}

const polling = usePolling(async () => {
  try {
    await refresh()
    error.value = ''
  } catch (cause) {
    // 单次失败不停轮询（后端重启会自己恢复），但要让用户看见"现在显示的是上一次取得的状态"。
    error.value = problem(cause, '状态刷新失败，显示的是上一次取得的状态')
  }
}, () => !!status.value && !isSettled(status.value), 2000)

async function load() {
  generation++
  polling.stop()
  status.value = null
  coverage.value = null
  plan.value = null
  events.value = []
  nextSeq = 0
  error.value = ''
  planError.value = ''
  coverageError.value = ''
  loading.value = true
  const current = generation
  try {
    await refresh()
    if (current === generation && status.value && !isSettled(status.value)) polling.start()
  } catch (cause) {
    if (current === generation) error.value = problem(cause, '任务读取失败')
  } finally {
    if (current === generation) loading.value = false
  }
}

watch(rootTaskId, load, { immediate: true })
onBeforeUnmount(() => { generation++ })

const settledLabel = computed(() => (status.value && !isSettled(status.value) ? '执行中，每 2 秒刷新一次' : ''))
</script>

<template>
  <section class="task-detail">
    <header>
      <RouterLink class="back" :to="{ name: 'federation-tasks' }">← 联邦任务</RouterLink>
      <h1>任务 <span class="ddp-mono">{{ rootTaskId }}</span></h1>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取任务…</p>

    <template v-if="status">
      <div class="axes" aria-label="状态轴">
        <span class="axis"><span class="axis-label">执行</span><StatusTag :meta="metaOf(TASK_STATUS, status.status)" /></span>
        <span class="axis"><span class="axis-label">规划</span><StatusTag :meta="metaOf(PLANNING_STATE, status.planning_state)" /></span>
        <span class="axis"><span class="axis-label">检索完成度</span><StatusTag :meta="metaOf(RETRIEVAL_COMPLETENESS, status.retrieval_completeness)" /></span>
        <span class="axis"><span class="axis-label">证据充分性</span><StatusTag :meta="metaOf(EVIDENCE_SUFFICIENCY, status.evidence_sufficiency)" /></span>
        <span class="axis"><span class="axis-label">交付</span><StatusTag :meta="metaOf(DELIVERY_STATE, status.delivery_state)" /></span>
      </div>
      <p class="meta">
        {{ searchModeLabel(status.search_mode) }} · 计划修订 <span class="ddp-num">{{ status.plan_revision }}</span>
        · 更新于 <span class="ddp-mono">{{ status.updated_at }}</span>
        <span v-if="settledLabel" class="muted">· {{ settledLabel }}</span>
      </p>
      <p v-if="status.error" class="ddp-degraded is-danger" role="status">执行出错：<span class="ddp-mono">{{ status.error }}</span></p>

      <section class="block">
        <h2>结果</h2>
        <TaskResultPanel v-if="status.result" :result="status.result"
          :coordinator-node-id="plan?.root_coordinator_node_id ?? null" />
        <p v-else class="muted">
          {{ status.status === 'cancelled' ? '任务已取消，没有结果。'
            : status.status === 'failed' ? '执行失败，没有产出结果。' : '还没有结果；执行结束后显示在这里。' }}
        </p>
      </section>

      <section class="block">
        <h2>覆盖账本</h2>
        <p v-if="coverageError" class="ddp-degraded is-danger">{{ coverageError }}</p>
        <CoveragePanel v-if="coverage" :ledger="coverage" />
        <p v-else-if="!coverageError" class="muted">执行开始后才有覆盖记录。</p>
      </section>

      <section class="block">
        <h2>执行计划</h2>
        <p v-if="planError" class="ddp-degraded is-danger">{{ planError }}</p>
        <PlanSummary v-if="plan" :plan="plan" />
        <p v-else-if="!planError" class="muted">计划还没生成。</p>
      </section>

      <section class="block">
        <h2>事件</h2>
        <EventTimeline v-if="events.length" :events="events" />
        <p v-else class="muted">暂无事件。</p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.task-detail { max-width: 1120px; margin: auto; display: grid; gap: 16px; }
header { display: grid; gap: 6px; }
.back { color: var(--ddp-ink-2); text-decoration: none; font-size: 13px; width: fit-content; min-height: 24px; }
.back:hover { text-decoration: underline; }
h1 { font-size: 27px; font-weight: 600; margin: 0; overflow-wrap: anywhere; }
h1 .ddp-mono { font-size: 16px; font-weight: 500; }
h2 { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
.axes { display: flex; flex-wrap: wrap; gap: 20px; }
.axis { display: inline-flex; align-items: center; gap: 8px; }
.axis-label { color: var(--ddp-ink-3); font-size: 12.5px; }
.meta { margin: 0; color: var(--ddp-ink-2); font-size: 13.5px; }
.muted { color: var(--ddp-ink-3); margin: 0; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.block { padding-top: 20px; border-top: var(--ddp-bw) solid var(--ddp-line); }
</style>
