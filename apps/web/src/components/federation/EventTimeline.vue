<script setup lang="ts">
import StatusTag from '@/components/common/StatusTag.vue'
import { TASK_EVENT_TYPE, metaOf } from '@/constants/federation'
import type { TaskEvent } from '@/federation/task-model'

/**
 * 事件是进度记录，不是状态真相（状态以任务各轴为准）。按序号追加，断线后从
 * `next_seq` 续读；这里只负责把"发生了什么、什么时候"说清楚。
 */
defineProps<{ events: TaskEvent[] }>()

function detail(event: TaskEvent): string {
  const payload = event.payload ?? {}
  const parts: string[] = []
  for (const key of ['error', 'delivery_id', 'plan_digest', 'execution_consent_ref', 'generation']) {
    const value = payload[key]
    if (value !== null && value !== undefined && value !== '') {
      parts.push(`${key}=${typeof value === 'string' && value.startsWith('sha256:') ? value.slice(7, 19) : String(value)}`)
    }
  }
  return parts.join(' · ')
}
</script>

<template>
  <ol class="timeline" aria-label="任务事件">
    <li v-for="event in events" :key="event.seq">
      <span class="ddp-num seq">{{ event.seq }}</span>
      <span class="ddp-mono at">{{ event.at }}</span>
      <StatusTag :meta="metaOf(TASK_EVENT_TYPE, event.type)" />
      <span v-if="detail(event)" class="ddp-mono detail">{{ detail(event) }}</span>
    </li>
  </ol>
</template>

<style scoped>
.timeline { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
li { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 4px 0; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
.seq { min-width: 2ch; color: var(--ddp-ink-3); }
.at { color: var(--ddp-ink-2); font-size: 12px; }
.detail { color: var(--ddp-ink-3); font-size: 12px; overflow-wrap: anywhere; }
</style>
