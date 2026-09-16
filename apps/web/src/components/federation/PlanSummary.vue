<script setup lang="ts">
import { computed } from 'vue'

import StatusTag from '@/components/common/StatusTag.vue'
import { PLANNING_STATE, metaOf, retentionLabel } from '@/constants/federation'
import type { TaskPlan } from '@/federation/task-model'

/**
 * 被批准的是**这一份计划修订**：谁执行哪一步、哪些数据发给谁、经过谁中继、保留多久。
 * 只读展示，数字等宽；本节点与远端分开标，外发一眼可见。
 */
const props = defineProps<{ plan: TaskPlan }>()
const node = (id: string) => (id === props.plan.root_coordinator_node_id ? `本节点（${id}）` : `远端 ${id}`)
const budget = computed(() => props.plan.budget)
</script>

<template>
  <section class="plan" aria-label="执行计划">
    <p class="summary">
      修订 <span class="ddp-num">{{ plan.revision }}</span> ·
      <StatusTag :meta="metaOf(PLANNING_STATE, plan.planning_state)" /> ·
      摘要 <span class="ddp-mono">{{ plan.plan_digest.slice(7, 19) }}</span> ·
      有效至 <span class="ddp-mono">{{ plan.valid_until }}</span>
    </p>
    <div class="scroll">
      <table>
        <thead><tr><th>步骤</th><th>操作</th><th>执行者</th><th>依赖</th></tr></thead>
        <tbody>
          <tr v-for="step in plan.steps" :key="step.step_id">
            <td class="ddp-mono">{{ step.step_id }}</td>
            <td class="ddp-mono">{{ step.operation }}</td>
            <td>{{ node(step.executor_node_id) }}</td>
            <td class="ddp-mono">{{ step.depends_on.join(', ') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!plan.data_edges.length" class="muted">这份计划没有跨节点的数据边：取证与生成都不外发原文。</p>
    <div v-else class="scroll">
      <table>
        <thead><tr><th>数据边</th><th>从</th><th>到</th><th>内容</th><th>中继</th><th>保留</th></tr></thead>
        <tbody>
          <tr v-for="edge in plan.data_edges" :key="edge.edge_id">
            <td class="ddp-mono">{{ edge.edge_id }}</td>
            <td>{{ node(edge.from_node_id) }}</td>
            <td>{{ node(edge.to_node_id) }}</td>
            <td class="ddp-mono">{{ edge.payload_kind }}</td>
            <td>{{ edge.relay_via?.length ? edge.relay_via.map(node).join('、') : '—' }}</td>
            <td>{{ retentionLabel(edge.retention) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="budget">
      总预算：请求 <span class="ddp-num">{{ budget.max_requests }}</span> ·
      字节 <span class="ddp-num">{{ budget.max_bytes }}</span> ·
      生成 token <span class="ddp-num">{{ budget.max_generation_tokens ?? 0 }}</span> ·
      跳数 <span class="ddp-num">{{ budget.max_hops }}</span> ·
      截止 <span class="ddp-mono">{{ budget.deadline }}</span>
    </p>
  </section>
</template>

<style scoped>
.plan { display: grid; gap: 12px; }
.summary, .budget { margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; color: var(--ddp-ink-2); font-size: 13.5px; }
.muted { margin: 0; color: var(--ddp-ink-3); font-size: 13px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 560px; }
th, td { padding: 6px 12px 6px 0; text-align: left; font-size: 13.5px; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
th { color: var(--ddp-ink-3); font-weight: 500; }
</style>
