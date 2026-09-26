<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import LocalTaskDetail from '@/components/federation/LocalTaskDetail.vue'
import TaskDetailBody from '@/components/federation/TaskDetailBody.vue'
import { approvedPlanLabel, getActiveSource, isDesktop } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

/**
 * 联邦任务详情页。数据源决定形态：
 *
 * - 桌面本机源：本机账本详情（`LocalTaskDetail`：`clientPlanGet`，两阶段批准经原生对话框）。
 * - 桌面中心源：只读镜像 —— 沿用 Web 详情（经 GET 代理读），composer/批准/确认写入口禁用。
 * - 浏览器：Web 详情（行为不变）。
 *
 * 路由参数名按形态区分：本机 `planId`，Web/中心 `rootTaskId`。
 */
const route = useRoute()
const auth = useAuthStore()
const desktop = isDesktop()
const activeSource = computed(() => getActiveSource())
const isLocalSource = computed(() => desktop && activeSource.value?.kind === 'local')
const centerReadonly = computed(() => desktop && activeSource.value?.kind === 'center' && auth.readOnly)
</script>

<template>
  <LocalTaskDetail v-if="isLocalSource" :key="String(route.params.planId ?? '')" />
  <section v-else class="detail-wrap">
    <p v-if="centerReadonly" class="readonly-hint" role="note">{{ approvedPlanLabel() }}</p>
    <TaskDetailBody
      :key="String(route.params.rootTaskId ?? '')"
      :root-task-id="String(route.params.rootTaskId ?? '')"
      :read-only="centerReadonly" />
  </section>
</template>

<style scoped>
.detail-wrap { display: grid; gap: 12px; }
.readonly-hint { color: var(--ddp-ink-3); font-size: 13px; max-width: 1120px; margin: 0 auto; width: 100%; }
</style>
