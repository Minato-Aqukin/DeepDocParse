<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import CenterProposeSwitch from '@/components/federation/CenterProposeSwitch.vue'
import LocalTaskPrepare from '@/components/federation/LocalTaskPrepare.vue'
import DirectoryBrowser from '@/components/federation/DirectoryBrowser.vue'
import TaskComposer from '@/components/federation/TaskComposer.vue'
import { approvedPlanLabel, getActiveSource, isDesktop } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

/**
 * 联邦任务新建页。数据源决定形态：
 *
 * - 桌面本机源：准备表单（`LocalTaskPrepare`）—— query 参数（`query` / `purpose` /
 *   `title`）预填，与「作为联邦任务发起」入口对应。
 * - 桌面中心源：只读 —— 说明任务只在本机账本建，给出本机工作区切换
 *   （`CenterProposeSwitch`）；切源即整页重载，预填走 sessionStorage 暂存，
 *   落在 `/tasks/new` 的准备页。不再跳回只读的本页循环。
 * - 浏览器：`TaskComposer`（行为不变）。
 */
const route = useRoute()
const router = useRouter()
const proposeQuery = computed(() => typeof route.query.query === 'string' ? route.query.query : '')
const proposePurpose = computed(() => route.query.purpose === 'wiki' ? 'wiki' as const : 'answer' as const)
const proposeTitle = computed(() => typeof route.query.title === 'string' ? route.query.title : '')
const auth = useAuthStore()
const desktop = isDesktop()
const activeSource = computed(() => getActiveSource())
const isLocalSource = computed(() => desktop && activeSource.value?.kind === 'local')
const centerReadonly = computed(() => desktop && activeSource.value?.kind === 'center' && auth.readOnly)

const prefill = ref(typeof route.query.query === 'string' ? route.query.query : '')
watch(() => route.query.query, (q) => {
  if (typeof q === 'string' && q) prefill.value = q
})

function created(rootTaskId: string) {
  void router.push({ name: 'federation-task', params: { rootTaskId } })
}
</script>

<template>
  <LocalTaskPrepare v-if="isLocalSource" />
  <section v-else class="compose-page">
    <header>
      <RouterLink class="back" :to="{ name: 'federation-tasks' }">← 联邦任务</RouterLink>
      <h1>发起联邦任务</h1>
      <p v-if="centerReadonly" class="readonly-hint" role="note">{{ approvedPlanLabel() }}</p>
    </header>
    <section v-if="centerReadonly" class="compose">
      <h2>在本机准备联邦任务</h2>
      <CenterProposeSwitch :query="proposeQuery" :purpose="proposePurpose" :title="proposeTitle" />
    </section>
    <section v-else class="compose">
      <h2>新建联邦任务</h2>
      <p v-if="prefill" class="muted">从只读页面带入的问题已预填，可直接提交或再改。</p>
      <TaskComposer :initial-query="prefill" @created="created" />
    </section>
    <section class="compose">
      <h2>互联公开目录</h2>
      <DirectoryBrowser />
    </section>
  </section>
</template>

<style scoped>
.compose-page { max-width: 1120px; margin: auto; display: grid; gap: 16px; }
header { display: grid; gap: 6px; justify-items: start; }
.back { color: var(--ddp-ink-2); text-decoration: none; font-size: 13px; width: fit-content; min-height: 24px; }
.back:hover { text-decoration: underline; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 0 0 12px; }
.compose { padding: 20px 0 24px; border-top: var(--ddp-bw) solid var(--ddp-line); }
.muted { color: var(--ddp-ink-3); }
.readonly-hint { color: var(--ddp-ink-3); font-size: 13px; }
</style>
