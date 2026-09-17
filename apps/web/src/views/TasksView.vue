<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { tasksApi } from '@/api/tasks'
import StatusTag from '@/components/common/StatusTag.vue'
import TaskComposer from '@/components/federation/TaskComposer.vue'
import {
  EVIDENCE_SUFFICIENCY,
  RETRIEVAL_COMPLETENESS,
  SCOPE_KIND_LABEL,
  TASK_STATUS,
  metaOf,
  searchModeLabel,
} from '@/constants/federation'
import type { TaskListItem } from '@/federation/task-model'

/**
 * 本人发起的联邦任务（`GET /api/v1/tasks`）。列表只有状态轴；结果、证据与覆盖账本进详情页读。
 * 翻页用服务端给的不透明游标：记下走过的游标栈，"上一页"回到栈里的上一个，不自己算偏移。
 */
const router = useRouter()
const items = ref<TaskListItem[]>([])
const loading = ref(false)
const error = ref('')
const composing = ref(false)
const cursors = ref<(string | undefined)[]>([undefined])
const next = ref<string | null>(null)
let generation = 0

async function load() {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const { data } = await tasksApi.list({ limit: 20, cursor: cursors.value[cursors.value.length - 1] })
    if (current !== generation) return
    if (!Array.isArray(data?.items) || !(data.next_cursor === null || typeof data.next_cursor === 'string')) {
      throw new Error('中心返回的任务列表格式不兼容')
    }
    items.value = data.items
    next.value = data.next_cursor
  } catch (cause) {
    if (current === generation) error.value = `任务列表加载失败：${cause instanceof Error ? cause.message : String(cause)}`
  } finally {
    if (current === generation) loading.value = false
  }
}

function forward() {
  if (!next.value) return
  cursors.value = [...cursors.value, next.value]
  void load()
}

function back() {
  if (cursors.value.length <= 1) return
  cursors.value = cursors.value.slice(0, -1)
  void load()
}

/** 建完直接进详情页：规划状态、计划与批准入口都在那边，列表上看不到。 */
function created(rootTaskId: string) {
  composing.value = false
  void router.push({ name: 'federation-task', params: { rootTaskId } })
}

onMounted(load)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <section class="tasks">
    <header>
      <div>
        <h1>联邦任务</h1>
        <p>你发起的跨节点检索与带出处回答。每个任务都保留执行计划、覆盖账本与原始证据。</p>
      </div>
      <div class="header-actions">
        <el-button :disabled="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="composing = !composing">
          {{ composing ? '收起' : '新建任务' }}
        </el-button>
      </div>
    </header>
    <section v-if="composing" class="compose">
      <h2>新建联邦任务</h2>
      <TaskComposer @created="created" />
    </section>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取任务…</p>
    <p v-else-if="!error && !items.length" class="muted">还没有联邦任务。</p>
    <div v-if="items.length" class="scroll">
      <table>
        <thead>
          <tr><th>问题</th><th>范围</th><th>执行</th><th>检索完成度</th><th>证据充分性</th><th>创建时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.root_task_id">
            <td class="query">
              <RouterLink :to="{ name: 'federation-task', params: { rootTaskId: item.root_task_id } }">
                {{ item.query || '（无问题文本）' }}
              </RouterLink>
              <span class="ddp-mono muted id">{{ item.root_task_id }}</span>
            </td>
            <td>{{ SCOPE_KIND_LABEL[item.scope_kind] ?? item.scope_kind }} · {{ searchModeLabel(item.search_mode) }}</td>
            <td><StatusTag :meta="metaOf(TASK_STATUS, item.status)" /></td>
            <td><StatusTag :meta="metaOf(RETRIEVAL_COMPLETENESS, item.retrieval_completeness)" /></td>
            <td><StatusTag :meta="metaOf(EVIDENCE_SUFFICIENCY, item.evidence_sufficiency)" /></td>
            <td class="ddp-mono">{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <nav class="pagination" aria-label="任务分页">
      <el-button :disabled="cursors.length <= 1 || loading" @click="back">上一页</el-button>
      <span class="ddp-num">{{ cursors.length }}</span>
      <el-button :disabled="!next || loading" @click="forward">下一页</el-button>
    </nav>
  </section>
</template>

<style scoped>
.tasks { max-width: 1120px; margin: auto; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
.header-actions { display: flex; gap: 12px; flex-shrink: 0; }
.compose { padding: 20px 0 24px; border-top: var(--ddp-bw) solid var(--ddp-line); border-bottom: var(--ddp-bw) solid var(--ddp-line); margin-bottom: 24px; }
.compose h2 { font-size: 18px; font-weight: 600; margin: 0 0 16px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
header p { color: var(--ddp-ink-2); margin: 8px 0 0; }
.muted { color: var(--ddp-ink-3); }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); }
.scroll { overflow-x: auto; }
table { width: 100%; min-width: 760px; border-collapse: collapse; }
th, td { padding: 10px 12px 10px 0; text-align: left; border-bottom: var(--ddp-bw) solid var(--ddp-line); font-size: 13.5px; vertical-align: top; }
th { color: var(--ddp-ink-3); font-weight: 500; }
.query { display: grid; gap: 2px; max-width: 360px; }
.query a { color: var(--ddp-ink); text-decoration: none; font-weight: 500; overflow-wrap: anywhere; min-height: 24px; }
.query a:hover { text-decoration: underline; }
.id { font-size: 11.5px; }
.pagination { display: flex; align-items: center; justify-content: flex-end; gap: 12px; margin-top: 20px; }
@media (max-width: 640px) { header { flex-direction: column; } }
</style>
