<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { knowledgeApi } from '@/api'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
import GraphCanvas from '@/components/knowledge/GraphCanvas.vue'
import ReviewQueue from '@/components/knowledge/ReviewQueue.vue'
import type { KnowledgeEdge, KnowledgeEntity, KnowledgeGraph } from '@/types/api'

const graph = ref<KnowledgeGraph>({ graph_version: 'ddp-graph/1', entities: [], edges: [] })
const loading = ref(false)
const building = ref(false)
const errorText = ref('')
const entityType = ref('')
const minimumConfidence = ref(0)
const selectedEntity = ref<KnowledgeEntity>()
const selectedEdge = ref<KnowledgeEdge>()
const evidenceId = ref('')
const splitAlias = ref('')
const edgePicker = ref('')

const entityTypes = computed(() => [...new Set(graph.value.entities.map((row) => row.entity_type))].sort())
const filteredEntities = computed(() => graph.value.entities.filter((row) =>
  !entityType.value || row.entity_type === entityType.value))
const filteredIds = computed(() => new Set(filteredEntities.value.map((row) => row.id)))
const filteredEdges = computed(() => graph.value.edges.filter((row) =>
  row.confidence >= minimumConfidence.value
  && filteredIds.value.has(row.subject_id) && filteredIds.value.has(row.object_id)))
// 键盘兜底不能把千条边各自渲染成 DOM；canvas 承担全量，选择器只列前 200 条。
const accessibleEdges = computed(() => filteredEdges.value.slice(0, 200))

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    graph.value = (await knowledgeApi.graph()).data
  } catch (error) {
    errorText.value = `图谱加载失败：${String(error)}`
  } finally {
    loading.value = false
  }
}

function selectEdge(edge: KnowledgeEdge) {
  selectedEdge.value = edge
  selectedEntity.value = undefined
  evidenceId.value = edge.citations.find((row) => row.resolved)?.evidence_id || ''
}

function selectEdgeById() {
  const edge = filteredEdges.value.find((row) => row.id === edgePicker.value)
  if (edge) selectEdge(edge)
}

async function build() {
  building.value = true
  try {
    const result = (await knowledgeApi.build()).data
    ElMessage.success(`已生成 ${result.entities} 个实体、${result.edges} 条边、${result.wiki_entries} 个 Wiki 条目`)
    await load()
  } catch (error) {
    ElMessage.error(`知识生成失败：${String(error)}`)
  } finally {
    building.value = false
  }
}

async function split() {
  if (!selectedEntity.value || !splitAlias.value) return
  try {
    await knowledgeApi.split(selectedEntity.value.id, splitAlias.value)
    ElMessage.success(`已将 ${splitAlias.value} 拆成独立实体`)
    splitAlias.value = ''
    await load()
  } catch (error) {
    ElMessage.error(`拆分失败：${String(error)}`)
  }
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <header class="page-head">
      <div><h2>实体关系图谱</h2><p>每条边都可以点回原文证据；红色只表示证据缺失或低置信合并。</p></div>
      <el-button :loading="building" @click="build">从最新证据更新知识层</el-button>
    </header>
    <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" />

    <section class="toolbar">
      <el-select v-model="entityType" placeholder="全部实体类型" clearable>
        <el-option v-for="kind in entityTypes" :key="kind" :label="kind" :value="kind" />
      </el-select>
      <label>最低边置信度 <el-slider v-model="minimumConfidence" :min="0" :max="1" :step="0.05" /></label>
      <label class="edge-picker">键盘选边
        <select v-model="edgePicker" @change="selectEdgeById">
          <option value="">请选择</option>
          <option v-for="edge in accessibleEdges" :key="edge.id" :value="edge.id">
            {{ edge.predicate }} · {{ Math.round(edge.confidence * 100) }}%
          </option>
        </select>
      </label>
      <span>{{ filteredEntities.length }} 节点 · {{ filteredEdges.length }} 条边</span>
    </section>

    <el-empty v-if="!graph.entities.length && !loading && !errorText" description="知识图谱为空；先从已有证据生成" />
    <div v-else class="workspace">
      <GraphCanvas :entities="filteredEntities" :edges="filteredEdges"
                   :selected-entity-id="selectedEntity?.id"
                   @select-node="selectedEntity = $event; selectedEdge = undefined; evidenceId = ''"
                   @select-edge="selectEdge" />
      <aside>
        <EvidencePreview v-if="evidenceId" :evidence-id="evidenceId" close-label="关闭证据"
                         @close="evidenceId = ''" />
        <template v-else-if="selectedEdge">
          <h3>边</h3>
          <p><code>{{ selectedEdge.predicate }}</code> · {{ Math.round(selectedEdge.confidence * 100) }}%</p>
          <el-alert v-if="selectedEdge.unsupported" title="这条边没有可解析的 bbox 证据" type="error" :closable="false" />
          <el-button v-for="citation in selectedEdge.citations" :key="citation.evidence_id || citation.snippet"
                     class="citation" :disabled="!citation.resolved || !citation.evidence_id"
                     @click="evidenceId = citation.evidence_id || ''">
            第 {{ citation.page_idx + 1 }} 页 · {{ citation.snippet.slice(0, 80) }}
          </el-button>
        </template>
        <template v-else-if="selectedEntity">
          <h3>{{ selectedEntity.canonical_name }}</h3>
          <p>{{ selectedEntity.entity_type }} · {{ selectedEntity.merged_by }} 合并 · {{ Math.round(selectedEntity.merge_confidence * 100) }}%</p>
          <el-alert v-if="selectedEntity.entity_merge_uncertain" title="低置信实体合并，需人工确认或拆分" type="error" :closable="false" />
          <template v-if="selectedEntity.aliases.length">
            <el-select v-model="splitAlias" placeholder="选择要拆出的别名">
              <el-option v-for="alias in selectedEntity.aliases" :key="alias" :value="alias" />
            </el-select>
            <el-button :disabled="!splitAlias" @click="split">一键拆分</el-button>
          </template>
        </template>
        <p v-else class="hint">悬停节点查看邻域；点击节点看合并信息，点击边看 bbox 裁图。</p>
        <ReviewQueue />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
.page-head, .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
h2, h3, p { margin: 0; }
.page-head p, .hint { margin-top: 5px; color: var(--ddp-ink-3); }
.toolbar { padding-block: 10px; border-block: 1px solid var(--ddp-line); }
.toolbar label { display: grid; grid-template-columns: auto minmax(150px, 280px); align-items: center; gap: 12px; flex: 1; }
.toolbar .edge-picker { grid-template-columns: auto minmax(110px, 180px); flex: none; }
.edge-picker select { min-height: 36px; border: 1px solid var(--ddp-line-strong); background: var(--ddp-panel); color: var(--ddp-ink); }
.toolbar > span { color: var(--ddp-ink-3); font-family: var(--ddp-font-mono); font-size: 12px; }
.workspace { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 14px; min-height: 0; }
aside { display: grid; align-content: start; gap: 14px; min-width: 0; padding-left: 14px; border-left: 1px solid var(--ddp-line); }
aside code { font-family: var(--ddp-font-mono); }
.citation { width: 100%; height: auto; min-height: 44px; white-space: normal; justify-content: flex-start; text-align: left; }
@media (max-width: 1050px) { .workspace { grid-template-columns: 1fr; } aside { padding: 14px 0 0; border: 0; border-top: 1px solid var(--ddp-line); } }
</style>
