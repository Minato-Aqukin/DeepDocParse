<script setup lang="ts">
import { computed } from 'vue'

import StatusTag from '@/components/common/StatusTag.vue'
import { COVERAGE_TARGET_STATE, ENUMERATION_STATE, metaOf, searchModeLabel } from '@/constants/federation'
import type { CoverageLedger } from '@/federation/task-model'

/**
 * 覆盖账本：**分母（枚举出的目标）与分子（逐目标结果）分开摆**。
 * 没有它，"没找到"无法区分"查过了没有"和"根本没去查"（计划 §7.4）。
 */
const props = defineProps<{ ledger: CoverageLedger }>()
const counts = computed(() => props.ledger.counts)
</script>

<template>
  <section class="coverage" aria-label="覆盖账本">
    <p class="summary">
      {{ searchModeLabel(ledger.search_mode) }} · 范围枚举
      <StatusTag :meta="metaOf(ENUMERATION_STATE, ledger.enumeration_state)" />
      <span v-if="ledger.search_mode === 'fast'" class="muted">快速模式只查选中的候选，永远不声明查全。</span>
    </p>
    <table class="counts">
      <tbody>
        <tr><th scope="row">枚举目标</th><td class="ddp-num">{{ counts.total_targets }}</td></tr>
        <tr><th scope="row">适用目标</th><td class="ddp-num">{{ counts.applicable_targets }}</td></tr>
        <tr><th scope="row">已取回证据</th><td class="ddp-num">{{ counts.succeeded }}</td></tr>
        <tr><th scope="row">有依据地排除</th><td class="ddp-num">{{ counts.excluded }}</td></tr>
        <tr><th scope="row">未完成</th><td class="ddp-num">{{ counts.incomplete }}</td></tr>
      </tbody>
    </table>
    <div class="scroll">
      <table class="entries">
        <thead>
          <tr><th>目标</th><th>状态</th><th class="right">尝试</th><th>实际索引修订</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="(entry, i) in ledger.entries" :key="i">
            <td class="ddp-mono target">{{ entry.target_key.origin_node_id }} / {{ entry.target_key.collection_id }}</td>
            <td><StatusTag :meta="metaOf(COVERAGE_TARGET_STATE, entry.state)" /></td>
            <td class="ddp-num">{{ entry.attempts }}</td>
            <td class="ddp-mono">{{ entry.actual_index_revision ?? '—' }}</td>
            <!-- 排除依据可能是中文说明，不进等宽栈（准则六）；机器错误码本身是 ASCII，照样对齐 -->
            <td class="reason">{{ entry.last_error ?? entry.exclusion_basis ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.coverage { display: grid; gap: 12px; }
.summary { margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--ddp-ink-2); }
.muted { color: var(--ddp-ink-3); font-size: 13px; }
table { border-collapse: collapse; }
th, td { padding: 6px 12px 6px 0; text-align: left; font-size: 13.5px; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
th { color: var(--ddp-ink-3); font-weight: 500; }
.counts th { padding-right: 24px; }
.scroll { overflow-x: auto; }
.entries { min-width: 640px; width: 100%; }
.target, .reason { overflow-wrap: anywhere; font-size: 12.5px; }
.right { text-align: right; }
</style>
