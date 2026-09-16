<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { RouterLink } from 'vue-router'
import PdfCanvas from '@/components/viewer/PdfCanvas.vue'
import LocalWikiPanel from '@/components/LocalWikiPanel.vue'
import FederationPlanPanel from '@/components/FederationPlanPanel.vue'
import { isDark, toggleTheme } from '@/composables/useTheme'
import { unwrap, workspaceError, type ClientView, type ConnectionSummary, type Json } from '@/platform/desktop'
import { DraftWriter } from '@/platform/draft-writer'
import type { Highlight } from '@/types/workbench'

const bridge = window.ddpDesktop
const connections = shallowRef<ConnectionSummary[]>([]), selected = ref(''), error = ref(''), busy = ref(false)
const question = ref(''), section = ref('resources'), selectedVersion = ref(''), sourceWidth = ref(380)
const scrollTop = ref(0), results = shallowRef<Record<string, Json>[]>([]), output = shallowRef<Record<string, Json> | null>(null)
const evidence = shallowRef<Record<string, Json> | null>(null), original = ref(''), pageIdx = ref(0), pageCount = ref(0)
const pendingKey = ref(''), saveStatus = ref(''), credentialMode = ref('')
const pairing = ref(false), persistCredential = ref(false), persistentAvailable = ref(false)
const centerLabel = ref(''), centerEndpoint = ref(''), centerNode = ref(''), centerWorkspace = ref(''), centerSubject = ref(''), centerSecret = ref('')
const view = shallowRef<ClientView | null>(null)
const modelCatalog = shallowRef<Record<string, Json> | null>(null), loadingModels = ref(false)
const resourceWindow = shallowRef<Record<string, Json> | null>(null), taskWindow = shallowRef<Record<string, Json> | null>(null)
const loadingWindow = ref(false), remoteSearchAllowed = ref(false)
const current = computed(() => connections.value.find(item => item.connectionId === selected.value))
const centers = computed(() => connections.value.filter(item => item.kind === 'remote'))
const ready = computed(() => view.value?.transport === 'ready')
const record = (value: unknown): Record<string, Json> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, Json> : {}
const rows = (value: unknown): Record<string, Json>[] => Array.isArray(value) ? value.map(record) : []
const text = (value: unknown) => typeof value === 'string' ? value : ''
const state = computed(() => record(view.value?.projection?.state))
const resources = computed(() => rows(resourceWindow.value?.items ?? state.value.resources))
const tasks = computed(() => rows(taskWindow.value?.items ?? state.value.tasks))
const resourcePage = computed(() => resourceWindow.value ?? record(record(state.value.windows).resources))
const taskPage = computed(() => taskWindow.value ?? record(record(state.value.windows).tasks))
const resourceVersion = (item: Record<string, Json>) => text(item.version_id) || text(item.id)
const models = computed(() => rows(modelCatalog.value?.items)), modelRuntime = computed(() => record(modelCatalog.value?.runtime))
const generation = computed(() => record(record(state.value.capabilities).generation))
const activeResource = computed(() => resources.value.find(item => resourceVersion(item) === selectedVersion.value))
const wikiSources = computed(() => resources.value.filter(item => item.state === 'ready' && (!selectedVersion.value || resourceVersion(item) === selectedVersion.value))
  .map(item => ({ resource_id: text(item.resource_id), source_version_id: resourceVersion(item), filename: text(item.filename) })).filter(item => item.resource_id))
const virtualStart = computed(() => Math.max(0, Math.floor(scrollTop.value / 48) - 4))
const virtualResources = computed(() => resources.value.slice(virtualStart.value, virtualStart.value + 20))
const transportLabel: Record<string, string> = { disconnected: '已断开', connecting: '连接中', authenticating: '认证中', ready: '已连接', backoff: '等待重连', blocked: '需要处理' }
const snapshotLabel: Record<string, string> = { loading: '读取中', current: '已同步', stale: '上次取得的内容', failed: '读取失败' }
const taskLabel: Record<string, string> = { queued: '排队中', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', ready: '可用' }
const taskKind: Record<string, string> = { parse: '解析 PDF', wiki: '构建 Wiki', answer: '生成回答', model_install: '安装模型', model_start: '启动模型', model_stop: '停止模型' }
const modelStatus: Record<string, string> = { not_installed: '未安装', partial: '下载未完成', installed: '已安装并校验', failed: '失败', verification_required: '需要校验', stopped: '未启动', starting: '启动中', ready: '运行中' }
function fileSize(value: unknown) { const bytes = Number(value); return Number.isFinite(bytes) ? `${(bytes / 1024 / 1024).toFixed(1)} MiB` : '未知大小' }
const pageSize = computed<[number, number] | null>(() => {
  const size = record(record(evidence.value?.locator).page_size)
  return typeof size.width === 'number' && typeof size.height === 'number' ? [size.width, size.height] : null
})
const highlights = computed<Highlight[]>(() => {
  const box = record(evidence.value?.locator).bbox
  return Array.isArray(box) && box.length === 4 && box.every(n => typeof n === 'number')
    ? [{ pageIdx: Number(record(evidence.value?.locator).physical_page_index ?? 0), pageSize: pageSize.value,
      kind: 'citation', bbox: box as [number, number, number, number], label: '出处' }] : []
})
let generationId = 0, subscription = '', eventRevision = -1, draftLoading = false, alive = true, draftDurable = true
let removeListener: (() => void) | undefined
let draftWriter: DraftWriter | undefined
watch(() => state.value.snapshot_id, () => { resourceWindow.value = null; taskWindow.value = null; scrollTop.value = 0 })
watch([question, selectedVersion, selected], () => { remoteSearchAllowed.value = false }, { flush: 'sync' })
watch([section, () => view.value?.projection?.sequence, ready], () => {
  if (section.value === 'models' && ready.value && current.value?.kind === 'local') void loadModels()
})

function clearOriginal() { if (original.value) URL.revokeObjectURL(original.value); original.value = ''; pageCount.value = 0 }
function draft() { return { question: question.value, section: section.value, selectedVersion: selectedVersion.value,
  sourceWidth: sourceWidth.value, pageIdx: pageIdx.value, pendingKey: pendingKey.value } }
function saveDraft() {
  if (!bridge || !selected.value || draftLoading || !draftWriter) return Promise.resolve()
  return draftWriter.write(draft())
}
watch([question, section, selectedVersion, sourceWidth, pageIdx, pendingKey], () => {
  if (draftLoading) return
  draftDurable = false; saveStatus.value = '草稿待保存'
  // Send the edit to the persistent owner immediately. A renderer reload must
  // not silently drop text sitting in a component's debounce timer.
  void saveDraft()
}, { flush: 'sync' })

async function select(connectionId: string) {
  if (!bridge) return
  await saveDraft()
  const mine = ++generationId
  if (subscription) void bridge.clientUnsubscribe({ subscriptionId: subscription })
  selected.value = connectionId; subscription = crypto.randomUUID(); eventRevision = -1; busy.value = false
  view.value = current.value?.view ?? null; error.value = ''; results.value = []; output.value = null; evidence.value = null
  modelCatalog.value = null; loadingModels.value = false; resourceWindow.value = null; taskWindow.value = null; loadingWindow.value = false
  clearOriginal(); draftLoading = true; question.value = ''; section.value = 'resources'; selectedVersion.value = ''; pendingKey.value = ''; draftWriter = undefined; draftDurable = false
  const sub = subscription
  try {
    const [summaryResult, savedResult] = await Promise.all([
      bridge.clientSubscribe({ connectionId, subscriptionId: sub }), bridge.clientReadDraft({ connectionId, key: 'workspace' }),
    ])
    if (mine !== generationId) return
    const summary = unwrap(summaryResult)
    if (summary.revision >= eventRevision) { eventRevision = summary.revision; view.value = summary.view }
    const saved = unwrap(savedResult), data = record(saved?.value)
    draftWriter = new DraftWriter(saved?.revision ?? 0, async (expectedRevision, value) => unwrap(await bridge.clientSaveDraft({ connectionId,
      key: 'workspace', expectedRevision, value })).revision, status => {
      if (mine !== generationId) return
      draftDurable = !status.error && !status.pending
      saveStatus.value = status.error ? workspaceError(status.error) : status.pending ? '草稿保存中' : '草稿已保存在此工作区'
      if (status.error) error.value = saveStatus.value
    })
    draftDurable = true; question.value = text(data.question)
    section.value = ['resources', 'conversation', 'wiki', 'tasks', 'models', 'plans'].includes(text(data.section)) ? text(data.section) : 'resources'
    selectedVersion.value = text(data.selectedVersion); pendingKey.value = text(data.pendingKey)
    sourceWidth.value = typeof data.sourceWidth === 'number' ? Math.max(300, Math.min(data.sourceWidth, 600)) : 380
    pageIdx.value = typeof data.pageIdx === 'number' ? Math.max(0, data.pageIdx) : 0
    saveStatus.value = saved ? '已恢复此工作区草稿' : ''
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) draftLoading = false }
}
async function addWorkspace() {
  if (!bridge || busy.value) return
  busy.value = true; error.value = ''
  try {
    const chosen = unwrap(await bridge.selectWorkspace()); if (!chosen) return
    const summary = unwrap(await bridge.clientConnectLocal({ workspaceId: chosen.workspaceId }))
    const previous = connections.value.findIndex(item => item.connectionId === summary.connectionId)
    if (previous < 0) connections.value = [...connections.value, summary]
    else connections.value = connections.value.map((item, index) => index === previous ? summary : item)
    await select(summary.connectionId)
  } catch (cause) { error.value = workspaceError(cause) }
  finally { busy.value = false }
}
async function pairCenter() {
  if (!bridge || busy.value) return
  busy.value = true; error.value = ''
  try {
    const authority = centerNode.value.trim(), workspace = centerWorkspace.value.trim(), subject = centerSubject.value.trim()
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([authority, workspace, subject])))
    const profileId = 'profile-' + Array.from(new Uint8Array(digest)).map(n => n.toString(16).padStart(2, '0')).join('').slice(0, 32)
    // The broker stores the token under the exact proposed identity. The provider
    // still proves the remote node before requesting it for any HTTP request.
    unwrap(await bridge.setCredential({ environmentId: authority, profileId, secret: centerSecret.value,
      persist: persistentAvailable.value && persistCredential.value }))
    const summary = unwrap(await bridge.clientPairRemote({
      environment: { environmentId: authority, authorityNodeId: authority, workspaceId: workspace, endpoint: centerEndpoint.value.trim() },
      profile: { profileId, issuer: authority, subject }, label: centerLabel.value.trim(),
    }))
    connections.value = [...connections.value.filter(item => item.connectionId !== summary.connectionId), summary]
    pairing.value = false; await select(summary.connectionId)
  } catch (cause) { error.value = workspaceError(cause) }
  finally { centerSecret.value = ''; busy.value = false }
}
async function run(action: 'search' | 'answer' | 'wiki') {
  if (!bridge || !current.value || !question.value.trim() || busy.value) return
  const connectionId = selected.value, mine = generationId
  busy.value = true; error.value = ''
  try {
    const payload = { query: question.value, version_ids: selectedVersion.value ? [selectedVersion.value] : null }
    if (action === 'search') {
      if (current.value.kind === 'remote' && !remoteSearchAllowed.value) throw new Error('approved_plan_required')
      const found = record(unwrap(await bridge.clientQuery({ connectionId, name: 'corpus.search', payload })))
      if (mine === generationId) { results.value = rows(found.hits); output.value = null }
    } else {
      if (pendingKey.value) throw new Error('receipt_required')
      pendingKey.value = crypto.randomUUID(); await saveDraft()
      if (!draftDurable || mine !== generationId || !alive) throw new Error('cache_failure')
      const accepted = unwrap(await bridge.clientCommand({ connectionId, name: action === 'wiki' ? 'wiki.build' : 'answer.generate',
        payload: { ...payload, execution_policy: 'local_only', allow_remote: false }, idempotencyKey: pendingKey.value }))
      if (mine === generationId) { output.value = record(accepted); pendingKey.value = ''; await saveDraft() }
    }
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) busy.value = false }
}
async function loadModels() {
  if (!bridge || !ready.value || loadingModels.value || current.value?.kind !== 'local') return
  const mine = generationId
  loadingModels.value = true
  try {
    const value = unwrap(await bridge.clientQuery({ connectionId: selected.value, name: 'models.list', payload: {} }))
    if (mine === generationId && alive) modelCatalog.value = record(value)
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) loadingModels.value = false }
}
async function modelCommand(action: 'install' | 'start' | 'stop', modelId?: string) {
  if (!bridge || busy.value || current.value?.kind !== 'local') return
  const mine = generationId, connectionId = selected.value
  try {
    if (pendingKey.value) throw new Error('receipt_required')
    busy.value = true; error.value = ''; pendingKey.value = crypto.randomUUID(); await saveDraft()
    if (!draftDurable || mine !== generationId || !alive) throw new Error('cache_failure')
    const result = unwrap(await bridge.clientCommand({ connectionId, name: `models.${action}`,
      payload: modelId ? { model_id: modelId } : {}, idempotencyKey: pendingKey.value }))
    if (mine === generationId) { output.value = record(result); pendingKey.value = ''; await saveDraft(); await loadModels() }
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) busy.value = false }
}
async function wikiQuery(name: 'wiki.list' | 'wiki.get' | 'wiki.revisions', payload: Json): Promise<Json> {
  if (!bridge || current.value?.kind !== 'local') throw new Error('unsupported_operation')
  const mine = generationId
  const result = unwrap(await bridge.clientQuery({ connectionId: selected.value, name, payload }))
  if (mine !== generationId || !alive) throw new Error('disposed')
  return result
}
async function wikiCommand(name: 'wiki.create' | 'wiki.rebuild' | 'wiki.edit', payload: Json): Promise<Json> {
  if (!bridge || current.value?.kind !== 'local' || busy.value) throw new Error('unsupported_operation')
  if (pendingKey.value) throw new Error('receipt_required')
  const mine = generationId, connectionId = selected.value
  busy.value = true; error.value = ''
  try {
    pendingKey.value = crypto.randomUUID(); await saveDraft()
    if (!draftDurable || mine !== generationId || !alive) throw new Error('cache_failure')
    const result = unwrap(await bridge.clientCommand({ connectionId, name, payload, idempotencyKey: pendingKey.value }))
    if (mine !== generationId || !alive) throw new Error('disposed')
    output.value = record(result); pendingKey.value = ''; await saveDraft()
    return result
  } finally { if (mine === generationId) busy.value = false }
}
async function nextWindow(kind: 'resources' | 'tasks') {
  if (!bridge || loadingWindow.value) return
  const page = kind === 'resources' ? resourcePage.value : taskPage.value
  const snapshotId = text(state.value.snapshot_id), cursor = text(page.next_cursor), mine = generationId
  if (!snapshotId || !cursor) return
  loadingWindow.value = true
  try {
    const value = record(unwrap(await bridge.clientQuery({ connectionId: selected.value,
      name: kind === 'resources' ? 'resource.page' : 'task.page', payload: { snapshot_id: snapshotId, cursor } })))
    // Never splice windows from different identities or catalog snapshots.
    if (mine !== generationId || snapshotId !== state.value.snapshot_id || value.snapshot_id !== snapshotId) return
    if (kind === 'resources') { resourceWindow.value = value; scrollTop.value = 0 }
    else taskWindow.value = value
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) loadingWindow.value = false }
}
async function reconcile() {
  if (!bridge || !pendingKey.value) return
  const mine = generationId
  try {
    const receipt = unwrap(await bridge.clientReceipt({ connectionId: selected.value, idempotencyKey: pendingKey.value }))
    if (mine !== generationId) return
    if (receipt === null) { error.value = '尚未找到回执；保留操作编号，不自动重复提交。'; return }
    output.value = record(receipt); pendingKey.value = ''; error.value = ''; await saveDraft()
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
}
async function importFile(kind: 'pdf' | 'bundle') {
  if (!bridge || busy.value) return
  const mine = generationId, connectionId = selected.value
  try {
    if (pendingKey.value) throw new Error('receipt_required')
    busy.value = true; pendingKey.value = crypto.randomUUID(); await saveDraft()
    if (!draftDurable || mine !== generationId || !alive) throw new Error('cache_failure')
    const result = unwrap(await bridge.clientImportFile({ connectionId, kind, idempotencyKey: pendingKey.value }))
    if (mine === generationId) { output.value = record(result); pendingKey.value = ''; error.value = ''; await saveDraft() }
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
  finally { if (mine === generationId) busy.value = false }
}
async function openSource(versionId: string, locator?: Record<string, Json>) {
  if (!bridge) return
  const mine = generationId; selectedVersion.value = versionId; evidence.value = locator ?? null
  clearOriginal(); pageIdx.value = Number(record(locator?.locator).physical_page_index ?? 0)
  try {
    const bytes = unwrap(await bridge.clientReadOriginal({ connectionId: selected.value, versionId }))
    if (!alive || mine !== generationId || selectedVersion.value !== versionId) return
    original.value = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: 'application/pdf' }))
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
}
async function showEvidence(evidenceId: string) {
  if (!bridge) return
  const mine = generationId
  try {
    const found = record(unwrap(await bridge.clientQuery({ connectionId: selected.value, name: 'evidence.get', payload: { evidence_id: evidenceId } })))
    if (mine !== generationId) return
    const envelope = record(found.evidence ?? found)
    // A verified bundle retains its original source identity. File access must
    // use this workspace's authorized copy, not rewrite or resolve the origin ID.
    await openSource(text(found.version_id) || text(envelope.source_version_id), envelope)
  } catch (cause) { if (mine === generationId) error.value = workspaceError(cause) }
}
async function wake() { if (bridge) { try { unwrap(await bridge.clientWake({ connectionId: selected.value })) } catch (cause) { error.value = workspaceError(cause) } } }
async function disconnect() { if (bridge) { try { unwrap(await bridge.clientDisconnect({ connectionId: selected.value })) } catch (cause) { error.value = workspaceError(cause) } } }
async function cancel(taskId: string) {
  if (!bridge) return
  try { unwrap(await bridge.clientCommand({ connectionId: selected.value, name: 'task.cancel', payload: { task_id: taskId }, idempotencyKey: crypto.randomUUID() })) }
  catch (cause) { error.value = workspaceError(cause) }
}
async function exportBundle() {
  if (!bridge || !selectedVersion.value) return
  try { unwrap(await bridge.clientExportBundle({ connectionId: selected.value, versionId: selectedVersion.value })) }
  catch (cause) { error.value = workspaceError(cause) }
}
onMounted(async () => {
  if (!bridge) return
  removeListener = bridge.onClientView(event => {
    if (event.subscriptionId !== subscription || event.connectionId !== selected.value || event.revision <= eventRevision) return
    eventRevision = event.revision; view.value = event.view
    const item = current.value; if (item) { item.view = event.view; item.revision = event.revision }
  })
  try {
    connections.value = unwrap(await bridge.clientList())
    const host = unwrap(await bridge.hostStatus())
    persistentAvailable.value = host.secrets.persistentAvailable
    credentialMode.value = host.secrets.persistentAvailable ? '凭证由系统密钥库保存' : '凭证仅保留在本次会话'
    if (connections.value[0]) await select(connections.value[0].connectionId)
  } catch (cause) { error.value = workspaceError(cause) }
})
onBeforeUnmount(() => {
  alive = false
  void saveDraft(); removeListener?.()
  if (subscription && bridge) void bridge.clientUnsubscribe({ subscriptionId: subscription })
  clearOriginal()
})
</script>

<template>
  <main class="environment-workspace" :style="{ '--source-width': `${sourceWidth}px` }">
    <header class="workspace-top">
      <strong>DeepDocParse</strong><span>{{ current?.label || '工作区' }}</span>
      <span v-if="current" class="identity">{{ current.environment.workspaceId }}</span>
      <span class="top-spacer" />
      <span v-if="current">{{ current.kind === 'local' ? '本机执行 · 原件不外发，远端计划须逐项批准' : '中心执行 · 按操作确认外发' }}</span>
      <el-button text :aria-label="isDark ? '切到浅色' : '切到深色'" @click="toggleTheme()">{{ isDark ? '浅色' : '深色' }}</el-button>
    </header>
    <aside class="workspace-nav">
      <h2>工作区</h2>
      <button v-for="item in connections" :key="item.connectionId" class="nav-item" :aria-current="item.connectionId === selected ? 'page' : undefined" @click="select(item.connectionId)">{{ item.label }}<small>{{ item.kind === 'local' ? '本机' : '中心' }}</small></button>
      <el-button v-if="bridge" text :loading="busy" @click="addWorkspace">打开本地工作区…</el-button>
      <el-button v-if="bridge" text @click="pairing = !pairing">配对中心…</el-button>
      <template v-if="current">
        <h2>内容</h2>
        <button v-for="entry in [['resources','资源'],['conversation','问答'],['wiki','Wiki'],['tasks','任务'],['plans','远端计划'],['models','本地模型']]" :key="entry[0]" class="nav-item" :aria-current="section === entry[0] ? 'page' : undefined" @click="section = entry[0]!">{{ entry[1] }}</button>
        <p class="muted">{{ credentialMode }}</p>
        <el-button text @click="disconnect">断开连接</el-button>
      </template>
      <RouterLink v-if="!bridge" to="/resources">打开本站资源库</RouterLink>
    </aside>
    <section class="workspace-content">
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <form v-if="pairing && bridge" class="center-pairing" @submit.prevent="pairCenter">
        <h1>配对中心</h1>
        <p class="muted">使用中心管理员提供的节点身份和你的工作区、用户编号。连接前会核对节点身份；凭证由桌面宿主管理。</p>
        <label>名称<input v-model="centerLabel" required maxlength="80" autocomplete="off" /></label>
        <label>HTTPS 地址<input v-model="centerEndpoint" required type="url" placeholder="https://center.example" autocomplete="off" /></label>
        <label>节点身份<input v-model="centerNode" required placeholder="node-…" autocomplete="off" spellcheck="false" /></label>
        <label>工作区编号<input v-model="centerWorkspace" required autocomplete="off" spellcheck="false" /></label>
        <label>用户编号<input v-model="centerSubject" required autocomplete="off" spellcheck="false" /></label>
        <label>API 凭证<input v-model="centerSecret" required type="password" autocomplete="off" spellcheck="false" /></label>
        <label class="persist-option"><input v-model="persistCredential" type="checkbox" :disabled="!persistentAvailable" />保存在系统密钥库</label>
        <p class="muted">{{ credentialMode }}</p>
        <div class="actions"><el-button native-type="submit" :loading="busy">验证并连接</el-button><el-button text @click="pairing = false; centerSecret = ''">取消</el-button></div>
      </form>
      <template v-if="!current">
        <h1>资料留在所属工作区</h1>
        <p>{{ bridge ? '打开一个本地目录，导入 PDF 或资料包后开始检索。' : '此浏览器页面尚未配对执行环境。可以进入本站资源库，或在桌面应用打开本地工作区。' }}</p>
        <el-button v-if="bridge" @click="addWorkspace">打开本地工作区</el-button>
      </template>
      <template v-else>
        <div class="connection-status" aria-live="polite">
          <span class="status-dot" :class="{ pending: view?.transport !== 'ready' }" />
          <span>{{ transportLabel[view?.transport ?? 'disconnected'] }} · {{ snapshotLabel[view?.snapshot ?? 'loading'] }}</span>
          <el-button v-if="!ready" text @click="wake">重新连接</el-button>
        </div>
        <p v-if="view?.reason" class="muted">{{ workspaceError(new Error(view.reason)) }}</p>
        <p v-if="pendingKey" class="pending-operation">有一项提交尚待对账。<code>{{ pendingKey }}</code> <el-button :disabled="!ready" text @click="reconcile">查询回执</el-button></p>
        <template v-if="section === 'resources'">
          <div class="section-heading"><h1>资源</h1><span class="mono">{{ resourcePage.visible_total ?? resources.length }}</span></div>
          <div class="actions"><el-button :disabled="!ready || busy" @click="importFile('pdf')">导入 PDF…</el-button><el-button :disabled="!ready || busy" @click="importFile('bundle')">导入资料包…</el-button></div>
          <p class="muted">点选资源可查看固定版本原文；检索范围将限定为该版本。</p>
          <div class="resource-list" role="list" aria-label="工作区资源" @scroll="scrollTop = ($event.target as HTMLElement).scrollTop">
            <div :style="{ height: `${resources.length * 48}px`, position: 'relative' }">
              <div v-for="(resource, index) in virtualResources" :key="resourceVersion(resource)" role="listitem" :style="{ position: 'absolute', top: `${(virtualStart + index) * 48}px`, width: '100%', height: '48px' }"><button class="resource-row" :aria-current="resourceVersion(resource) === selectedVersion ? 'true' : undefined" @click="openSource(resourceVersion(resource))">
                <span>{{ text(resource.filename) || text(resource.title) || text(resource.id) }}</span><small>{{ taskLabel[text(resource.state)] || text(resource.state) }}</small>
              </button></div>
            </div>
            <p v-if="!resources.length && view?.snapshot === 'current'" class="muted">此工作区尚无资源。</p>
          </div>
          <div v-if="resourcePage.has_more || resourceWindow" class="actions"><span class="muted">本页 {{ resources.length }} 项 · 共 {{ resourcePage.visible_total }} 项；其余内容尚未缓存</span><el-button v-if="resourceWindow" text @click="resourceWindow = null; scrollTop = 0">返回资源首页</el-button><el-button :disabled="!ready || !resourcePage.has_more || loadingWindow" @click="nextWindow('resources')">下一页资源</el-button></div>
        </template>
        <LocalWikiPanel v-if="section === 'wiki' && current.kind === 'local' && bridge" :key="selected" :bridge="bridge" :connection-id="selected" :ready="ready" :pending="!!pendingKey || busy" :sources="wikiSources" :query="wikiQuery" :command="wikiCommand" @evidence="showEvidence" />
        <p v-else-if="section === 'wiki'" class="muted">中心 Wiki 需要完整执行计划与外发许可，此入口尚未开放。</p>
        <FederationPlanPanel v-if="section === 'plans' && current.kind === 'local' && bridge" :key="'plans-' + selected" :bridge="bridge" :connection-id="selected" :ready="ready" :pending="!!pendingKey || busy" :centers="centers" :resources="resources" />
        <p v-else-if="section === 'plans'" class="muted">远端计划从本机工作区发起：计划、批准、对账与交付记录保存在本机。请先切到一个本机工作区。</p>
        <template v-if="section === 'conversation'">
          <h1>问答</h1>
          <p class="scope-line">检索范围：{{ selectedVersion ? text(activeResource?.filename) || text(activeResource?.title) || selectedVersion : '此工作区的可用资源' }} <el-button v-if="selectedVersion" text @click="selectedVersion = ''; evidence = null; clearOriginal()">使用工作区全部资料</el-button></p>
          <label class="input-label" for="workspace-question">问题或检索词</label>
          <textarea id="workspace-question" v-model="question" :readonly="draftLoading" maxlength="4096" rows="5" placeholder="从资料中查找什么？" />
          <p class="muted" aria-live="polite">{{ saveStatus }}</p>
          <label v-if="current.kind === 'remote'" class="outbound-consent"><input v-model="remoteSearchAllowed" type="checkbox" />允许将当前检索词和所选版本编号发送给 {{ current.label }}（{{ current.environment.authorityNodeId }}）；修改内容后需重新确认。</label>
          <div class="actions"><el-button :disabled="!ready || !question.trim() || busy || (current.kind === 'remote' && !remoteSearchAllowed)" @click="run('search')">检索证据</el-button><el-button :disabled="!ready || !question.trim() || busy || !!pendingKey || current.kind === 'remote'" :loading="busy" @click="run('answer')">生成回答</el-button></div>
          <p v-if="current.kind === 'remote'" class="muted">中心生成需要完整执行计划和外发许可，此入口尚未开放。</p>
          <p class="muted">{{ generation.available === false || generation.ready === false ? '本地生成模型尚未就绪；检索仍可使用。' : '生成内容需要逐条核对原始出处。' }}</p>
          <div v-for="hit in results" :key="text(hit.evidence_id)" class="evidence-result"><p>{{ text(hit.text) }}</p><el-button text @click="showEvidence(text(hit.evidence_id))">查看原始出处</el-button></div>
        </template>
        <template v-if="section === 'tasks'">
          <h1>任务</h1>
          <article v-for="task in tasks" :key="text(task.id)" class="task-row"><div><strong>{{ taskKind[text(task.kind)] || '任务' }}</strong><span>{{ taskLabel[text(task.status)] || text(task.status) }}</span></div><code>{{ task.id }}</code><p v-if="task.error" class="error">{{ workspaceError(new Error(text(task.error))) }}</p><el-button v-if="['queued','running'].includes(text(task.status))" text :disabled="!ready" @click="cancel(text(task.id))">取消任务</el-button><el-button v-if="task.result" text @click="output = record(task.result)">查看结果</el-button></article>
          <p v-if="!tasks.length" class="muted">尚无任务记录。</p>
          <div v-if="taskPage.has_more || taskWindow" class="actions"><span class="muted">本页 {{ tasks.length }} 项 · 共 {{ taskPage.visible_total }} 项</span><el-button v-if="taskWindow" text @click="taskWindow = null">返回任务首页</el-button><el-button :disabled="!ready || !taskPage.has_more || loadingWindow" @click="nextWindow('tasks')">下一页任务</el-button></div>
        </template>
        <template v-if="section === 'models'">
          <div class="section-heading"><h1>本地模型</h1><el-button v-if="current.kind === 'local'" text :loading="loadingModels" :disabled="!ready" @click="loadModels">刷新状态</el-button></div>
          <p v-if="current.kind !== 'local'" class="muted">此处只管理本机工作区的模型。中心能力由该中心提供。</p>
          <template v-else>
            <p class="muted">模型按工作区安装。只有点击下载才会联网获取下列文件；文档和问题保留在本机。下载可在任务中取消或继续。</p>
            <p v-if="modelCatalog" class="scope-line">运行状态：{{ modelStatus[text(modelRuntime.status)] || text(modelRuntime.status) }} <span v-if="modelRuntime.model_id">· {{ modelRuntime.model_id }}</span><el-button v-if="['ready','starting'].includes(text(modelRuntime.status))" text :disabled="!ready || busy || !!pendingKey" @click="modelCommand('stop')">停止模型</el-button></p>
            <article v-for="model in models" :key="text(record(model.manifest).id)" class="model-row">
              <h2>{{ text(record(model.manifest).name) || text(record(model.manifest).id) }}</h2>
              <p class="mono">{{ record(model.manifest).id }}</p>
              <p class="muted">{{ fileSize(record(model.manifest).bytes) }} · {{ record(model.manifest).license }} · {{ record(model.manifest).device || record(model.manifest).backend }} · {{ modelStatus[text(model.status)] || text(model.status) }}</p>
              <p v-if="model.downloaded_bytes" class="muted">已下载 {{ fileSize(model.downloaded_bytes) }} / {{ fileSize(record(model.manifest).bytes) }}</p>
              <p v-if="model.error" class="error">{{ workspaceError(new Error(text(model.error))) }}</p>
              <div class="actions"><el-button v-if="model.status !== 'installed'" :disabled="!ready || busy || !!pendingKey" @click="modelCommand('install', text(record(model.manifest).id))">{{ model.status === 'partial' ? '继续下载并校验' : model.status === 'verification_required' ? '校验已下载文件' : '下载并校验' }}</el-button><el-button v-if="model.status === 'installed' && record(model.manifest).kind === 'model'" :disabled="!ready || busy || !!pendingKey || modelRuntime.status === 'starting'" @click="modelCommand('start', text(record(model.manifest).id))">在本机启动</el-button></div>
            </article>
            <p v-if="!modelCatalog" class="muted">{{ loadingModels ? '正在读取模型目录…' : '连接工作区后读取模型状态。' }}</p>
          </template>
        </template>
        <article v-if="output" class="generated-result">
          <h2>{{ output.answer ? '生成内容 · 待复核' : '操作结果' }}</h2><p v-if="output.answer" class="generated-text">{{ text(output.answer) }}</p>
          <p v-else>{{ text(output.status) || text(output.task_id) || '已取得回执，可在任务中查看进展。' }}</p>
          <button v-for="item in rows(output.evidence)" :key="text(record(item.evidence ?? item).evidence_id)" class="citation-link" @click="showEvidence(text(record(item.evidence ?? item).evidence_id))">出处 · {{ Number(record(record(item.evidence ?? item).locator).physical_page_index ?? 0) + 1 }} 页</button>
        </article>
      </template>
    </section>
    <aside class="source-panel">
      <div class="section-heading"><h2>原文与出处</h2><el-button v-if="selectedVersion" text :disabled="!ready" @click="exportBundle">导出资料包</el-button></div>
      <label class="panel-width">面板宽度 <input v-model.number="sourceWidth" type="range" min="300" max="600" aria-label="原文面板宽度" /></label>
      <p v-if="selectedVersion" class="mono source-id">{{ selectedVersion }}</p>
      <dl v-if="evidence" class="source-provenance"><dt>原始节点</dt><dd>{{ text(evidence.origin_node_id) || '未提供' }}</dd><dt>原始版本</dt><dd>{{ text(evidence.source_version_id) }}</dd><dt>原件摘要</dt><dd>{{ text(evidence.source_digest) || '未提供' }}</dd></dl>
      <template v-if="original"><div class="page-controls"><el-button text :disabled="pageIdx <= 0" @click="pageIdx--">上一页</el-button><span class="mono">{{ pageIdx + 1 }} / {{ pageCount || '…' }}</span><el-button text :disabled="pageIdx + 1 >= pageCount" @click="pageIdx++">下一页</el-button></div><PdfCanvas :src="original" :page-idx="pageIdx" :page-size="pageSize" :highlights="highlights" @loaded="pageCount = $event" /></template>
      <p v-else class="muted">选择资源或点击回答的出处，在这里核对原文。</p>
    </aside>
  </main>
</template>

<style scoped>
.environment-workspace { min-height: 100vh; display: grid; grid-template-columns: 190px minmax(300px, 1fr) var(--source-width); grid-template-rows: 56px 1fr; color: var(--ddp-ink); }
.workspace-top { grid-column: 1 / -1; display: flex; gap: 20px; align-items: center; padding: 0 20px; border-bottom: 1px solid var(--ddp-line); }
.workspace-top strong { font-weight: 600; }.top-spacer { flex: 1; }.identity { max-width: 180px; overflow: hidden; text-overflow: ellipsis; font-family: var(--f-mono); font-size: 11px; color: var(--ddp-ink-3); }
.workspace-nav { padding: 24px 12px; border-right: 1px solid var(--ddp-line); }.workspace-nav h2 { padding: 0 8px; margin: 4px 0 12px; font-size: 12px; color: var(--ddp-ink-3); }.workspace-nav h2:not(:first-child) { margin-top: 32px; }
.nav-item { display: flex; width: 100%; align-items: center; justify-content: space-between; padding: 10px 8px; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }.nav-item[aria-current=page] { background: color-mix(in srgb, var(--ddp-ink) 7%, transparent); font-weight: 600; }.nav-item small { font-size: 11px; color: var(--ddp-ink-3); }
.workspace-content { padding: 24px 28px; min-width: 0; }.source-panel { min-width: 0; padding: 24px 16px; border-left: 1px solid var(--ddp-line); }.section-heading, .connection-status, .actions, .page-controls { display: flex; align-items: center; gap: 12px; }.section-heading { justify-content: space-between; }.connection-status { font-size: 12px; margin-bottom: 28px; }.status-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }.status-dot.pending { background: transparent; border: 1px solid currentColor; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 24px; }h2 { font-size: 15px; font-weight: 600; }p { line-height: 1.7; }.muted, small { color: var(--ddp-ink-3); font-size: 12px; }.error { color: var(--ddp-cite); }.mono, code { font-family: var(--f-mono); font-variant-numeric: tabular-nums; font-size: 11px; overflow-wrap: anywhere; }.source-id { overflow-wrap: anywhere; }.panel-width { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--ddp-ink-3); }.panel-width input { width: 110px; accent-color: var(--ddp-ink); }
.resource-list { height: 520px; overflow: auto; position: relative; }.resource-row { position: absolute; width: 100%; height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 12px; text-align: left; padding: 0 8px; border: 0; border-bottom: 1px solid var(--ddp-line); color: inherit; background: transparent; cursor: pointer; font: inherit; }.resource-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.resource-row[aria-current=true] { background: color-mix(in srgb, var(--ddp-ink) 7%, transparent); }
.input-label { display: block; margin: 20px 0 8px; font-size: 13px; }textarea { width: 100%; box-sizing: border-box; padding: 12px; border: 1px solid var(--ddp-line); border-radius: 4px; resize: vertical; background: var(--ddp-panel); color: inherit; font: inherit; line-height: 1.7; }.scope-line { font-size: 12px; }.evidence-result, .generated-result, .task-row { padding: 20px 0; border-top: 1px solid var(--ddp-line); margin-top: 24px; }.generated-text { white-space: pre-wrap; }.task-row > div { display: flex; justify-content: space-between; }.task-row strong { font-weight: 500; }.task-row span { font-size: 12px; }.citation-link { color: var(--ddp-cite); border: 0; background: transparent; cursor: pointer; padding: 8px 12px 8px 0; }.pending-operation { font-size: 12px; }.workspace-nav .muted { margin-top: 32px; }.page-controls { justify-content: space-between; margin: 20px 0; }
.center-pairing { margin-bottom: 32px; padding-bottom: 28px; border-bottom: 1px solid var(--ddp-line); }.center-pairing label { display: block; font-size: 12px; margin: 16px 0; }.center-pairing input:not([type=checkbox]) { display: block; box-sizing: border-box; width: 100%; margin-top: 6px; padding: 9px; border: 1px solid var(--ddp-line); border-radius: 4px; background: var(--ddp-panel); color: inherit; font: inherit; }.center-pairing .persist-option { display: flex; align-items: center; gap: 8px; }
.source-provenance { font-size: 11px; color: var(--ddp-ink-3); }.source-provenance dt { margin-top: 8px; }.source-provenance dd { margin: 3px 0 0; overflow-wrap: anywhere; font-family: var(--f-mono); }
.model-row { padding: 20px 0; border-top: 1px solid var(--ddp-line); }.outbound-consent { display: block; font-size: 12px; line-height: 1.8; margin: 16px 0; overflow-wrap: anywhere; }.outbound-consent input { margin-right: 8px; }
.source-panel { position: sticky; top: 0; align-self: start; max-height: 100vh; overflow: auto; box-sizing: border-box; }
@media (max-width: 1100px) { .source-panel { position: static; max-height: none; } }
@media (max-width: 1100px) { .environment-workspace { grid-template-columns: 160px minmax(300px, 1fr); }.source-panel { grid-column: 2; border-left: 0; border-top: 1px solid var(--ddp-line); }.workspace-nav { grid-row: 2 / 4; }.identity { display: none; }.workspace-top { gap: 12px; font-size: 12px; } }
</style>
