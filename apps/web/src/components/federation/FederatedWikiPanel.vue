<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { tasksApi } from '@/api/tasks'
import {
  formatWikiLocator,
  resolveWikiEvidenceRef,
  versionedWikiApi,
  type WikiDependency,
  type WikiRevisionDocument,
} from '@/api/wiki'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
import type { FederatedWikiRef } from '@/federation/task-model'

/**
 * 联邦 Wiki 结果阅读器：只渲染落库后的 `revision_out`（= `versionedWikiApi` 的
 * `WikiRevisionDocument` 形状）。`generate_federated` 的 draft 是内部中间态，
 * 不直接给前端 —— TaskStatus.result 里如带 `wiki`，只认 `{ wiki_id, revision_id }`
 * 引用并经 `versionedWikiApi` 读取同一形状后渲染。
 *
 * 生成句逐条可点回固定证据，人工段落标 human/unsupported，`merge_conflicts`
 * 与按页 `stale` 可见；409 `revision_conflict` 时提示重读，不静默覆盖。
 * 引用缺失/404 时明确说“产物尚未落库或不可见”，不渲染旧草稿形状。
 *
 * 引用解析（联邦 closure）：claim/relation/`dependency_manifest` 的 `evidence_id`
 * 可能是 `source:+…` qualified 引用，绝不能直接送本地证据接口。点击先按
 * `dependency_manifest` join，无 origin 的旧本地依赖保持既有本地行为；带 origin
 * 的必须明确等于当前中心权威（`tasksApi.identity()` 的 `authority_node_id`，
 * 与任务结果页 `coordinatorNodeId` 同一中心身份）才用 `source_evidence_id`
 * 打开本地原文；跨源或权威未知一律在抽屉里展示固定出处，不发本地请求，
 * 更不向任意 origin 直发请求。原来源 ID 原样保留，不改写成当前中心。
 */
const props = defineProps<{ wiki?: FederatedWikiRef | null }>()

const wikiId = ref('')
const revisionInput = ref('')
const document = ref<WikiRevisionDocument | null>(null)
const loading = ref(false)
const error = ref('')
const previewId = ref('')
const foreignDep = ref<WikiDependency | null>(null)
const foreignRef = ref('')
const foreignReason = ref<'origin_mismatch' | 'authority_unknown' | ''>('')
const foreignCite = ref('')
const authorityNodeId = ref<string | null>(null)
const identityError = ref('')
let docGeneration = 0
let evidenceGeneration = 0

const revision = computed(() => document.value?.revision)
const pages = computed(() => revision.value?.pages ?? [])
const foreignPageStale = computed(() => {
  const key = foreignDep.value?.page_key
  if (!key) return false
  return pages.value.find((page) => page.page_key === key)?.stale ?? false
})

function problem(cause: unknown, fallback: string): string {
  const response = (cause as { response?: { status?: number; data?: { error?: { code?: string; message?: string } } } })?.response
  if (response?.status === 409) return `${fallback}：修订冲突（409 revision_conflict），请重读当前修订后再写。`
  if (response?.status === 404) return `${fallback}：Wiki 或修订不可用（404）。`
  const detail = response?.data?.error
  if (detail?.code) return `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
  return `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

function clearEvidence() {
  previewId.value = ''
  foreignDep.value = null
  foreignRef.value = ''
  foreignReason.value = ''
  foreignCite.value = ''
}

function closePreview() {
  evidenceGeneration++
  clearEvidence()
}

function depsFor(pageKey: string): WikiDependency[] {
  return revision.value?.dependency_manifest.filter((dep) => dep.page_key === pageKey) ?? []
}

function relationsFor(pageKey: string) {
  return (revision.value?.relations ?? []).filter((relation) =>
    (relation.subject_id ?? '') === pageKey || (relation.object_id ?? '') === pageKey
    || !(relation.subject_id ?? relation.object_id))
}

/** 当前中心权威身份：已有 `tasks.ts#identity`（`/api/v1/capabilities`）的握手，不另起名目。 */
async function loadAuthority() {
  try {
    const { data } = await tasksApi.identity()
    authorityNodeId.value = data.identity.authority_node_id
    identityError.value = ''
  } catch (cause) {
    authorityNodeId.value = null
    identityError.value = problem(cause, '当前中心身份读取失败，跨源引用一律按固定出处展示')
  }
}

async function open(wikiRef?: string, revisionRef?: string) {
  const id = (wikiRef ?? wikiId.value).trim()
  if (!id) return
  const mine = ++docGeneration
  evidenceGeneration++
  clearEvidence()
  loading.value = true
  error.value = ''
  try {
    const revisionId = (revisionRef ?? revisionInput.value).trim()
    const { data } = revisionId
      ? await versionedWikiApi.readRevision(id, revisionId)
      : await versionedWikiApi.read(id)
    if (mine !== docGeneration) return
    document.value = data
  } catch (cause) {
    if (mine !== docGeneration) return
    document.value = null
    error.value = problem(cause, 'Wiki 读取失败')
  } finally {
    if (mine === docGeneration) loading.value = false
  }
}

function selectEvidence(refId: string, cite = '') {
  evidenceGeneration++
  clearEvidence()
  if (!refId) return
  const resolved = resolveWikiEvidenceRef(
    revision.value?.dependency_manifest, refId, authorityNodeId.value)
  if (resolved.kind === 'foreign') {
    // 跨源或权威未知：只展示固定出处与已持有内容，不发本地证据请求，
    // 更不向任意 origin 直发请求。
    foreignDep.value = resolved.dependency
    foreignRef.value = refId
    foreignReason.value = resolved.reason
    foreignCite.value = cite
    return
  }
  if (resolved.kind === 'missing') {
    error.value =
      `引用 ${refId} 在当前修订的固定来源依赖里没有登记，不做本地打开。请核对修订是否已更新。`
    return
  }
  previewId.value = resolved.evidenceId
}

onMounted(loadAuthority)
onBeforeUnmount(() => { docGeneration++; evidenceGeneration++ })

watch(() => props.wiki, (ref) => {
  if (ref?.wiki_id) void open(ref.wiki_id, ref.revision_id)
  else { docGeneration++; evidenceGeneration++; document.value = null; error.value = ''; clearEvidence() }
}, { immediate: true })
</script>
<template>
  <section class="federated-wiki" aria-label="联邦 Wiki 结果">
    <div class="open">
      <h3>已落库版本</h3>
      <p class="hint">Wiki 产物落库后按 `revision_out` 形状读：生成句点回固定证据，人工段落与合并冲突逐项可见。</p>
      <p v-if="identityError" class="ddp-degraded" role="status">{{ identityError }}</p>
      <p v-if="!props.wiki" class="hint">任务结果里没有 Wiki 引用（wiki_id/revision_id）：产物尚未落库或不可见，不渲染草稿。</p>
      <p v-else class="meta">引用 Wiki <span class="ddp-mono">{{ props.wiki.wiki_id }}</span> · 修订 <span class="ddp-mono">{{ props.wiki.revision_id }}</span></p>
      <div class="actions">
        <el-input v-model="wikiId" placeholder="wiki_id" class="id-input" />
        <el-input v-model="revisionInput" placeholder="revision_id（可选，空=当前修订）" class="id-input" />
        <el-button :loading="loading" @click="open()">打开固定版本</el-button>
      </div>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <template v-if="document">
        <p class="meta">
          {{ document.wiki.title }} · 修订 <span class="ddp-mono">{{ revision?.id }}</span>
          <span v-if="revision?.stale" class="error">部分来源已变化，需要重新核对。</span>
        </p>
        <p v-if="(revision?.merge_conflicts ?? []).length" class="error">
          新旧页面存在合并冲突，请保留人工内容并逐项复核。
        </p>
        <article v-for="page in pages" :key="page.page_key" class="page">
          <h3>{{ page.title }}<span v-if="page.stale" class="error"> · 待更新</span></h3>
          <section v-for="(part, i) in page.generated_sections" :key="i">
            <h4>{{ part.heading }}</h4>
            <p v-for="sentence in part.sentences" :key="sentence.id" class="sentence">
              {{ sentence.text }}
              <button v-for="id in sentence.evidence_ids" :key="id" type="button" class="cite"
                      @click="selectEvidence(id, sentence.text)">
                固定原文
              </button>
              <small v-if="sentence.unsupported">无有效原始出处 · 待复核</small>
            </p>
          </section>
          <section v-if="page.human_paragraphs.length" class="human">
            <h4>人工补充 · 未核证</h4>
            <p v-for="item in page.human_paragraphs" :key="item.id">{{ item.text }}</p>
          </section>
          <section v-if="relationsFor(page.page_key).length" class="relations">
            <h4>原文关系 · 待复核</h4>
            <p v-for="(relation, index) in relationsFor(page.page_key)" :key="index" class="sentence relation">
              {{ relation.predicate }}
              <button v-for="id in relation.evidence_ids" :key="id" type="button" class="cite"
                      @click="selectEvidence(id, relation.predicate)">
                关系出处
              </button>
            </p>
          </section>
          <details>
            <summary>固定来源依赖（{{ depsFor(page.page_key).length }}）</summary>
            <p v-for="dep in depsFor(page.page_key)" :key="dep.evidence_id" class="hint">
              <code class="ddp-mono">{{ dep.origin_node_id ?? '本地' }} / {{ dep.resource_id }} / {{ dep.source_version_id }} · {{ formatWikiLocator(dep.locator) }}</code>
              <button type="button" class="cite" @click="selectEvidence(dep.evidence_id)">核对原件</button>
            </p>
          </details>
        </article>
      </template>
    </div>

    <el-drawer :model-value="!!previewId || !!foreignDep" size="min(560px, 100vw)" title="固定原文"
               @close="closePreview()">
      <EvidencePreview v-if="previewId" :key="previewId" :evidence-id="previewId" close-label="关闭" @close="closePreview()" />
      <section v-else-if="foreignDep" class="foreign" aria-label="跨源固定出处">
        <p v-if="foreignReason === 'authority_unknown'" class="ddp-degraded" role="status">
          当前中心身份未知，无法判断这条引用是否在本中心；只展示固定出处，不做本地打开，也没有向来源节点发请求。
        </p>
        <p v-else class="ddp-degraded" role="status">
          该引用来自其他节点；这里只展示修订时保存的来源信息，未读取原文，也没有向来源节点发请求。
        </p>
        <p v-if="foreignCite" class="cite-text">引用该出处的内容：{{ foreignCite }}</p>
        <dl class="facts">
          <dt>引用</dt><dd class="ddp-mono">{{ foreignRef }}</dd>
          <dt>原来源证据</dt><dd class="ddp-mono">{{ foreignDep.source_evidence_id ?? '未提供' }}</dd>
          <dt>来源节点</dt><dd class="ddp-mono">{{ foreignDep.origin_node_id }}</dd>
          <dt>权威节点</dt><dd class="ddp-mono">{{ foreignDep.authority_node_id ?? '—' }}</dd>
          <dt>资源</dt><dd class="ddp-mono">{{ foreignDep.resource_id }}</dd>
          <dt>来源版本</dt><dd class="ddp-mono">{{ foreignDep.source_version_id }}</dd>
          <dt>解析修订</dt><dd class="ddp-mono">{{ foreignDep.parse_revision }}</dd>
          <dt>定位</dt><dd>{{ formatWikiLocator(foreignDep.locator) }}</dd>
          <dt>策略修订</dt><dd class="ddp-mono">{{ foreignDep.policy_revision ?? '—' }}</dd>
          <dt>发布</dt><dd class="ddp-mono">{{ foreignDep.source_publication ?? '—' }}</dd>
          <dt>回执</dt><dd class="ddp-mono">{{ foreignDep.retrieval_receipt_ref ?? '—' }}</dd>
          <dt>摘录摘要</dt><dd class="ddp-mono">{{ foreignDep.excerpt_digest }}</dd>
          <dt>复核状态</dt><dd>{{ foreignPageStale ? '来源已变化：该页待更新与复核。' : '尚未读取原文；固定定位与摘要不等于内容已核验。' }}</dd>
        </dl>
        <el-button link @click="closePreview()">关闭</el-button>
      </section>
    </el-drawer>
  </section>
</template>

<style scoped>
.federated-wiki { display: grid; gap: 16px; }
.meta, .hint { margin: 0; color: var(--ddp-ink-2); font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.page { padding-top: 12px; border-top: var(--ddp-bw) solid var(--ddp-line); }
h3 { font-size: 16px; margin: 0 0 8px; }
h4 { font-size: 13px; margin: 12px 0 6px; }
.sentence { line-height: 1.8; }
.sentence.relation { display: grid; gap: 4px; }
.human { border-top: var(--ddp-bw) solid var(--ddp-line); margin-top: 12px; }
.relations { border-top: var(--ddp-bw) solid var(--ddp-line); margin-top: 12px; }
.foreign { display: grid; gap: 12px; }
.foreign .facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; margin: 0; }
.foreign .facts dt { color: var(--ddp-ink-3); font-size: 12px; }
.foreign .facts dd { margin: 0; font-size: 12.5px; overflow-wrap: anywhere; }
.foreign .cite-text { margin: 0; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.cite { background: transparent; border: 0; padding: 4px 12px 4px 0; color: var(--ddp-cite); cursor: pointer; }
.open { display: grid; gap: 12px; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
.id-input { max-width: 320px; }
details { border-top: var(--ddp-bw) solid var(--ddp-line); padding-top: 12px; }
summary { cursor: pointer; font-size: 12px; }
small { font-size: 12px; color: var(--ddp-ink-3); }
</style>
