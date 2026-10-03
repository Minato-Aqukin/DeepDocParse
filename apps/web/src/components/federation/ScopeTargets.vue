<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { tasksApi } from '@/api/tasks'
import { directoryApi } from '@/api/directory'
import StatusTag from '@/components/common/StatusTag.vue'
import { COVERAGE_TARGET_STATE, metaOf } from '@/constants/federation'

/**
 * 已封存范围的目标分页（`GET /api/v1/federation/scopes/{scope_id}/targets`）。
 *
 * 分母来自调用方已封存的范围清单：第一页不带 cursor 起读，之后按 `next_cursor`
 * 跟随，`complete=true` 的终止页成员为空。目标行带**实时撤销覆盖**：
 * 撤销（revoked）与失联（unreachable）是读时算出来的当前态，不改写冻结的分母。
 */
const props = defineProps<{ scopeId: string; localNodeId?: string }>()

const pages = ref<{ targets: { target_key: { origin_node_id: string; collection_id: string; operation: string }; state: string }[]; next_cursor: string | null; complete: boolean; total_targets: number; expired: boolean }[]>([])
const error = ref('')
const loading = ref(false)
const metadata = ref<Record<string, { name?: string; owner_id?: string; error?: string; publication?: string }>>({})
const targets = computed(() => pages.value.flatMap(page => page.targets))
const groups = computed(() => [
  { label: '本站公开集合', local: true, targets: targets.value.filter(target => target.target_key.origin_node_id === props.localNodeId) },
  { label: '远端公开集合', local: false, targets: targets.value.filter(target => target.target_key.origin_node_id !== props.localNodeId) },
])

async function read(cursor?: string) {
  loading.value = true
  error.value = ''
  try {
    const { data } = await tasksApi.scopeTargets(props.scopeId, cursor)
    pages.value = [...pages.value, data]
    if (props.localNodeId) {
      await Promise.all(data.targets.filter(target => target.target_key.origin_node_id === props.localNodeId).map(async target => {
        const id = target.target_key.collection_id
        try {
          const response = await directoryApi.collection(id)
          metadata.value[id] = response.data
        } catch {
          metadata.value[id] = { error: '归属详情不可读取（可能已撤销或无权）' }
        }
      }))
    }
  } catch (cause) {
    const response = (cause as { response?: { status?: number; data?: { error?: { code?: string; message?: string } } } })?.response
    if (response?.status === 404) error.value = '范围或游标不可用（404）：不借状态码泄露存在性。'
    else if (response?.status === 410) error.value = '范围已过期（410 scope_expired）：请重新枚举生成新范围。'
    else {
      const detail = response?.data?.error
      error.value = detail?.code
        ? `范围目标读取失败：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
        : `范围目标读取失败：${cause instanceof Error ? cause.message : String(cause)}`
    }
  } finally {
    loading.value = false
  }
}

function reset() {
  pages.value = []
  error.value = ''
  metadata.value = {}
  void read()
}
defineExpose({ reset })
onMounted(() => { if (props.localNodeId) reset() })
</script>

<template>
  <section class="scope-targets" aria-label="范围目标">
    <div class="actions">
      <el-button v-if="!localNodeId" :loading="loading" text @click="reset">读取范围目标第一页</el-button>
      <el-button
        v-if="pages.length && !pages[pages.length - 1]?.complete"
        :loading="loading" text @click="read(pages[pages.length - 1]?.next_cursor ?? undefined)"
      >
        {{ localNodeId ? '继续读集合下一页' : '继续读下一页' }}
      </el-button>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="localNodeId">
      <p v-if="pages.length" class="meta">
        已读 {{ targets.length }} 个本范围目标 ·
        {{ pages[pages.length - 1]?.complete ? '已读到本范围终止页' : '尚有目录页未读' }}
        <span v-if="pages.some(page => page.expired)"> · 范围已过期，请重新封存</span>
      </p>
      <section v-for="group in groups" :key="group.label" :aria-label="group.label">
        <h3>{{ group.label }}</h3>
        <ul>
          <li v-for="target in group.targets" :key="`${target.target_key.origin_node_id}/${target.target_key.collection_id}`">
            <template v-if="!group.local || !metadata[target.target_key.collection_id]?.publication || metadata[target.target_key.collection_id]?.publication === 'published'">
              <span>{{ group.local ? (metadata[target.target_key.collection_id]?.name ?? target.target_key.collection_id) : target.target_key.collection_id }}</span>
              <span class="ddp-mono">来源节点 {{ target.target_key.origin_node_id }} / {{ target.target_key.collection_id }}</span>
              <span v-if="group.local">归属 {{ metadata[target.target_key.collection_id]?.owner_id ?? metadata[target.target_key.collection_id]?.error ?? '正在读取归属…' }}</span>
              <span v-else class="meta">远端目录未提供归属引用</span>
              <StatusTag :meta="metaOf(COVERAGE_TARGET_STATE, target.state)" />
            </template>
            <span v-else class="meta">该本站集合已不再公开 · {{ target.target_key.collection_id }}</span>
          </li>
        </ul>
        <p v-if="!group.targets.length && !loading" class="meta">当前已读目录页内暂无集合。</p>
      </section>
    </template>
    <template v-else v-for="(page, i) in pages" :key="i">
      <p class="meta">
        第 {{ i + 1 }} 页 · 本范围 {{ page.total_targets }} 个目标 ·
        {{ page.complete ? '已读到终止页' : '还有下一页' }} ·
        {{ page.expired ? '范围已过期' : '范围内' }}
      </p>
      <ul>
        <li v-for="(target, j) in page.targets" :key="j">
          <span class="ddp-mono">{{ target.target_key.origin_node_id }} / {{ target.target_key.collection_id }}</span>
          <StatusTag :meta="metaOf(COVERAGE_TARGET_STATE, target.state)" />
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.scope-targets { display: grid; gap: 8px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
h3 { margin: 12px 0 8px; font-size: 14px; font-weight: 600; }
.meta { margin: 0; color: var(--ddp-ink-2); font-size: 12.5px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 4px; }
li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12.5px; }
</style>
