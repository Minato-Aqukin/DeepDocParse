<script setup lang="ts">
import { computed, ref } from 'vue'

import StatusTag from '@/components/common/StatusTag.vue'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
import {
  COVERAGE_TARGET_STATE,
  EVIDENCE_SUFFICIENCY,
  RETRIEVAL_COMPLETENESS,
  VALIDATION_STATE,
  answerReason,
  conflictBasisLabel,
  metaOf,
  sourceTypeLabel,
} from '@/constants/federation'
import { citationIndex, type FederatedEvidence, type TaskResult } from '@/federation/task-model'

/**
 * 联邦任务结果 —— 结论、证据、矛盾、没查到的地方放在同一处。
 *
 * 顺序是刻意的：**先说证据够不够、查全没有，再给答案**。把"证据不足""只查了部分范围"
 * 放在答案下面，用户读完答案才看到限定，等于把限定藏起来（不变式 2）。
 * 答案文本按纯文本渲染，`[n]` 只替换成指向第 n 条证据的按钮 —— 生成文本不走 v-html。
 */
const props = defineProps<{
  result: TaskResult
  /** 协调者（本中心）的节点身份：来源是它的证据可以在本站打开原文 */
  coordinatorNodeId: string | null
}>()

const previewId = ref('')
const focused = ref<number | null>(null)
const index = computed(() => citationIndex(props.result.evidence))
const reason = computed(() => answerReason(props.result.answer_reason))
const sufficiency = computed(() => metaOf(EVIDENCE_SUFFICIENCY, props.result.evidence_sufficiency))
const completeness = computed(() => metaOf(RETRIEVAL_COMPLETENESS, props.result.retrieval_completeness))
const partial = computed(() => props.result.retrieval_completeness !== 'complete')

type Segment = { text: string; cite?: number }
const segments = computed<Segment[][]>(() => (props.result.answer ?? '').split(/\n+/).filter((line) => line.trim())
  .map((line) => {
    const parts: Segment[] = []
    let last = 0
    for (const match of line.matchAll(/\[(\d+)\]/g)) {
      const at = match.index ?? 0
      if (at > last) parts.push({ text: line.slice(last, at) })
      parts.push({ text: match[0], cite: Number(match[1]) })
      last = at + match[0].length
    }
    if (last < line.length) parts.push({ text: line.slice(last) })
    return parts
  }))

function numbersOf(refs: string[]): number[] {
  return refs.map((ref) => index.value.get(ref)).filter((n): n is number => typeof n === 'number')
}

function focus(n: number) {
  focused.value = n
  document.getElementById(`evidence-${n}`)?.scrollIntoView({ block: 'nearest' })
}

function isLocal(item: FederatedEvidence) {
  return !!props.coordinatorNodeId && item.origin_node_id === props.coordinatorNodeId
}

function locator(item: FederatedEvidence) {
  const { physical_page_index: page, seq } = item.locator ?? {}
  const parts = []
  if (typeof page === 'number') parts.push(`第 ${page + 1} 页`)
  if (typeof seq === 'number') parts.push(`块 ${seq}`)
  return parts.join(' · ') || '定位缺失'
}
</script>

<template>
  <section class="task-result" aria-label="任务结果">
    <div class="axes">
      <span class="axis"><span class="axis-label">证据充分性</span><StatusTag :meta="sufficiency" /></span>
      <span class="axis"><span class="axis-label">检索完成度</span><StatusTag :meta="completeness" /></span>
    </div>

    <p v-if="result.evidence_sufficiency === 'insufficient'" class="ddp-degraded">
      证据不足：本次取得的原文不足以支撑结论，系统不替你下结论。
    </p>
    <div v-if="result.conflicts.length" class="ddp-degraded" role="note" aria-label="证据矛盾">
      <p class="lead">证据之间存在矛盾，需要人工复核 —— 系统不会替你挑一个。</p>
      <ul class="conflicts">
        <li v-for="(item, i) in result.conflicts" :key="i">
          <span>{{ conflictBasisLabel(item.basis) }}</span>
          <span class="refs">
            <button v-for="n in numbersOf(item.evidence_refs)" :key="n" type="button" class="cite" @click="focus(n)">[{{ n }}]</button>
          </span>
          <StatusTag :meta="metaOf(VALIDATION_STATE, item.semantic_review)" />
        </li>
      </ul>
    </div>
    <div v-if="partial && result.unretrieved_targets.length" class="ddp-degraded" role="note" aria-label="未查到的范围">
      <p class="lead">只查了部分范围。下面这些目标没有取回证据，"没找到"不代表那里没有：</p>
      <ul class="targets">
        <li v-for="(item, i) in result.unretrieved_targets" :key="i">
          <span class="ddp-mono">{{ item.target_key.origin_node_id }} / {{ item.target_key.collection_id }}</span>
          <StatusTag :meta="metaOf(COVERAGE_TARGET_STATE, item.state)" />
          <span v-if="item.last_error" class="ddp-mono muted">{{ item.last_error }}</span>
        </li>
      </ul>
    </div>

    <h2>回答</h2>
    <div v-if="result.answer" class="answer">
      <p v-for="(line, i) in segments" :key="i">
        <template v-for="(part, j) in line" :key="j">
          <button v-if="part.cite && part.cite <= result.evidence.length" type="button" class="cite"
            :aria-label="`查看证据 ${part.cite}`" @click="focus(part.cite)">{{ part.text }}</button>
          <span v-else>{{ part.text }}</span>
        </template>
      </p>
      <p class="provenance">
        生成：<span class="ddp-mono">{{ result.provider?.model ?? '—' }}</span>
        （{{ result.provider?.location === 'local' ? '本节点' : '远端节点' }}）
        · 外发内容：{{ result.disclosure.remote ? result.disclosure.payload.join('、') || '—' : '未外发' }}
        · 引用结构 <StatusTag :meta="metaOf(VALIDATION_STATE, result.validation_state)" />
      </p>
    </div>
    <p v-else-if="reason" class="ddp-degraded" :class="{ 'is-danger': reason.type === 'danger' }" role="status">
      没有生成回答：{{ reason.label }}
    </p>
    <p v-else class="muted">这个任务只取证据，不生成回答。</p>

    <template v-if="result.claim_evidence_bindings.length">
      <h2>主张与证据</h2>
      <ol class="claims">
        <li v-for="claim in result.claim_evidence_bindings" :key="claim.claim_id">
          <span>{{ claim.claim_text }}</span>
          <span class="refs">
            <button v-for="n in numbersOf(claim.evidence_refs)" :key="n" type="button" class="cite" @click="focus(n)">[{{ n }}]</button>
          </span>
          <span class="muted">语义支持 <StatusTag :meta="metaOf(VALIDATION_STATE, claim.semantic_review ?? 'needs_review')" /></span>
        </li>
      </ol>
    </template>

    <h2>证据（{{ result.evidence.length }}）</h2>
    <p v-if="!result.evidence.length" class="muted">没有取回任何证据。</p>
    <ol v-else class="evidence">
      <li v-for="(item, i) in result.evidence" :id="`evidence-${i + 1}`" :key="item.evidence_id"
        :class="{ focused: focused === i + 1 }">
        <span class="number ddp-num">[{{ i + 1 }}]</span>
        <div class="body">
          <p class="where">
            <span class="ddp-cite-page">{{ locator(item) }}</span>
            · {{ sourceTypeLabel(item.source_type) }}
            · {{ isLocal(item) ? '本节点' : `远端 ${item.origin_node_id}` }}
          </p>
          <p class="ids">
            资源 <span class="ddp-mono">{{ item.resource_id }}</span>
            · 版本 <span class="ddp-mono">{{ item.source_version_id }}</span>
            · 摘要 <span class="ddp-mono">{{ item.excerpt_digest.slice(7, 19) }}</span>
          </p>
          <el-button v-if="isLocal(item)" class="open" link @click="previewId = item.evidence_id">查看原文出处</el-button>
          <p v-else-if="coordinatorNodeId" class="muted">原文由来源节点持有，本页只显示固定定位。</p>
          <p v-else class="muted">执行计划没读到，暂时判断不了这条证据是否在本节点，不提供原文预览。</p>
        </div>
      </li>
    </ol>

    <el-drawer :model-value="!!previewId" size="min(560px, 100vw)" title="原文出处" @close="previewId = ''">
      <EvidencePreview v-if="previewId" :evidence-id="previewId" close-label="关闭" @close="previewId = ''" />
    </el-drawer>
  </section>
</template>

<style scoped>
.task-result { display: grid; gap: 14px; }
h2 { font-size: 16px; font-weight: 600; margin: 18px 0 0; }
p { margin: 0; }
.axes { display: flex; flex-wrap: wrap; gap: 20px; }
.axis { display: inline-flex; align-items: center; gap: 8px; }
.axis-label { color: var(--ddp-ink-3); font-size: 12.5px; }
.lead { margin-bottom: 6px; }
.conflicts, .targets, .claims, .evidence { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }
.conflicts li, .targets li, .claims li { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.claims { counter-reset: claim; }
.answer { display: grid; gap: 8px; line-height: var(--ddp-lh); }
.cite {
  border: 0; background: transparent; padding: 0 1px; font: inherit; cursor: pointer;
  color: var(--ddp-cite); font-weight: 500; font-variant-numeric: tabular-nums; min-height: 24px;
}
.cite:hover { text-decoration: underline; }
.refs { display: inline-flex; gap: 2px; }
.provenance { color: var(--ddp-ink-2); font-size: 13px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.muted { color: var(--ddp-ink-3); font-size: 13px; }
.evidence li {
  display: grid; grid-template-columns: 44px 1fr; gap: 8px; padding: 10px 0;
  border-bottom: var(--ddp-bw) solid var(--ddp-line);
}
.evidence li.focused { background: var(--ddp-panel-2); }
.number { text-align: left; color: var(--ddp-ink-2); }
.body { display: grid; gap: 4px; min-width: 0; }
.where { font-size: 13.5px; }
.ids { font-size: 12px; color: var(--ddp-ink-2); overflow-wrap: anywhere; }
/* 中文标签不进等宽栈：等宽族里的汉字会按全角排开，读成"资 源"（准则六） */
.open { justify-self: start; }
</style>
