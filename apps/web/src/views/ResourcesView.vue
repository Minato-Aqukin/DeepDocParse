<script setup lang="ts">
import {
  INDEX_STATUS_META, PARSE_STATUS_META, bundleReplicaAvailabilityLabelOf, indexStatusLabelOf, parseStatusLabelOf,
} from '@deepdocparse/contracts'
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { downloadAs } from '@/api/http'
import { resourcesApi } from '@/api/resources'
import type { BundleEvidence, BundleReplica, Resource, ResourceVersion } from '@/api/resources'
import UploadDialog from '@/components/document/UploadDialog.vue'
import { usePolling } from '@/composables/usePolling'
import { approvedPlanLabel, federationTaskNewLocation, isDesktop, onLocalSource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
// 中心只读：按钮留在界面上但禁用，并写明原因（plan §1.5）。
const desktop = isDesktop()
const readonlyHint = computed(() => (desktop && auth.readOnly ? approvedPlanLabel() : ''))
function proposeUploadAsTask() {
  router.push(federationTaskNewLocation('上传资料到中心'))
}
const scope = ref<'mine' | 'site_public'>('mine')
const resources = ref<Resource[]>([])
const loading = ref(false)
const error = ref('')
const offset = ref(0)
const more = ref(false)
const upload = ref(false)
let generation = 0
const labels = { private: '私有', draft: '草稿', published: '已公开', withdrawn: '已撤下' }
const uploadTarget = ref<Resource | null>(null)

function openUpload(resource: Resource | null = null) {
  uploadTarget.value = resource
  upload.value = true
}

/** quiet：轮询刷新不闪「正在读取」，也不清掉已有列表 */
async function load(quiet = false) {
  const current = ++generation
  if (!quiet) loading.value = true
  error.value = ''
  try {
    const { data } = await resourcesApi.list(scope.value, offset.value)
    if (current !== generation) return
    if (!Array.isArray(data.items) || typeof data.has_more !== 'boolean') {
      throw new Error('中心返回的资源列表格式不兼容')
    }
    resources.value = data.items
    more.value = data.has_more
  } catch (cause) {
    if (current === generation) error.value = `资源加载失败：${String(cause)}`
  } finally { if (current === generation) loading.value = false }
  if (hasActive.value && !polling.running.value) polling.start()
}

/**
 * 版本的解析/索引还在动时自己刷新：上传对话框只管到「已登记」，
 * 之后的解析与索引要在这里看得见，而不是让人以为登记完就能检索了。
 */
const hasActive = computed(() => resources.value.some((resource) => resource.versions.some((version) =>
  (version.parse_status && PARSE_STATUS_META[version.parse_status]?.active)
  || (version.index_status && INDEX_STATUS_META[version.index_status]?.active))))
const polling = usePolling(() => load(true), () => hasActive.value)
function open(resource: Resource, version: ResourceVersion) {
  router.push({ name: 'workbench', params: { id: version.document_id }, query: {
    resource_id: resource.id, version_id: version.id,
    ...(version.parse_job_id ? { job: version.parse_job_id } : {}),
  } })
}
async function publication(resource: Resource) {
  const value = resource.publication === 'published' ? 'private' : 'published'
  try {
    await ElMessageBox.confirm(value === 'published'
      ? `公开「${resource.display_name}」的原文和可用证据到本站公开库？`
      : `撤下「${resource.display_name}」？新的访问将重新检查权限。`, '资源公开范围')
  } catch { return }
  try { await resourcesApi.publish(resource.id, value); await load() }
  catch (cause) { error.value = `公开范围更新失败：${String(cause)}` }
}
async function remove(resource: Resource) {
  try { await ElMessageBox.confirm(`删除你的资源「${resource.display_name}」及其版本？其他人的独立资源会保留。`, '删除资源') }
  catch { return }
  try { await resourcesApi.remove(resource.id); await load() }
  catch (cause) { error.value = `删除失败：${String(cause)}` }
}
async function bundle(resource: Resource, version: ResourceVersion) {
  try { await downloadAs(resourcesApi.bundleUrl(resource.id, version.id), `${resource.display_name}.ddp.zip`) }
  catch (cause) {
    const status = (cause as { response?: { status?: number } })?.response?.status
    if (status === 410) {
      const current: ReplicaState = replicasOf.value[version.id] ?? { items: [], problem: '', availability: '' }
      replicasOf.value = { ...replicasOf.value, [version.id]: { ...current, sourceUnavailable: true,
        availability: '', sourceDigest: null, validUntil: null,
        problem: bundleReplicaAvailabilityLabelOf('unavailable')! } }
    } else error.value = `Bundle 导出失败：${String(cause)}`
  }
}

/**
 * Bundle 证据信封 / 授权副本 / 许可来源。
 * 副本与许可来源接口 404 = 中心尚未提供，显示“尚未提供”，不伪造副本；
 * 410 = 撤销/过期，不显示在线或已重新授权；目录副本与响应头离线快照使用同一契约文案。
 */
const evidenceFor = ref('')
const evidenceBody = ref<BundleEvidence | null>(null)
const evidenceProblem = ref('')
interface ReplicaState {
  items: BundleReplica[]
  problem: string
  availability: string
  sourceDigest?: string | null
  validUntil?: string | null
  sourceUnavailable?: boolean
}
const replicasOf = ref<Record<string, ReplicaState>>({})
function sourceUnavailable(version: ResourceVersion) {
  const state = replicasOf.value[version.id]
  return state?.sourceUnavailable || Boolean(state?.items.length
    && state.items.every(replica => replica.availability === 'unavailable'))
}
async function showEvidence(resource: Resource, version: ResourceVersion) {
  evidenceFor.value = version.id
  evidenceBody.value = null
  evidenceProblem.value = ''
  try {
    const { data } = await resourcesApi.bundleEvidence(resource.id, version.id)
    evidenceFor.value = version.id
    evidenceBody.value = data
  } catch (cause) { evidenceProblem.value = `证据信封读取失败：${String(cause)}` }
}
async function loadReplicas(resource: Resource, version: ResourceVersion) {
  const current: ReplicaState = replicasOf.value[version.id] ?? { items: [], problem: '', availability: '' }
  replicasOf.value = { ...replicasOf.value, [version.id]: current }
  try {
    const { data } = await resourcesApi.replicas(resource.id, version.id)
    const items = data.replicas ?? []
    const unavailable = items.length > 0 && items.every(replica => replica.availability === 'unavailable')
    replicasOf.value = { ...replicasOf.value,
      [version.id]: { ...current, items, problem: current.sourceUnavailable ? current.problem : '',
        ...(unavailable ? { availability: '', sourceDigest: null, validUntil: null } : {}) } }
  } catch (cause) {
    const status = (cause as { response?: { status?: number } })?.response?.status
    replicasOf.value = { ...replicasOf.value, [version.id]: { ...current,
      problem: status === 404 ? '中心尚未提供授权副本目录（404），不伪造副本。' : `副本目录读取失败：${String(cause)}` } }
  }
}
async function licensedSource(resource: Resource, version: ResourceVersion) {
  const current: ReplicaState = replicasOf.value[version.id] ?? { items: [], problem: '', availability: '' }
  try {
    const { blob, availability, sourceDigest, validUntil } = await resourcesApi.licensedSource(resource.id, version.id)
    const label = availability === 'online' ? '在线来源' : availability === 'offline_snapshot'
      ? bundleReplicaAvailabilityLabelOf('licensed_copy')! : '来源可用性未知（老中心，未返回可用性头）'
    replicasOf.value = { ...replicasOf.value, [version.id]: { ...current, availability: label, sourceDigest, validUntil } }
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${resource.display_name}.source`
    a.click()
    URL.revokeObjectURL(url)
  } catch (cause) {
    const status = (cause as { response?: { status?: number } })?.response?.status
    replicasOf.value = { ...replicasOf.value, [version.id]: { ...current,
      availability: '', sourceDigest: null, validUntil: null,
      sourceUnavailable: status === 410 || current.sourceUnavailable,
      problem: status === 410 ? bundleReplicaAvailabilityLabelOf('unavailable')!
        : status === 404 ? '中心尚未提供许可来源读取（404）。' : `许可来源读取失败：${String(cause)}` } }
  }
}
async function revokeReplica(resource: Resource, version: ResourceVersion, replicaId: string) {
  const current = replicasOf.value[version.id] ?? { items: [], problem: '', availability: '' }
  try {
    await ElMessageBox.confirm(`撤销副本 ${replicaId}？撤销后该副本停止新授权。`, '撤销授权副本')
  } catch { return }
  try {
    await resourcesApi.revokeReplica(resource.id, version.id, replicaId, `revoke-${version.id}-${replicaId}-${crypto.randomUUID()}`)
    await loadReplicas(resource, version)
  } catch (cause) { replicasOf.value = { ...replicasOf.value, [version.id]: { ...current, problem: `副本撤销失败：${String(cause)}` } } }
}
function page(delta: number) { offset.value = Math.max(0, offset.value + delta * 50); void load() }
watch(scope, () => { offset.value = 0; resources.value = []; void load() })
onMounted(load)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <section class="resources">
    <header><div><h1>资源库</h1><p>每份资源保留自己的归属、固定版本和原始出处。</p></div>
      <el-tooltip v-if="!auth.canUpload && !readonlyHint" content="只读成员不能上传文档">
        <span><el-button type="primary" disabled>上传资料</el-button></span>
      </el-tooltip>
      <el-tooltip v-else-if="readonlyHint" :content="readonlyHint">
        <span><el-button type="primary" disabled>上传资料</el-button></span>
      </el-tooltip>
      <el-button v-else type="primary" @click="openUpload()">上传资料</el-button>
    </header>
    <nav aria-label="资源范围">
      <el-radio-group v-model="scope"><el-radio-button value="mine">我的资源</el-radio-button><el-radio-button value="site_public">本站公开</el-radio-button></el-radio-group>
      <el-button @click="load">刷新</el-button>
    </nav>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取资源…</p>
    <p v-else-if="!resources.length">{{ scope === 'mine' ? '尚无资源。上传资料后，系统会建立你的独立资源记录。' : '本站暂无已就绪的公开资源。' }}</p>
    <article v-for="resource in resources" :key="resource.id">
      <div class="resource-heading"><h2>{{ resource.display_name }}</h2><span class="state">● {{ labels[resource.publication] }}</span></div>
      <p class="identity">资源 {{ resource.id }} · 来源 {{ resource.uploader_ref.issuer }} / {{ resource.uploader_ref.subject }}</p>
      <div v-for="version in resource.versions" :key="version.id" class="version">
        <span class="number">v{{ version.version_no }}</span>
        <button class="title" @click="open(resource, version)">{{ version.filename }}</button>
        <span class="phase" :data-parse="version.parse_status ?? 'none'" :data-index="version.index_status ?? 'none'">
          解析 {{ parseStatusLabelOf(version.parse_status) ?? '无解析任务' }} · 索引 {{ indexStatusLabelOf(version.index_status) ?? '无' }}
        </span>
        <span class="digest" :title="version.source_digest">{{ version.source_digest ? (version.source_digest_verified === false ? '原文字节未验证 · ' : '') + version.source_digest.slice(0, 16) : '原文字节未验证' }}</span>
        <el-button v-if="!sourceUnavailable(version)" link @click="bundle(resource, version)">导出 Bundle</el-button>
        <el-button link @click="showEvidence(resource, version)">证据信封</el-button>
        <template v-if="!onLocalSource">
          <el-button link @click="loadReplicas(resource, version)">授权副本</el-button>
          <el-button v-if="!sourceUnavailable(version)" link @click="licensedSource(resource, version)">许可来源</el-button>
        </template>
      </div>
      <div v-if="evidenceBody && resource.versions.some(v => v.id === evidenceFor)" class="bundle-detail" aria-label="证据信封">
        <p class="muted">结构验收 {{ evidenceBody.structural_validation }} · 语义 {{ evidenceBody.semantic_review }}（导入证据不能当成已复核证据）</p>
        <p v-if="evidenceProblem" role="alert" class="error">{{ evidenceProblem }}</p>
        <p class="muted">证据 {{ evidenceBody.evidence.length }} 条（冻结信封，只读）</p>
      </div>
      <div v-for="version in resource.versions" :key="`replicas-${version.id}`">
        <p v-if="replicasOf[version.id]?.problem" role="alert" class="error">{{ replicasOf[version.id]?.problem }}</p>
        <p v-if="replicasOf[version.id]?.availability" class="muted">
          {{ replicasOf[version.id]?.availability }}
          <span v-if="replicasOf[version.id]?.sourceDigest"> · 原文摘要 <span class="digest">{{ replicasOf[version.id]?.sourceDigest }}</span></span>
          <span v-if="replicasOf[version.id]?.validUntil"> · 许可有效至 <span class="digest">{{ replicasOf[version.id]?.validUntil }}</span></span>
        </p>
        <ul v-if="(replicasOf[version.id]?.items ?? []).length" aria-label="授权副本">
          <li v-for="replica in replicasOf[version.id]?.items ?? []" :key="replica.replica_id">
            <span class="digest">{{ replica.replica_id }}</span>
            <span :class="replica.availability === 'unavailable' ? 'error' : 'muted'"
                  :role="replica.availability === 'unavailable' ? 'alert' : undefined">{{ bundleReplicaAvailabilityLabelOf(replica.availability) }}</span>
            <span class="muted"> · 原文摘要 <span class="digest">{{ replica.source_digest }}</span></span>
            <span v-if="replica.valid_until" class="muted"> · 许可有效至 <span class="digest">{{ replica.valid_until }}</span></span>
            <el-tooltip v-if="readonlyHint" :content="readonlyHint">
              <span><el-button link type="danger" disabled>撤销副本</el-button></span>
            </el-tooltip>
            <el-button v-else link type="danger" @click="revokeReplica(resource, version, replica.replica_id)">撤销副本</el-button>
          </li>
        </ul>
      </div>
      <footer v-if="scope === 'mine' && resource.owner_id === auth.profile?.id && (auth.canUpload || readonlyHint)">
        <el-tooltip v-if="readonlyHint" :content="readonlyHint">
          <span>
            <el-button disabled>追加版本</el-button>
            <el-button disabled>{{ resource.publication === 'published' ? '撤下公开' : '公开到本站' }}</el-button>
            <el-button type="danger" plain disabled>删除资源</el-button>
          </span>
        </el-tooltip>
        <template v-else>
          <el-button :disabled="resource.publication === 'withdrawn'"
                     @click="openUpload(resource)">追加版本</el-button>
          <el-button v-if="!onLocalSource" @click="publication(resource)">{{ resource.publication === 'published' ? '撤下公开' : '公开到本站' }}</el-button>
          <el-button type="danger" plain @click="remove(resource)">删除资源</el-button>
        </template>
        <el-button v-if="readonlyHint" link @click="proposeUploadAsTask">作为联邦任务发起</el-button>
      </footer>
    </article>
    <div class="pagination"><el-button :disabled="offset === 0 || loading" @click="page(-1)">上一页</el-button><span class="number">{{ offset / 50 + 1 }}</span><el-button :disabled="!more || loading" @click="page(1)">下一页</el-button></div>
    <UploadDialog v-model="upload" :resource-id="uploadTarget?.id" @uploaded="load" />
  </section>
</template>

<style scoped>
.resources { max-width: 1120px; margin: auto; }
header, nav, .resource-heading, .version, footer, .pagination { display: flex; align-items: center; gap: 16px; }
header { justify-content: space-between; margin-bottom: 24px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 0; }
p { color: var(--ddp-ink-2); margin: 8px 0; }
article { padding: 24px 0; border-bottom: 1px solid var(--ddp-line); }
.state { color: var(--ddp-ink-2); font-size: 13px; }
.identity, .number, .digest { font-family: var(--ddp-font-mono); font-size: 12px; font-variant-numeric: tabular-nums; }
.phase { color: var(--ddp-ink-2); font-size: 12.5px; }
.identity { overflow-wrap: anywhere; }
.version { min-height: 44px; flex-wrap: wrap; }
.title { border: 0; background: transparent; color: var(--ddp-ink); text-align: left; cursor: pointer; font: inherit; min-height: 44px; }
.title:hover { text-decoration: underline; }
.digest { margin-left: auto; color: var(--ddp-ink-2); }
footer { margin-top: 12px; }
.pagination { justify-content: flex-end; margin-top: 24px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); }
.bundle-detail { margin: 8px 0; padding-left: 12px; border-left: 2px solid var(--ddp-line); }
.bundle-detail .muted { color: var(--ddp-ink-2); font-size: 12.5px; }
</style>
