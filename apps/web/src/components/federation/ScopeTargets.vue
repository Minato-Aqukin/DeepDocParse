<script setup lang="ts">
import { ref } from 'vue'

import { tasksApi } from '@/api/tasks'
import StatusTag from '@/components/common/StatusTag.vue'
import { COVERAGE_TARGET_STATE, metaOf } from '@/constants/federation'

/**
 * 已封存范围的目标分页（`GET /api/v1/federation/scopes/{scope_id}/targets`）。
 *
 * 分母来自调用方已封存的范围清单：第一页不带 cursor 起读，之后按 `next_cursor`
 * 跟随，`complete=true` 的终止页成员为空。目标行带**实时撤销覆盖**：
 * 撤销（revoked）与失联（unreachable）是读时算出来的当前态，不改写冻结的分母。
 */
const props = defineProps<{ scopeId: string }>()

const pages = ref<{ targets: { target_key: { origin_node_id: string; collection_id: string; operation: string }; state: string }[]; next_cursor: string | null; complete: boolean; total_targets: number; expired: boolean }[]>([])
const error = ref('')
const loading = ref(false)

async function read(cursor?: string) {
  loading.value = true
  error.value = ''
  try {
    const { data } = await tasksApi.scopeTargets(props.scopeId, cursor)
    pages.value = [...pages.value, data]
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
  void read()
}
defineExpose({ reset })
</script>

<template>
  <section class="scope-targets" aria-label="范围目标">
    <div class="actions">
      <el-button :loading="loading" text @click="reset">读取范围目标第一页</el-button>
      <el-button
        v-if="pages.length && !pages[pages.length - 1]?.complete"
        :loading="loading" text @click="read(pages[pages.length - 1]?.next_cursor ?? undefined)"
      >
        继续读下一页
      </el-button>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-for="(page, i) in pages" :key="i">
      <p class="meta">
        第 {{ i + 1 }} 页 · 共 {{ page.total_targets }} 个目标 ·
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
.meta { margin: 0; color: var(--ddp-ink-2); font-size: 12.5px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 4px; }
li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12.5px; }
</style>
