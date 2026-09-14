<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { downloadAs } from '@/api/http'
import { resourcesApi, type Resource, type ResourceVersion } from '@/api/resources'
import UploadDialog from '@/components/document/UploadDialog.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const scope = ref<'mine' | 'site_public'>('mine')
const resources = ref<Resource[]>([])
const loading = ref(false)
const error = ref('')
const offset = ref(0)
const more = ref(false)
const upload = ref(false)
let generation = 0
const labels = { private: '私有', draft: '草稿', published: '已公开', withdrawn: '已撤下' }

async function load() {
  const current = ++generation
  loading.value = true
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
}
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
  catch (cause) { error.value = `Bundle 导出失败：${String(cause)}` }
}
function page(delta: number) { offset.value = Math.max(0, offset.value + delta * 50); void load() }
watch(scope, () => { offset.value = 0; resources.value = []; void load() })
onMounted(load)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <section class="resources">
    <header><div><h1>资源库</h1><p>每份资源保留自己的归属、固定版本和原始出处。</p></div>
      <el-button v-if="auth.canUpload" type="primary" @click="upload = true">上传资料</el-button>
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
        <span class="digest" :title="version.source_digest">{{ version.source_digest ? version.source_digest.slice(0, 16) : '原文字节未验证' }}</span>
        <el-button link @click="bundle(resource, version)">导出 Bundle</el-button>
      </div>
      <footer v-if="scope === 'mine'"><el-button link @click="publication(resource)">{{ resource.publication === 'published' ? '设为私有' : '公开到本站' }}</el-button><el-button link type="danger" @click="remove(resource)">删除资源</el-button></footer>
    </article>
    <div class="pagination"><el-button :disabled="offset === 0 || loading" @click="page(-1)">上一页</el-button><span class="number">{{ offset / 50 + 1 }}</span><el-button :disabled="!more || loading" @click="page(1)">下一页</el-button></div>
    <UploadDialog v-model="upload" @uploaded="load" />
  </section>
</template>

<style scoped>
.resources { max-width: 1120px; margin: auto; }
header, nav, .resource-heading, .version, footer, .pagination { display: flex; align-items: center; gap: 16px; }
header { justify-content: space-between; margin-bottom: 24px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 0; }
p { color: var(--ink-2); margin: 8px 0; }
article { padding: 24px 0; border-bottom: 1px solid var(--line); }
.state { color: var(--ink-2); font-size: 13px; }
.identity, .number, .digest { font-family: var(--f-mono); font-size: 12px; font-variant-numeric: tabular-nums; }
.identity { overflow-wrap: anywhere; }
.version { min-height: 44px; flex-wrap: wrap; }
.title { border: 0; background: transparent; color: var(--ink); text-align: left; cursor: pointer; font: inherit; min-height: 44px; }
.title:hover { text-decoration: underline; }
.digest { margin-left: auto; color: var(--ink-2); }
footer { margin-top: 12px; }
.pagination { justify-content: flex-end; margin-top: 24px; }
.error { border-left: 2px solid var(--danger); padding-left: 12px; color: var(--danger); }
@media (max-width: 640px) { header { align-items: start; flex-direction: column; } .digest { margin-left: 0; } }
</style>
