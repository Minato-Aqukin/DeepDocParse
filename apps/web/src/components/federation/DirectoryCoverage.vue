<script setup lang="ts">
import type { DirectoryScopeEnvelope } from '@/api/directory'
import StatusTag from '@/components/common/StatusTag.vue'
import { ENUMERATION_STATE, metaOf } from '@/constants/federation'

defineProps<{ scope: DirectoryScopeEnvelope }>()
</script>

<template>
  <section class="coverage" aria-label="目录覆盖与水位">
    <h3>目录覆盖与水位</h3>
    <p>
      范围枚举 <StatusTag :meta="metaOf(ENUMERATION_STATE, scope.effective_enumeration_state)" />
      <span class="ddp-mono">{{ scope.effective_enumeration_state }}</span> ·
      本范围已观测 {{ scope.total_targets }} 个公开集合目标
    </p>
    <p v-if="scope.effective_enumeration_state !== 'sealed' || scope.manifest.unexpanded_subtrees.length">
      部分枚举：未展开分支不计入已观测目标，不能据此宣称查全。
    </p>
    <p v-else>已封存本次有界范围的枚举；不代表全网普查或最新内容。</p>
    <p>内容未冻结；目录水位不是内容快照。有效至 <span class="ddp-mono">{{ scope.manifest.valid_until }}</span><span v-if="scope.expired"> · 范围已过期，请重新封存</span>。</p>
    <div class="scroll">
      <table>
        <caption>逐节点目录水位（修订 / 取回时间）</caption>
        <thead><tr><th>来源节点</th><th>目录</th><th>目录修订</th><th>取回时间 fetched_at</th></tr></thead>
        <tbody>
          <tr v-for="(revision, index) in scope.manifest.registry_revision_vector" :key="index">
            <td class="ddp-mono">{{ revision.node_id }}</td>
            <td>{{ revision.directory_ref ?? '未提供目录类型' }}</td>
            <td class="ddp-num">{{ revision.registry_revision }}</td>
            <td class="ddp-mono">{{ revision.fetched_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <section aria-label="未展开节点">
      <h4>未展开节点 / 分支</h4>
      <ul v-if="scope.manifest.unexpanded_subtrees.length">
        <li v-for="(branch, index) in scope.manifest.unexpanded_subtrees" :key="index"><span class="ddp-mono">{{ branch.node_id }}</span> · {{ branch.reason }}</li>
      </ul>
      <p v-else>本范围清单未记录未展开分支；不推断其他节点不存在。</p>
    </section>
  </section>
</template>

<style scoped>
.coverage { display: grid; gap: 12px; }
h3, h4, p { margin: 0; }
h3, h4 { font-size: 14px; font-weight: 600; }
p, li { color: var(--ddp-ink-2); font-size: 13px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 540px; }
caption { text-align: left; color: var(--ddp-ink-2); font-size: 13px; margin-bottom: 8px; }
th, td { padding: 6px 12px 6px 0; text-align: left; font-size: 13px; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
th { color: var(--ddp-ink-3); font-weight: 500; }
ul { margin: 8px 0 0; padding-left: 20px; }
</style>
