<script setup lang="ts">
import { onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'

import { stashTaskPrefill, workspaceError, type SourceSummary } from '@/platform/desktop'

/**
 * 中心源的「作为联邦任务发起」入口（plan §1.5）。
 *
 * 联邦任务只在本机账本建：中心源下不跳回只读的 `/tasks/new` 循环，
 * 而是说明去处、列出本机工作区供切换；切源即整页重载，预填按目标源
 * 存进 sessionStorage（`stashTaskPrefill`），重载后落在 `/tasks/new`。
 * 没有本机源时指到 `/sources`。
 */
const props = withDefaults(defineProps<{
  query?: string
  purpose?: 'answer' | 'wiki'
  title?: string
}>(), { query: '', purpose: 'answer', title: '' })

const router = useRouter()
const locals = shallowRef<SourceSummary[]>([])
const loading = ref(true)
const error = ref('')
const switching = ref('')

interface CenterSwitchHost {
  sourceList?: () => Promise<{ ok: boolean; value?: SourceSummary[]; error?: { code?: string } }>
  sourceActivate?: (input: { sourceId: string }) => Promise<{ ok: boolean; error?: { code?: string } }>
}

function host(): CenterSwitchHost | undefined {
  return window.ddpDesktop as unknown as CenterSwitchHost | undefined
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await host()?.sourceList?.()
    if (!result) {
      error.value = '宿主暂未提供数据源接口，请更新桌面端后重试'
      return
    }
    if (!result.ok) {
      error.value = `本机工作区列表读取失败：${workspaceError(new Error(result.error?.code ?? ''))}`
      return
    }
    locals.value = (result.value ?? []).filter((s) => s.kind === 'local' && s.state === 'ready')
  } catch (cause) {
    error.value = `本机工作区列表读取失败：${cause instanceof Error ? cause.message : String(cause)}`
  } finally {
    loading.value = false
  }
}

async function switchTo(target: SourceSummary) {
  if (switching.value) return
  switching.value = target.sourceId
  error.value = ''
  try {
    stashTaskPrefill({
      sourceId: target.sourceId,
      ...(props.query ? { query: props.query } : {}),
      ...(props.purpose === 'wiki' ? { purpose: 'wiki' as const } : {}),
      ...(props.title ? { title: props.title } : {}),
    })
    const result = await host()?.sourceActivate?.({ sourceId: target.sourceId })
    if (!result) {
      error.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      error.value = `切换失败：${workspaceError(new Error(result.error?.code ?? ''))}，请在数据源页重试`
      return
    }
    // 切源即整页重载：先把散列指到本机准备页（预填走暂存，不经 URL 跨源传问题文本）。
    location.hash = '#/tasks/new'
    location.reload()
  } finally {
    switching.value = ''
  }
}

function goSources() {
  void router.push({ name: 'sources' })
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="propose-switch" aria-label="在本地准备联邦任务">
    <p class="muted">联邦任务只在本机账本准备与批准：问题、用途与标题先在本机工作区生成待审阅计划，经原生对话框批准后才派发。当前是中心源（只读），选一个本机工作区继续，预填已带过去。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取本机工作区…</p>
    <ul v-else-if="locals.length" class="source-list">
      <li v-for="s in locals" :key="s.sourceId">
        <span>{{ s.label }}</span>
        <el-button size="small" type="primary" :loading="switching === s.sourceId" @click="switchTo(s)">
          切换到此工作区并继续
        </el-button>
      </li>
    </ul>
    <p v-else class="muted">还没有可用的本机工作区。</p>
    <el-button v-if="!loading && !locals.length" @click="goSources">去数据源页打开本机工作区</el-button>
  </section>
</template>

<style scoped>
.propose-switch { display: grid; gap: 12px; }
.muted { color: var(--ddp-ink-3); font-size: 13px; margin: 0; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.source-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.source-list li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
</style>
