<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { unwrap, workspaceError, type DesktopBridge, type Json } from '@/platform/desktop'
import { DraftWriter } from '@/platform/draft-writer'

type Row = Record<string, Json>
type Source = { resource_id: string; source_version_id: string; filename: string }
type Query = 'wiki.list' | 'wiki.get' | 'wiki.revisions'
type Command = 'wiki.create' | 'wiki.rebuild' | 'wiki.edit'
const props = defineProps<{ connectionId: string; bridge: DesktopBridge; ready: boolean; pending: boolean;
  sources: Source[]; query: (name: Query, payload: Json) => Promise<Json>;
  command: (name: Command, payload: Json) => Promise<Json> }>()
const emit = defineEmits<{ evidence: [id: string] }>()
const row = (v: unknown): Row => v && typeof v === 'object' && !Array.isArray(v) ? v as Row : {}
const rows = (v: unknown): Row[] => Array.isArray(v) ? v.map(row) : []
const text = (v: unknown) => typeof v === 'string' ? v : ''
const title = ref(''), error = ref(''), saved = ref(''), busy = ref(false)
const listing = shallowRef<Row>({}), history = shallowRef<Row>({}), document = shallowRef<Row | null>(null)
const selectedWiki = ref(''), pageKey = ref(''), editBase = ref(''), paragraphId = ref('')
const editText = ref(''), originalText = ref('')
const revision = computed(() => row(document.value?.revision)), wiki = computed(() => row(document.value?.wiki))
const pages = computed(() => rows(revision.value.pages)), page = computed(() => pages.value.find(p => p.page_key === pageKey.value))
const dirty = computed(() => editText.value !== originalText.value)
const oldRevision = computed(() => revision.value.id && revision.value.id !== wiki.value.current_revision_id)
let alive = true, loadingDraft = true, requestGeneration = 0, writer: DraftWriter | undefined

function draft() { return { title: title.value, selectedWiki: selectedWiki.value, pageKey: pageKey.value,
  editBase: editBase.value, paragraphId: paragraphId.value, editText: editText.value, originalText: originalText.value } }
function persist() {
  if (loadingDraft || !writer) return Promise.resolve()
  return writer.write(draft())
}
watch([title, selectedWiki, pageKey, editBase, paragraphId, editText, originalText], () => { void persist() }, { flush: 'sync' })
function editorForPage(key: string) {
  const target = pages.value.find(p => p.page_key === key), first = rows(target?.human_paragraphs)[0]
  pageKey.value = key; editBase.value = text(revision.value.id)
  paragraphId.value = text(first?.id) || crypto.randomUUID()
  editText.value = text(first?.text); originalText.value = editText.value
}
async function list(cursor?: string) {
  const mine = requestGeneration
  try {
    const result = row(await props.query('wiki.list', { limit: 50, ...(cursor ? { cursor } : {}) }))
    if (alive && mine === requestGeneration) listing.value = result
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
}
async function loadHistory(cursor?: string) {
  const id = selectedWiki.value, mine = requestGeneration
  if (!id) return
  try {
    const result = row(await props.query('wiki.revisions', { wiki_id: id, limit: 50, ...(cursor ? { cursor } : {}) }))
    if (alive && mine === requestGeneration && id === selectedWiki.value) history.value = result
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
}
async function open(id: string, revisionId?: string, restore = false) {
  if (dirty.value && !restore) { error.value = '先保存人工编辑，或明确放弃当前编辑后再切换修订。'; return }
  const mine = ++requestGeneration
  error.value = ''
  try {
    const result = row(await props.query('wiki.get', { wiki_id: id, ...(revisionId ? { revision_id: revisionId } : {}) }))
    if (!alive || mine !== requestGeneration) return
    document.value = result; selectedWiki.value = id
    if (!restore || !pages.value.some(p => p.page_key === pageKey.value)) editorForPage(text(pages.value[0]?.page_key))
    await loadHistory()
  } catch (cause) { if (alive && mine === requestGeneration) error.value = workspaceError(cause) }
}
function selectPage(key: string) {
  if (dirty.value) { error.value = '先保存人工编辑，或明确放弃当前编辑后再切换页面。'; return }
  editorForPage(key); error.value = ''
}
function editParagraph(value?: Row) {
  if (dirty.value) { error.value = '先保存人工编辑，或明确放弃当前编辑后再切换段落。'; return }
  paragraphId.value = text(value?.id) || crypto.randomUUID(); editText.value = text(value?.text)
  originalText.value = editText.value; editBase.value = text(revision.value.id)
}
async function build(rebuild = false) {
  if (busy.value || props.pending || !props.ready || (rebuild && !selectedWiki.value)) return
  if (!props.sources.length || props.sources.length > 50) { error.value = '请在资源页选择 1 至 50 个已就绪的固定版本。'; return }
  if (dirty.value) { error.value = '先保存人工编辑后再构建新修订。'; return }
  const buildTitle = rebuild ? text(wiki.value.title) : title.value.trim()
  if (!buildTitle) return
  busy.value = true; error.value = ''
  try {
    await persist()
    if (!writer?.durable || !alive) throw new Error('cache_failure')
    const body = { title: buildTitle, sources: props.sources.map(({resource_id,source_version_id}) => ({resource_id,source_version_id})),
      max_pages: 4, max_output_tokens: 4096, execution_policy: 'local_only', allow_remote: false,
      ...(rebuild ? { base_revision_id: text(revision.value.id) } : {}) }
    const result = row(await props.command(rebuild ? 'wiki.rebuild' : 'wiki.create', { ...(rebuild ? { wiki_id: selectedWiki.value } : {}), body }))
    if (!alive) return
    const id = text(row(result.wiki).id)
    if (id) await open(id)
    await list()
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
  finally { if (alive) busy.value = false }
}
async function saveHuman() {
  if (!page.value || !dirty.value || !editText.value.trim() || props.pending || busy.value || !props.ready) return
  busy.value = true; error.value = ''
  const editing = editText.value, target = paragraphId.value
  try {
    await persist()
    if (!writer?.durable || !alive) throw new Error('cache_failure')
    const paragraphs = rows(page.value.human_paragraphs).map(p => ({ id: text(p.id), text: text(p.text) }))
    const found = paragraphs.findIndex(p => p.id === target)
    if (found >= 0) paragraphs[found] = { id: target, text: editing }
    else paragraphs.push({ id: target, text: editing })
    const result = row(await props.command('wiki.edit', { wiki_id: selectedWiki.value, page_key: pageKey.value,
      body: { base_revision_id: editBase.value, paragraphs } }))
    if (!alive) return
    const updated = row(result.revision)
    // An acceptance receipt without a committed revision is not an edit success.
    if (!updated.id) { error.value = '已取得受理回执，编辑草稿保留；请在任务完成后读取修订。'; return }
    document.value = result; editBase.value = text(updated.id); originalText.value = editing
    await persist(); await loadHistory(); await list()
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
  finally { if (alive) busy.value = false }
}
onMounted(async () => {
  try {
    const value = unwrap(await props.bridge.clientReadDraft({ connectionId: props.connectionId, key: 'wiki-editor' }))
    if (!alive) return
    const data = row(value?.value), connectionId = props.connectionId
    writer = new DraftWriter(value?.revision ?? 0, async (expectedRevision, value) => unwrap(await props.bridge.clientSaveDraft({ connectionId,
      key: 'wiki-editor', expectedRevision, value })).revision, state => {
      if (!alive) return
      saved.value = state.error ? '编辑草稿保存失败' : state.pending ? '编辑草稿保存中' : '编辑草稿已保存'
      if (state.error) error.value = workspaceError(state.error)
    })
    title.value = text(data.title); selectedWiki.value = text(data.selectedWiki); pageKey.value = text(data.pageKey)
    editBase.value = text(data.editBase); paragraphId.value = text(data.paragraphId)
    editText.value = text(data.editText); originalText.value = text(data.originalText)
    loadingDraft = false
    if (props.ready) { await list(); if (selectedWiki.value) await open(selectedWiki.value, editBase.value || undefined, true) }
  } catch (cause) { if (alive) { error.value = workspaceError(cause); loadingDraft = false } }
})
watch(() => props.ready, ready => { if (ready && !loadingDraft) { void list(); if (selectedWiki.value && !document.value) void open(selectedWiki.value, editBase.value || undefined, true) } })
onBeforeUnmount(() => { void persist(); alive = false; requestGeneration++ })
</script>

<template>
  <section class="local-wiki">
    <h1>Wiki</h1><p v-if="error" role="alert" class="error">{{ error }}</p>
    <label for="wiki-title">新 Wiki 主题</label><input id="wiki-title" v-model="title" maxlength="255" placeholder="用资料解释什么？" />
    <p class="muted">使用当前资源范围内 {{ sources.length }} 个固定版本，最多 4 页、4096 输出 token。内容与原文关系均需人工复核。</p>
    <div class="actions"><el-button :disabled="!ready || pending || busy || !title.trim() || !sources.length" :loading="busy" @click="build()">构建 Wiki</el-button><el-button text :disabled="!ready" @click="list()">刷新 Wiki 列表</el-button></div>
    <div class="wiki-list" aria-label="Wiki 列表"><button v-for="item in rows(listing.items)" :key="text(row(item.wiki).id)" :aria-current="row(item.wiki).id === selectedWiki ? 'page' : undefined" @click="open(text(row(item.wiki).id))">{{ row(item.wiki).title }} <small>{{ row(item.revision).page_count ?? '—' }} 页</small></button></div>
    <div v-if="listing.visible_total" class="actions"><span class="muted">本页 {{ rows(listing.items).length }} / 共 {{ listing.visible_total }} 项</span><el-button v-if="listing.has_more" text @click="list(text(listing.next_cursor))">下一页 Wiki</el-button></div>
    <template v-if="document">
      <h2>{{ wiki.title }}</h2><p class="mono">修订 {{ revision.id }}</p>
      <p v-if="oldRevision" class="error">正在查看历史修订；写入需基于当前修订，旧基准会被拒绝。</p>
      <p v-if="revision.stale" class="error">部分来源已经变化或不可用；这些页面需要重新核对。</p>
      <div class="actions"><el-button text :disabled="!ready || pending || busy" @click="open(selectedWiki)">读取当前修订</el-button><el-button :disabled="!ready || pending || busy || !!oldRevision" @click="build(true)">用当前资源生成新修订</el-button></div>
      <label class="history-label" for="wiki-history">历史修订</label><select id="wiki-history" :value="text(revision.id)" @change="open(selectedWiki, ($event.target as HTMLSelectElement).value)"><option v-for="item in rows(history.items)" :key="text(item.id)" :value="text(item.id)">{{ item.id }} · {{ item.created_at }}</option></select>
      <el-button v-if="history.has_more" text @click="loadHistory(text(history.next_cursor))">更多历史修订</el-button>
      <nav class="page-tabs" aria-label="Wiki 页面"><button v-for="item in pages" :key="text(item.page_key)" :aria-current="item.page_key === pageKey ? 'page' : undefined" @click="selectPage(text(item.page_key))">{{ item.title }}{{ item.stale ? ' · 待更新' : '' }}</button></nav>
      <article v-if="page" class="wiki-page">
        <h3>{{ page.title }}</h3>
        <section v-for="(part, index) in rows(page.generated_sections)" :key="index"><h4>{{ part.heading }}</h4><div v-for="sentence in rows(part.sentences)" :key="text(sentence.id)" class="wiki-sentence"><p>{{ sentence.text }}</p><button v-for="id in sentence.evidence_ids as string[]" :key="id" class="citation" @click="emit('evidence', id)">原始出处</button><small>生成内容 · 待复核</small></div></section>
        <section class="human-paragraphs"><h4>人工补充</h4><article v-for="item in rows(page.human_paragraphs)" :key="text(item.id)"><p>{{ item.text }}</p><small>人工内容 · 未核证</small><el-button text @click="editParagraph(item)">编辑此段</el-button></article><el-button text @click="editParagraph()">新增补充段落</el-button></section>
        <label for="wiki-human">人工编辑草稿</label><textarea id="wiki-human" v-model="editText" maxlength="10000" rows="5" />
        <p class="muted" aria-live="polite">{{ saved }}</p><div class="actions"><el-button :disabled="!dirty || !editText.trim() || !ready || busy || pending || !!oldRevision" @click="saveHuman">保存人工编辑为新修订</el-button><el-button v-if="dirty" text @click="editText = originalText; error = ''">放弃当前编辑</el-button></div>
      </article>
      <section v-if="rows(revision.relations).length" class="wiki-relations"><h3>原文关系 · 待复核</h3><article v-for="(relation, index) in rows(revision.relations)" :key="index"><p>{{ relation.predicate }}</p><button v-for="id in relation.evidence_ids as string[]" :key="id" class="citation" @click="emit('evidence', id)">关系出处</button></article></section>
      <details><summary>固定来源依赖（{{ rows(revision.dependency_manifest).length }}）</summary><article v-for="(dependency, index) in rows(revision.dependency_manifest)" :key="index" class="dependency"><code>{{ row(dependency.original).origin_node_id }} / {{ row(dependency.original).source_version_id }}</code><el-button text @click="emit('evidence', text(dependency.evidence_id))">核对原件</el-button></article></details>
      <p v-if="rows(revision.merge_conflicts).length" class="error">新旧页面存在合并冲突，请保留人工内容并逐项复核。</p>
    </template>
  </section>
</template>

<style scoped>
h1 { font-size: 22px; font-weight: 600; margin: 0 0 24px; }h2 { font-size: 18px; margin-top: 28px; }h3 { font-size: 16px; }h4 { font-size: 13px; }p { line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }label { display: block; font-size: 13px; margin: 16px 0 8px; }
input, textarea, select { box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid var(--ddp-line); border-radius: 4px; background: var(--ddp-panel); color: inherit; font: inherit; }textarea { resize: vertical; }.actions, .page-tabs { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }.muted, small { font-size: 12px; color: var(--ddp-ink-3); }.error { color: var(--ddp-cite); }.mono, code { font: 11px var(--f-mono); overflow-wrap: anywhere; }
.wiki-list { margin-top: 24px; }.wiki-list button { display: flex; justify-content: space-between; width: 100%; padding: 12px 0; text-align: left; border: 0; border-bottom: 1px solid var(--ddp-line); background: transparent; color: inherit; font: inherit; cursor: pointer; }.page-tabs { border-bottom: 1px solid var(--ddp-line); margin-top: 24px; }.page-tabs button { border: 0; background: transparent; color: inherit; padding: 12px 6px; cursor: pointer; }.page-tabs [aria-current=page] { border-bottom: 2px solid var(--ddp-ink); }.citation { background: transparent; border: 0; padding: 4px 12px 4px 0; color: var(--ddp-cite); cursor: pointer; }.wiki-sentence { margin-bottom: 20px; }.wiki-sentence p { margin-bottom: 4px; }.human-paragraphs, .wiki-relations { border-top: 1px solid var(--ddp-line); margin-top: 24px; }.dependency { margin-top: 12px; }details { border-top: 1px solid var(--ddp-line); padding-top: 16px; margin-top: 24px; }summary { cursor: pointer; font-size: 12px; }
</style>
