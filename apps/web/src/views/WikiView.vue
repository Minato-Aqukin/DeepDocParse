<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { knowledgeApi } from '@/api'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
import GraphCanvas from '@/components/knowledge/GraphCanvas.vue'
import type { EvidenceBacklink, KnowledgeGraph, WikiDetail, WikiSentence, WikiSummary } from '@/types/api'

const entries = ref<WikiSummary[]>([])
const detail = ref<WikiDetail>()
const localGraph = ref<KnowledgeGraph>({ graph_version: 'ddp-graph/1', entities: [], edges: [] })
const evidenceId = ref('')
const backlinks = ref<EvidenceBacklink[]>([])
const loading = ref(false)
const errorText = ref('')
const filter = ref('')
const selectedEntryId = ref('')

const grouped = computed(() => {
  const out: Record<string, WikiSummary[]> = {}
  for (const entry of entries.value.filter((row) => row.title.toLowerCase().includes(filter.value.toLowerCase()))) {
    ;(out[entry.entity.entity_type] ||= []).push(entry)
  }
  return out
})

async function loadList() {
  loading.value = true
  errorText.value = ''
  try {
    entries.value = (await knowledgeApi.wikiList()).data
    const first = entries.value[0]
    if (first) await selectEntry(first)
  } catch (error) {
    errorText.value = `Wiki 加载失败：${String(error)}`
  } finally {
    loading.value = false
  }
}

async function selectEntry(entry: WikiSummary) {
  selectedEntryId.value = entry.id
  evidenceId.value = ''
  backlinks.value = []
  try {
    const [wiki, graph] = await Promise.all([
      knowledgeApi.wiki(entry.id), knowledgeApi.graph(entry.entity.id, 1),
    ])
    detail.value = wiki.data
    localGraph.value = graph.data
  } catch (error) {
    errorText.value = `条目加载失败：${String(error)}`
  }
}

async function selectSentence(sentence: WikiSentence) {
  const citation = sentence.citations.find((row) => row.resolved && row.evidence_id)
  evidenceId.value = citation?.evidence_id || ''
  if (!evidenceId.value) {
    backlinks.value = []
    return
  }
  try {
    backlinks.value = (await knowledgeApi.backlinks(evidenceId.value)).data.backlinks
  } catch (error) {
    errorText.value = `反链加载失败：${String(error)}`
  }
}

onMounted(loadList)
</script>

<template>
  <div class="wiki" v-loading="loading">
    <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" />
    <aside class="tree">
      <h2>知识 Wiki</h2>
      <el-input v-model="filter" placeholder="筛选实体" clearable />
      <el-empty v-if="!entries.length && !loading" description="尚无 Wiki 条目" :image-size="72" />
      <section v-for="(rows, kind) in grouped" :key="kind">
        <h3>{{ kind }}</h3>
        <button v-for="entry in rows" :key="entry.id" type="button"
                :class="{ active: entry.id === selectedEntryId }" @click="selectEntry(entry)">
          {{ entry.title }}
        </button>
      </section>
    </aside>

    <main>
      <template v-if="detail">
        <header><h1>{{ detail.entry.title }}</h1><p>{{ detail.entry.entity.aliases.join(' · ') || detail.entry.entity.entity_type }}</p></header>
        <section v-for="section in detail.sections" :key="section.id" class="section">
          <h2>{{ section.heading }}</h2>
          <button v-for="sentence in section.sentences" :key="sentence.id" type="button"
                  class="sentence" :class="{ unsupported: sentence.unsupported }"
                  @click="selectSentence(sentence)">
            <span>{{ sentence.text }}</span>
            <small v-if="sentence.unsupported">unsupported · 无法指回 bbox</small>
            <small v-else>引用 {{ sentence.evidence_ids.length }} 条</small>
            <small v-if="sentence.conflict_group" class="conflict">冲突组 {{ sentence.conflict_group }} · 与同组说法并列</small>
          </button>
        </section>
      </template>
      <el-empty v-else description="从左侧选择条目" />
    </main>

    <aside class="evidence">
      <EvidencePreview v-if="evidenceId" :evidence-id="evidenceId" close-label="关闭证据"
                       @close="evidenceId = ''; backlinks = []" />
      <section v-if="evidenceId" class="backlinks">
        <h3>反链</h3>
        <p v-if="!backlinks.length">当前没有其他引用。</p>
        <article v-for="item in backlinks" :key="`${item.source_kind}:${item.source_id}`">
          <code>{{ item.source_kind }}</code><span>{{ item.label }}</span>
        </article>
      </section>
      <template v-else>
        <h3>局部图谱 · 1 跳</h3>
        <GraphCanvas :entities="localGraph.entities" :edges="localGraph.edges" :height="300"
                     :selected-entity-id="detail?.entry.entity.id" />
        <p class="hint">点击正文句子后，这里会切换到四层证据预览和完整反链。</p>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.wiki { display: grid; grid-template-columns: 220px minmax(360px, 1fr) minmax(320px, 420px); gap: 0; min-height: calc(100vh - 120px); background: var(--ddp-panel); border: 1px solid var(--ddp-line); }
.wiki > .el-alert { grid-column: 1 / -1; }
.tree, .evidence { padding: 16px; min-width: 0; overflow: auto; }
.tree { border-right: 1px solid var(--ddp-line); }
.evidence { border-left: 1px solid var(--ddp-line); }
.tree h2, main h1, main h2, h3, p { margin: 0; }
.tree > section { margin-top: 18px; }
.tree h3 { margin-bottom: 6px; color: var(--ddp-ink-3); font-size: 12px; text-transform: uppercase; }
.tree button { display: block; width: 100%; min-height: 40px; padding: 7px 9px; border: 0; border-left: 2px solid transparent; background: transparent; color: var(--ddp-ink-2); text-align: left; cursor: pointer; }
.tree button:hover, .tree button.active { background: color-mix(in srgb, var(--ddp-ink) 6%, transparent); color: var(--ddp-ink); }
.tree button.active { border-left-color: var(--ddp-ink); font-weight: 600; }
main { padding: 28px 34px; overflow: auto; }
main header { padding-bottom: 20px; border-bottom: 1px solid var(--ddp-line); }
main header p, .hint { margin-top: 6px; color: var(--ddp-ink-3); }
.section { margin-top: 28px; }
.section > h2 { margin-bottom: 12px; font-size: 18px; }
.sentence { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 14px; width: 100%; min-height: 44px; padding: 9px 10px; border: 0; border-left: 2px solid var(--ddp-cite); background: transparent; color: var(--ddp-ink); text-align: left; cursor: pointer; }
.sentence:hover { background: color-mix(in srgb, var(--ddp-cite) 6%, transparent); }
.sentence.unsupported { border-left-color: var(--ddp-danger); }
.sentence small { color: var(--ddp-cite); white-space: nowrap; }
.sentence.unsupported small { color: var(--ddp-danger); }
.sentence .conflict { grid-column: 1 / -1; color: var(--ddp-ink-3); white-space: normal; }
.evidence { display: grid; align-content: start; gap: 14px; }
.backlinks { display: grid; gap: 8px; padding-top: 14px; border-top: 1px solid var(--ddp-line); }
.backlinks article { display: grid; gap: 3px; padding: 7px 0; border-bottom: 1px solid var(--ddp-line); }
.backlinks code { color: var(--ddp-ink-3); font-family: var(--ddp-font-mono); font-size: 11px; }
@media (max-width: 1200px) { .wiki { grid-template-columns: 200px 1fr; } .evidence { grid-column: 1 / -1; border: 0; border-top: 1px solid var(--ddp-line); } }
@media (max-width: 760px) { .wiki { grid-template-columns: 1fr; } .tree { border: 0; border-bottom: 1px solid var(--ddp-line); } main { padding: 20px 16px; } }
</style>
