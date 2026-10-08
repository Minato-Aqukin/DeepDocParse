<script setup lang="ts">
import { isAxiosError } from 'axios'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { knowledgeApi, versionedWikiApi } from '@/api'
import type { WikiRevisionDocument } from '@/api'
import { tasksApi } from '@/api/tasks'
import { formatWikiLocator, resolveWikiEvidenceRef } from '@/api/wiki'
import type { WikiBuildBody, WikiDependency } from '@/api/wiki'
import { resourcesApi } from '@/api/resources'
import type { Resource } from '@/api/resources'
import type { ResourceContext } from '@/api/resource-context'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
import GraphCanvas from '@/components/knowledge/GraphCanvas.vue'
import type { EvidenceBacklink, KnowledgeGraph } from '@/types/api'
import { approvedPlanLabel, federationTaskNewLocation, isDesktop } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'
import { degradedLabelOf } from '@/constants/status'
import { wikiStaleReasonLabelOf } from '@deepdocparse/contracts'

/**
 * 版本化 Wiki 阅读器（`versionedWikiApi` = `/api/wikis` 的 `revision_out` 形状）。
 *
 * 旧 `/api/wiki` 实体页与版本化工作流是两套东西：这里只走版本化那套。
 * 生成句逐条可点回固定证据（evidence_ids），人工段落标 human/unsupported，
 * 按页 stale 与 revision 层 merge_conflicts 可见；写操作（创建/重建/人工编辑/
 * 发布）全部带 Idempotency-Key，409 `revision_conflict` 时提示重读，不静默覆盖。
 * 权限：非属主只能读已发布修订，未发布/来源变化的公开修订读到 404（不是空页）。
 *
 * 引用解析（联邦 closure）：claim/`dependency_manifest` 的 `evidence_id`
 * 可能是 `source:+…` qualified 引用。点击先按 `dependency_manifest` join，
 * 无 origin 的旧本地依赖保持既有本地行为；带 origin 的必须明确等于当前中心
 * 权威（`tasksApi.identity()` 的 `authority_node_id`）才用 `source_evidence_id`
 * 走本地证据/反链接口；跨源或权威未知一律在右侧展示固定出处，不发本地请求，
 * 更不向任意 origin 直发请求。原来源 ID 原样保留，不改写成当前中心。
 */
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
// 中心只读：构建/编辑按钮禁用并写明原因，另给「作为联邦任务发起」入口（plan §1.5）。
const desktop = isDesktop()
const readonlyHint = computed(() => (desktop && auth.readOnly ? approvedPlanLabel() : ''))
function proposeWikiAsTask(title: string) {
  // The question is sent verbatim to the center once approved: never prefill a
  // placeholder like "构建 Wiki" for a new Wiki — the user writes the real one.
  router.push(federationTaskNewLocation(title
    ? { query: `为 Wiki《${title}》生成新修订`, purpose: 'wiki', title }
    : { purpose: 'wiki' }))
}
const documents = ref<WikiRevisionDocument[]>([])
const document = ref<WikiRevisionDocument | null>(null)
const localGraph = ref<KnowledgeGraph>({ graph_version: 'ddp-graph/1', entities: [], edges: [] })
const evidenceId = ref('')
const evidenceContext = ref<ResourceContext>()
const backlinks = ref<EvidenceBacklink[]>([])
const foreignDep = ref<WikiDependency | null>(null)
const foreignRef = ref('')
const foreignReason = ref<'origin_mismatch' | 'authority_unknown' | ''>('')
const foreignCite = ref('')
const authorityNodeId = ref<string | null>(null)
const identityError = ref('')
const loading = ref(false)
const listLoading = ref(false)
let listReady = false
const errorText = ref('')
const filter = ref('')
const selectedWikiId = ref('')
const selectedPageKey = ref('')
const selectedRevision = ref('')
const editBase = ref('')
const editText = ref('')
const editing = ref(false)
let docGeneration = 0
let evidenceGeneration = 0
const builderOpen = ref(false)
const building = ref(false)
const buildError = ref('')
const buildTitle = ref('')
const buildWikiId = ref('')
const buildBase = ref('')
const buildSources = ref<string[]>([])
const buildPages = ref<number | undefined>(2)
const buildEvidence = ref<number | undefined>(50)
const buildTokens = ref<number | undefined>(2048)
const buildChars = ref<number | undefined>(12000)
const sourceResources = ref<Resource[]>([])
const sourceLoading = ref(false)
const sourceMore = ref({ mine: true, site_public: true })
let sourceOffset = 0
let sourceGeneration = 0
let buildAttempt: { body: string; key: string } | null = null
const sourceOptions = computed(() => sourceResources.value.flatMap(resource =>
  resource.versions.map(version => ({
    resourceId: resource.id, versionId: version.id,
    label: `${resource.display_name} · v${version.version_no} · ${version.id.slice(0, 8)}`,
    // 服务端只从版本当前索引背后的原始证据里选（wiki-format），没建好索引的版本必然被拒
    ready: resource.publication !== 'withdrawn'
      && version.parse_status === 'succeeded' && version.source_digest_verified === true
      && version.index_status === 'ready',
  }))))
const selectedSources = computed(() => sourceOptions.value.filter(option =>
  option.ready && buildSources.value.includes(option.versionId)))
const validBuild = computed(() => buildTitle.value.trim().length > 0
  && buildSources.value.length > 0 && buildSources.value.length <= 50
  && selectedSources.value.length === buildSources.value.length
  && [[buildPages.value, 1, 12], [buildEvidence.value, 1, 200],
    [buildTokens.value, 256, 16384], [buildChars.value, 1000, 200000]]
    .every(([value, min, max]) => typeof value === 'number' && Number.isInteger(value)
      && value >= min! && value <= max!))
const hasMoreSources = computed(() => sourceMore.value.mine || sourceMore.value.site_public)

async function loadSources(reset = false) {
  const mine = ++sourceGeneration
  if (reset) {
    sourceOffset = 0
    sourceResources.value = []
    sourceMore.value = { mine: true, site_public: true }
  }
  sourceLoading.value = true
  buildError.value = ''
  try {
    const scopes = (['mine', 'site_public'] as const).filter(scope => sourceMore.value[scope])
    const results = await Promise.all(scopes.map(async scope => ({
      scope, data: (await resourcesApi.list(scope, sourceOffset)).data,
    })))
    if (mine !== sourceGeneration) return
    const resources = new Map(sourceResources.value.map(resource => [resource.id, resource]))
    for (const { scope, data } of results) {
      for (const resource of data.items) resources.set(resource.id, resource)
      sourceMore.value[scope] = data.has_more
    }
    sourceResources.value = [...resources.values()]
    sourceOffset += 50
  } catch (cause) {
    if (mine === sourceGeneration) buildError.value = problem(cause, '来源列表读取失败')
  } finally {
    if (mine === sourceGeneration) sourceLoading.value = false
  }
}

/** 上一修订**生成时**选的固定来源。依赖清单里还有随人工段落带过来的旧版本依赖（发布要查），
 *  拿它预填会把用户已经换掉的旧版本又选回去（D 阶段浏览器实测：换成 v2 重建后，
 *  再点"生成新修订"预填出 v1 + v2）。老修订没有选证报告，才退回依赖清单。 */
function generationSources(revision: WikiRevisionDocument['revision']): string[] {
  const sources = (revision.limits.evidence_selection as { sources?: unknown } | undefined)?.sources
  const ids = Array.isArray(sources)
    ? sources.map((item) => (item as { source_version_id?: unknown }).source_version_id)
      .filter((id): id is string => typeof id === 'string')
    : revision.dependency_manifest.map((dep) => dep.source_version_id)
  return [...new Set(ids)]
}

async function openBuilder(rebuild = false) {
  if (!auth.canUpload || building.value || (rebuild && !document.value)) return
  buildWikiId.value = rebuild ? document.value!.wiki.id : ''
  buildBase.value = rebuild ? document.value!.revision.id : ''
  buildTitle.value = rebuild ? document.value!.wiki.title : ''
  buildSources.value = rebuild ? generationSources(document.value!.revision) : []
  buildAttempt = null
  buildError.value = ''
  builderOpen.value = true
  await loadSources(true)
}

async function submitBuild() {
  if (!validBuild.value || building.value) return
  const body: WikiBuildBody = {
    title: buildTitle.value.trim(),
    sources: selectedSources.value.map(source => ({
      resource_id: source.resourceId, source_version_id: source.versionId,
    })),
    max_pages: buildPages.value, max_evidence: buildEvidence.value,
    max_output_tokens: buildTokens.value, max_input_chars: buildChars.value,
  }
  const signature = JSON.stringify({ wiki: buildWikiId.value, base: buildBase.value, body })
  if (buildAttempt?.body !== signature) {
    buildAttempt = { body: signature, key: `wiki-build-${crypto.randomUUID()}` }
  }
  building.value = true
  buildError.value = ''
  try {
    const { data } = buildWikiId.value
      ? await versionedWikiApi.rebuild(buildWikiId.value,
        { ...body, base_revision_id: buildBase.value }, buildAttempt.key)
      : await versionedWikiApi.create(body, buildAttempt.key)
    docGeneration++
    evidenceGeneration++
    clearEvidence()
    document.value = data
    selectedWikiId.value = data.wiki.id
    selectedRevision.value = data.revision.id
    editBase.value = data.revision.id
    selectedPageKey.value = data.revision.pages[0]?.page_key ?? ''
    editText.value = ''
    documents.value = [data, ...documents.value.filter(item => item.wiki.id !== data.wiki.id)]
    builderOpen.value = false
    errorText.value = ''
    await router.push(wikiLocation(data.wiki.id, data.revision.id))
  } catch (cause) {
    buildError.value = problem(cause, 'Wiki 构建失败')
  } finally {
    building.value = false
  }
}

const revision = computed(() => document.value?.revision)
const pages = computed(() => revision.value?.pages ?? [])
const page = computed(() => pages.value.find((item) => item.page_key === selectedPageKey.value)
  ?? pages.value[0])
const staleReasons = computed(() => revision.value?.stale_reasons ?? {})
const staleLabels = computed(() => [...new Set(Object.values(staleReasons.value).flat())]
  .map((reason) => wikiStaleReasonLabelOf(reason) ?? reason))
/** 重建对话框里：已选的固定版本若不是该资源的最新版，就说出来 —— 过期提示之后，
 *  用户要自己把来源换成新版本（选择器刻意不自动替换，见契约）。 */
const newerVersionHints = computed(() => buildSources.value.flatMap((versionId) => {
  const resource = sourceResources.value.find((item) => item.versions.some((v) => v.id === versionId))
  const chosen = resource?.versions.find((v) => v.id === versionId)
  const newest = resource?.versions.reduce((a, b) => (b.version_no > a.version_no ? b : a))
  return resource && chosen && newest && newest.version_no > chosen.version_no
    ? [`${resource.display_name} 已有更新的 v${newest.version_no}（当前选的是 v${chosen.version_no}）`]
    : []
}))
const conflicts = computed(() => revision.value?.merge_conflicts ?? [])
const oldRevision = computed(() => revision.value
  && document.value
  && revision.value.id !== document.value.wiki.current_revision_id)
const evidenceSelection = computed(() => {
  const selection = revision.value?.limits.evidence_selection
  if (!selection || typeof selection !== 'object'
    || !('selected_evidence' in selection) || typeof selection.selected_evidence !== 'number'
    || !('total_original_evidence' in selection) || typeof selection.total_original_evidence !== 'number'
    || !('complete' in selection) || typeof selection.complete !== 'boolean') return null
  const degraded = 'ranking_degraded' in selection && typeof selection.ranking_degraded === 'string'
    ? selection.ranking_degraded : null
  return { selected: selection.selected_evidence, total: selection.total_original_evidence,
    complete: selection.complete, degradedLabel: degraded && (degradedLabelOf(degraded) ?? degraded) }
})

const filtered = computed(() => documents.value.filter((item) =>
  item.wiki.title.toLowerCase().includes(filter.value.toLowerCase())))

function problem(cause: unknown, fallback: string): string {
  const response = isAxiosError(cause) ? cause.response : undefined
  const data = response?.data
  const detail = data && typeof data === 'object' && 'error' in data ? data.error : null
  const code = detail && typeof detail === 'object' && 'code' in detail
    && typeof detail.code === 'string' ? detail.code : ''
  const message = detail && typeof detail === 'object' && 'message' in detail
    && typeof detail.message === 'string' ? detail.message : ''
  if (response?.status === 409 && code === 'revision_conflict') {
    return `${fallback}：修订冲突（409 revision_conflict），请重读当前修订后再写。`
  }
  if (response?.status === 404) return `${fallback}：Wiki 或修订不可用（404，可能未发布或无权限）。`
  if (code) return `${fallback}：${code}${message ? `（${message}）` : ''}`
  return `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}
const pageDeps = computed(() => (revision.value?.dependency_manifest ?? [])
  .filter((d) => d.page_key === page.value?.page_key))
const pageRelations = computed(() => (revision.value?.relations ?? [])
  .filter((r) => (r.subject_id ?? '') === page.value?.page_key || (r.object_id ?? '') === page.value?.page_key
    || !(r.subject_id ?? r.object_id)))
function clearEvidence(mine?: number) {
  if (mine !== undefined && mine !== evidenceGeneration) return
  evidenceId.value = ''
  evidenceContext.value = undefined
  backlinks.value = []
  foreignDep.value = null
  foreignRef.value = ''
  foreignReason.value = ''
  foreignCite.value = ''
}

const foreignPageStale = computed(() => {
  const key = foreignDep.value?.page_key
  if (!key) return false
  return pages.value.find((item) => item.page_key === key)?.stale ?? false
})

function closeEvidence() {
  evidenceGeneration++
  clearEvidence()
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

function wikiLocation(wikiId: string, revisionId?: string) {
  return { path: '/wiki', query: { wiki_id: wikiId, ...(revisionId ? { revision_id: revisionId } : {}) } }
}

async function loadLocation() {
  const wikiId = typeof route.query.wiki_id === 'string' ? route.query.wiki_id : documents.value[0]?.wiki.id
  const revisionId = typeof route.query.revision_id === 'string' ? route.query.revision_id : undefined
  if (!wikiId) return
  if (document.value?.wiki.id === wikiId && document.value.revision.id === revisionId) return
  await selectWiki(wikiId, revisionId)
}

async function loadList() {
  listLoading.value = true
  errorText.value = ''
  try {
    await loadAuthority()
    documents.value = (await versionedWikiApi.list()).data
    listReady = true
    await loadLocation()
  } catch (cause) {
    // 旧实体 Wiki（`/api/wiki`）是另一套阅读器：版本化列表不可用时只说清原因，
    // 不把 404 伪装成空列表（门禁五：后端挂掉必须说出原因）。
    errorText.value = problem(cause, 'Wiki 加载失败')
  } finally {
    listLoading.value = false
  }
}

async function selectWiki(wikiId: string, revisionId?: string) {
  selectedWikiId.value = wikiId
  document.value = null
  editText.value = ''
  const mine = ++docGeneration
  evidenceGeneration++
  clearEvidence(evidenceGeneration)
  loading.value = true
  errorText.value = ''
  try {
    const { data } = revisionId
      ? await versionedWikiApi.readRevision(wikiId, revisionId)
      : await versionedWikiApi.read(wikiId)
    if (mine !== docGeneration) return
    document.value = data
    selectedRevision.value = data.revision.id
    editBase.value = data.revision.id
    if (!data.revision.pages.some((item) => item.page_key === selectedPageKey.value)) {
      selectedPageKey.value = data.revision.pages[0]?.page_key ?? ''
    }
  } catch (cause) {
    if (mine !== docGeneration) return
    errorText.value = problem(cause, 'Wiki 修订加载失败')
  } finally {
    if (mine === docGeneration) loading.value = false
  }
}

async function selectEvidence(evidence_id: string, cite = '') {
  const mine = ++evidenceGeneration
  clearEvidence(mine)
  if (!evidence_id) return
  const resolved = resolveWikiEvidenceRef(
    revision.value?.dependency_manifest, evidence_id, authorityNodeId.value)
  if (resolved.kind === 'foreign') {
    // 跨源或权威未知：只展示固定出处与已持有摘录依据，不发本地证据/反链请求，
    // 更不向任意 origin 直发请求。
    foreignDep.value = resolved.dependency
    foreignRef.value = evidence_id
    foreignReason.value = resolved.reason
    foreignCite.value = cite
    return
  }
  if (resolved.kind === 'missing') {
    errorText.value =
      `引用 ${evidence_id} 在当前修订的固定来源依赖里没有登记，不做本地打开。请核对修订是否已更新。`
    return
  }
  evidenceContext.value = resolved.dependency ? {
    resource_id: resolved.dependency.resource_id,
    version_id: resolved.dependency.source_version_id,
  } : undefined
  evidenceId.value = resolved.evidenceId
  try {
    const fetched = (await knowledgeApi.backlinks(resolved.evidenceId, evidenceContext.value)).data.backlinks
    // 晚到响应直接丢弃：不碰当前已选证据的任何状态。
    if (mine !== evidenceGeneration) return
    backlinks.value = fetched
  } catch (cause) {
    if (mine !== evidenceGeneration) return
    // 反链失败不撤掉已打开的本地预览（旧行为）：只报错，bbox 预览照常可用。
    errorText.value = problem(cause, '反链加载失败')
  }
}

async function saveHuman() {
  if (!auth.canUpload || oldRevision.value || !document.value || !page.value || !editText.value.trim() || editing.value) return
  const mine = docGeneration
  editing.value = true
  errorText.value = ''
  try {
    const key = `wikiedit-${document.value.wiki.id}-${page.value.page_key}-${crypto.randomUUID()}`
    const paragraphs = [...page.value.human_paragraphs, { id: crypto.randomUUID(), text: editText.value.trim() }]
    const { data } = await versionedWikiApi.editPage(
      document.value.wiki.id, page.value.page_key,
      { base_revision_id: editBase.value, paragraphs }, key)
    if (mine !== docGeneration) return
    document.value = data
    selectedRevision.value = data.revision.id
    editBase.value = data.revision.id
    editText.value = ''
    documents.value = [data, ...documents.value.filter(item => item.wiki.id !== data.wiki.id)]
    await router.push(wikiLocation(data.wiki.id, data.revision.id))
  } catch (cause) {
    if (mine !== docGeneration) return
    errorText.value = problem(cause, '人工编辑保存失败')
  } finally {
    editing.value = false
  }
}

watch(() => [route.query.wiki_id, route.query.revision_id], () => {
  if (listReady) void loadLocation()
})

onBeforeUnmount(() => { docGeneration++; evidenceGeneration++; sourceGeneration++ })
onMounted(loadList)
</script>

<template>
  <div class="wiki" v-loading="loading || listLoading">
    <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" />
    <aside class="tree">
      <h2>知识 Wiki</h2>
      <el-button v-if="auth.canUpload" :disabled="building" @click="openBuilder()">构建 Wiki</el-button>
      <template v-else-if="readonlyHint">
        <el-tooltip :content="readonlyHint"><span><el-button disabled>构建 Wiki</el-button></span></el-tooltip>
        <el-button link @click="proposeWikiAsTask('')">作为联邦任务发起</el-button>
      </template>
      <el-input v-model="filter" placeholder="筛选标题" clearable />
      <el-empty v-if="!filtered.length && !loading && !listLoading && !errorText" description="尚无版本化 Wiki" :image-size="72" />
      <button v-for="item in filtered" :key="item.wiki.id" type="button"
              :class="{ active: item.wiki.id === selectedWikiId }" @click="router.push(wikiLocation(item.wiki.id))">
        {{ item.wiki.title }}
      </button>
    </aside>

    <main>
      <template v-if="document">
        <header>
          <h1>{{ document.wiki.title }}</h1>
          <p>修订 <span class="ddp-mono">{{ revision?.id }}</span> · 当前 <span class="ddp-mono">{{ document.wiki.current_revision_id }}</span></p>
          <nav class="revision-nav" aria-label="Wiki 修订">
            <RouterLink :to="wikiLocation(document.wiki.id, revision?.id)">本修订固定链接</RouterLink>
            <RouterLink v-if="revision?.base_revision_id"
                        :to="wikiLocation(document.wiki.id, revision.base_revision_id)">上一修订</RouterLink>
            <RouterLink v-if="oldRevision" :to="wikiLocation(document.wiki.id)">查看当前修订</RouterLink>
          </nav>
          <p v-if="evidenceSelection" role="status">
            本次采用 {{ evidenceSelection.selected }} / {{ evidenceSelection.total }} 条原始证据。
            <span v-if="!evidenceSelection.complete">这是预算内选取的部分证据，不代表已覆盖全部原文。</span>
          </p>
          <p v-if="evidenceSelection?.degradedLabel" class="ddp-degraded" role="status">
            选证排序降级：{{ evidenceSelection.degradedLabel }}
          </p>
          <el-button v-if="auth.canUpload" :disabled="!!oldRevision || building"
                     @click="openBuilder(true)">选择来源并生成新修订</el-button>
          <template v-else-if="readonlyHint">
            <el-tooltip :content="readonlyHint"><span><el-button disabled>选择来源并生成新修订</el-button></span></el-tooltip>
            <el-button link @click="proposeWikiAsTask(document?.wiki.title ?? '')">作为联邦任务发起</el-button>
          </template>
          <p v-if="oldRevision" class="ddp-degraded" role="status">正在查看历史修订；写入需基于当前修订，旧基准会被拒绝（409 revision_conflict）。</p>
          <p v-if="revision?.stale" class="ddp-degraded" role="status">
            部分来源已经变化或不可用；这些页面需要重新核对（{{ staleLabels.join('、') }}）。
          </p>
          <p v-if="conflicts.length" class="ddp-degraded" role="status">新旧页面存在合并冲突（{{ conflicts.length }}），请保留人工内容并逐项复核。</p>
        </header>
        <nav class="page-tabs" aria-label="Wiki 页面">
          <button v-for="item in pages" :key="item.page_key"
                  :class="{ active: (page?.page_key ?? '') === item.page_key }"
                  @click="selectedPageKey = item.page_key">
            {{ item.title }}{{ item.stale ? ' · 待更新' : '' }}
          </button>
        </nav>
        <template v-if="page">
          <section v-for="(part, i) in page.generated_sections" :key="i" class="section">
            <h2>{{ part.heading }}</h2>
            <!-- 每个 evidence_id 一个按钮（FederatedWikiPanel 同款）：整句只有一个
              click 位时第 2..N 条引用点不开，只能看不能核。 -->
            <div v-for="sentence in part.sentences" :key="sentence.id"
                 class="sentence" :class="{ unsupported: sentence.unsupported }">
              <span>{{ sentence.text }}</span>
              <span class="cites">
                <button v-for="(id, n) in sentence.evidence_ids" :key="id" type="button" class="cite"
                        @click="selectEvidence(id, sentence.text)">
                  出处 {{ n + 1 }}
                </button>
              </span>
              <small v-if="sentence.unsupported">unsupported · 无法指回 bbox</small>
              <small v-else>引用 {{ sentence.evidence_ids.length }} 条</small>
              <small v-if="sentence.conflict_group" class="conflict">冲突组 {{ sentence.conflict_group }} · 与同组说法并列</small>
            </div>
          </section>
          <section class="section">
            <h2>人工补充 · 未核证</h2>
            <p v-for="item in page.human_paragraphs" :key="item.id">{{ item.text }}</p>
            <template v-if="auth.canUpload">
              <label :for="`human-${page.page_key}`">新增人工段落（只写 human_paragraphs，不改生成文本）</label>
              <textarea :id="`human-${page.page_key}`" v-model="editText" maxlength="10000" rows="4" />
              <el-button :disabled="!editText.trim() || editing || !!oldRevision" :loading="editing" @click="saveHuman">保存人工编辑为新修订</el-button>
            </template>
            <template v-else-if="readonlyHint">
              <el-tooltip :content="readonlyHint"><span><el-button disabled>保存人工编辑为新修订</el-button></span></el-tooltip>
              <el-button link @click="proposeWikiAsTask(document?.wiki.title ?? '')">作为联邦任务发起</el-button>
            </template>
          </section>
          <details>
            <summary>固定来源依赖（{{ revision?.dependency_manifest.length ?? 0 }}）</summary>
            <p v-for="dep in pageDeps" :key="dep.evidence_id" class="hint">
              <code class="ddp-mono">{{ dep.origin_node_id ?? '本地' }} / {{ dep.resource_id }} / {{ dep.source_version_id }} · {{ formatWikiLocator(dep.locator) }}</code>
              <el-button link @click="selectEvidence(dep.evidence_id)">核对原件</el-button>
            </p>
          </details>
          <section v-if="pageRelations.length" class="section relations">
            <h2>原文关系 · 待复核</h2>
            <article v-for="(relation, index) in pageRelations" :key="index" class="relation">
              <p>{{ relation.predicate }}</p>
              <p class="hint">
                <span v-if="relation.subject_id" class="ddp-mono">{{ relation.subject_id }}</span>
                <span v-if="relation.subject_id && relation.object_id"> → </span>
                <span v-if="relation.object_id" class="ddp-mono">{{ relation.object_id }}</span>
              </p>
              <p>
                <el-button v-for="id in relation.evidence_ids" :key="id" link @click="selectEvidence(id, relation.predicate)">关系出处</el-button>
              </p>
            </article>
          </section>
        </template>
      </template>
      <el-empty v-else description="从左侧选择 Wiki" />
    </main>

    <aside class="evidence">
      <p v-if="identityError" class="ddp-degraded" role="alert">{{ identityError }}</p>
      <EvidencePreview v-if="evidenceId" :key="evidenceId" :evidence-id="evidenceId" close-label="关闭证据"
                       :context="evidenceContext" @close="closeEvidence()" />
      <section v-if="evidenceId" class="backlinks">
        <h3>反链</h3>
        <p v-if="!backlinks.length">当前没有引用。</p>
        <article v-for="item in backlinks" :key="`${item.source_kind}:${item.revision_id ?? ''}:${item.source_id}`">
          <code>{{ item.source_kind }}</code><span>{{ item.label }}</span>
          <RouterLink v-if="item.wiki_id && item.revision_id" :to="wikiLocation(item.wiki_id, item.revision_id)">
            Wiki：{{ item.wiki_title }}（固定修订）
          </RouterLink>
        </article>
      </section>
      <section v-else-if="foreignDep" class="foreign" aria-label="跨源固定出处">
        <h3>跨源固定出处</h3>
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
        <el-button link @click="closeEvidence()">关闭</el-button>
      </section>
      <template v-else>
        <h3>局部图谱 · 1 跳</h3>
        <GraphCanvas :entities="localGraph.entities" :edges="localGraph.edges" :height="300"
                     :selected-entity-id="undefined" />
        <p class="hint">点击正文句子后，这里会切换到四层证据预览和完整反链。</p>
      </template>
    </aside>
    <el-dialog v-model="builderOpen" :title="buildWikiId ? '生成 Wiki 新修订' : '构建 Wiki'"
               width="680px" :close-on-click-modal="!building"
               :close-on-press-escape="!building" :show-close="!building">
      <el-alert v-if="buildError" :title="buildError" type="error" :closable="false" />
      <el-form label-position="top" :disabled="building">
        <el-form-item label="Wiki 主题">
          <el-input v-model="buildTitle" maxlength="255" placeholder="希望整理哪些知识？" />
        </el-form-item>
        <el-form-item label="固定来源版本">
          <p class="hint">选择自己的资料或本站公开资料。只使用勾选的固定版本，不自动替换为最新版。</p>
          <el-select v-model="buildSources" multiple filterable :multiple-limit="50"
                     class="source-picker" placeholder="选择已解析、已建索引、原件已验证的版本">
            <el-option v-for="source in sourceOptions" :key="source.versionId"
                       :value="source.versionId" :disabled="!source.ready"
                       :label="source.label + (source.ready ? '' : ' · 尚未完成解析、索引或原件校验')" />
          </el-select>
          <p v-if="buildSources.length !== selectedSources.length" class="hint" role="status">
            部分已选版本不在当前可用列表中。请加载更多来源，或移除不可用版本后重新选择。
          </p>
          <p v-for="hint in newerVersionHints" :key="hint" class="ddp-degraded" role="status">{{ hint }}</p>
          <el-button v-if="hasMoreSources" link :loading="sourceLoading" @click="loadSources()">
            加载更多来源
          </el-button>
          <el-button v-else link :loading="sourceLoading" @click="loadSources(true)">刷新来源</el-button>
        </el-form-item>
        <div class="build-limits">
          <el-form-item label="最多页面">
            <el-input-number v-model="buildPages" :min="1" :max="12" :precision="0" />
          </el-form-item>
          <el-form-item label="最多原始证据">
            <el-input-number v-model="buildEvidence" :min="1" :max="200" :precision="0" />
          </el-form-item>
          <el-form-item label="总输出 token 上限">
            <el-input-number v-model="buildTokens" :min="256" :max="16384" :step="256" :precision="0" />
          </el-form-item>
          <el-form-item label="输入字符上限">
            <el-input-number v-model="buildChars" :min="1000" :max="200000" :step="1000" :precision="0" />
          </el-form-item>
        </div>
        <p class="hint">生成内容仍需核对。预算限制不等于全文已覆盖；模型上下文不足时会明确失败。</p>
        <p v-if="buildWikiId" class="hint">基于当前修订生成，保留人工补充；并发修改或合并冲突不会被静默覆盖。</p>
      </el-form>
      <template #footer>
        <span v-if="building" role="status">正在基于固定来源生成，请勿重复提交…</span>
        <el-button :disabled="building" @click="builderOpen = false">取消</el-button>
        <el-button :disabled="!validBuild || sourceLoading" :loading="building"
                   @click="submitBuild">{{ buildWikiId ? '生成新修订' : '开始构建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.wiki { display: grid; grid-template-columns: 220px minmax(360px, 1fr) minmax(320px, 420px); gap: 0; min-height: calc(100vh - 120px); background: var(--ddp-panel); border: 1px solid var(--ddp-line); }
.wiki > .el-alert { grid-column: 1 / -1; }
.tree, .evidence { padding: 16px; min-width: 0; overflow: auto; }
.tree { border-right: 1px solid var(--ddp-line); }
.evidence { border-left: 1px solid var(--ddp-line); }
.source-picker { width: 100%; }
.build-limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 20px; }
.tree h2, main h1, main h2, h3, p { margin: 0; }
.tree > section { margin-top: 18px; }
.tree h3 { margin-bottom: 6px; color: var(--ddp-ink-3); font-size: 12px; text-transform: uppercase; }
.tree button { display: block; width: 100%; min-height: 40px; padding: 7px 9px; border: 0; border-left: 2px solid transparent; background: transparent; color: var(--ddp-ink-2); text-align: left; cursor: pointer; }
.tree button:hover, .tree button.active { background: color-mix(in srgb, var(--ddp-ink) 6%, transparent); color: var(--ddp-ink); }
.tree button.active { border-left-color: var(--ddp-ink); font-weight: 600; }
main { padding: 28px 34px; overflow: auto; }
main header { padding-bottom: 20px; border-bottom: 1px solid var(--ddp-line); }
main header p, .hint { margin-top: 6px; color: var(--ddp-ink-3); }
.revision-nav { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 8px; }
.revision-nav a { color: var(--ddp-ink-2); text-underline-offset: 3px; }
.section { margin-top: 28px; }
.section > h2 { margin-bottom: 12px; font-size: 18px; }
.sentence { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 14px; width: 100%; min-height: 44px; padding: 9px 10px; border: 0; border-left: 2px solid var(--ddp-cite); background: transparent; color: var(--ddp-ink); text-align: left; }
.sentence.unsupported { border-left-color: var(--ddp-danger); }
.sentence small { color: var(--ddp-cite); white-space: nowrap; }
.sentence.unsupported small { color: var(--ddp-danger); }
/* 句内逐条出处按钮：出处红只许出现在这类元素上（准则一）。 */
.sentence .cites { display: inline-flex; flex-wrap: wrap; gap: 2px 8px; }
.sentence .cite { border: 0; background: transparent; padding: 0; color: var(--ddp-cite); font-size: 12px; white-space: nowrap; cursor: pointer; }
.sentence .cite:hover { text-decoration: underline; }
.evidence { display: grid; align-content: start; gap: 14px; }
.foreign { display: grid; align-content: start; gap: 12px; }
.foreign .facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; margin: 0; }
.foreign .facts dt { color: var(--ddp-ink-3); font-size: 12px; }
.foreign .facts dd { margin: 0; font-size: 12.5px; overflow-wrap: anywhere; }
.relations .relation { padding: 8px 0; border-bottom: 1px solid var(--ddp-line); }
.backlinks { display: grid; gap: 8px; padding-top: 14px; border-top: 1px solid var(--ddp-line); }
.backlinks article { display: grid; gap: 3px; padding: 7px 0; border-bottom: 1px solid var(--ddp-line); }
.backlinks code { color: var(--ddp-ink-3); font-family: var(--ddp-font-mono); font-size: 11px; }
@media (max-width: 1200px) { .wiki { grid-template-columns: 200px 1fr; } .evidence { grid-column: 1 / -1; border: 0; border-top: 1px solid var(--ddp-line); } }
@media (max-width: 760px) { .wiki { grid-template-columns: 1fr; } .tree { border: 0; border-bottom: 1px solid var(--ddp-line); } main { padding: 20px 16px; } }
</style>
