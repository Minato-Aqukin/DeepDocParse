<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { documentsApi } from '@/api'
import { documentContext, selectedResource, selectedVersion } from '@/api/resource-context'
import StatusTag from '@/components/common/StatusTag.vue'
import EngineOptionsForm from '@/components/engine/EngineOptionsForm.vue'
import { pruneOptions } from '@/constants/engines'
import { parseStatusOf } from '@/constants/status'
import type { DocumentInfo, EngineChoice, JobInfo } from '@/types/api'
import { loadEnginePreference } from '@/utils/preferences'
import { useAuthStore } from '@/stores/auth'
import { approvedPlanLabel, onLocalSource, onReadOnlySource } from '@/platform/desktop'

/**
 * 解析历史按资源隔离。选择另一个解析会追加不可变资源版本，
 * 不重写原版本或旧出处；问答与引用继续使用各自固定的 ParseJob。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const document = ref<DocumentInfo>()
const jobs = ref<JobInfo[]>([])
const loading = ref(false)
/** 轮询中的 transient 失败落在这里行内展示，不杀死轮询。 */
const errorText = ref('')
const dialog = ref(false)
const choice = ref<EngineChoice>(loadEnginePreference())
const reparsing = ref(false)
let loadGeneration = 0
/** 至少成功加载过一次（失败重试时用"上次已知状态"决策要不要继续轮询）。 */
let loadedOnce = false
let pollTimer: number | undefined

function stopPolling() {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  pollTimer = undefined
}

async function load(quiet = false) {
  stopPolling()
  const generation = ++loadGeneration
  const id = String(route.params.id)
  const url = `/api/documents/${id}`
  const context = {
    resource_id: selectedResource(url, location.hash),
    version_id: selectedVersion(url, location.hash),
  }
  if (!quiet) loading.value = true
  // 单次失败也必须重排轮询：一次 500 就永久停掉，用户只能手动刷新。
  // 错误落到 errorText 行内展示（loading 只在初次 load 时闪，不干扰轮询）。
  let failed: unknown = null
  try {
    const [detail, history] = await Promise.all([
      documentsApi.get(id, context), documentsApi.listJobs(id, context),
    ])
    if (generation !== loadGeneration) return
    document.value = detail.data
    jobs.value = history.data
  } catch (cause) {
    if (generation !== loadGeneration) return
    failed = cause
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
  if (generation !== loadGeneration) return
  if (failed) {
    errorText.value = failed instanceof Error ? failed.message : String(failed)
    // 失败时按"上次已知的状态"决定要不要继续：上次还在跑（或一次都没成功过、
    // 状态未知）就继续轮询等恢复；已经落定的手动刷新失败不另起一个永久轮询。
    // "还在动"以契约为准：archiving 也是 active，手写 pending/running 会漏掉它。
    const active = !loadedOnce || jobs.value.some((job) => parseStatusOf(job.status).active)
    if (active) pollTimer = window.setTimeout(() => { void load(true) }, 2000)
    return
  }
  errorText.value = ''
  loadedOnce = true
  if (jobs.value.some((job) => parseStatusOf(job.status).active)) {
    pollTimer = window.setTimeout(() => { void load(true) }, 2000)
  }
}

async function reparse() {
  if (!document.value || reparsing.value) return
  const path = route.fullPath
  const context = documentContext(document.value)
  reparsing.value = true
  try {
    await documentsApi.reparse(String(route.params.id), {
      engine: choice.value.engine,
      options: pruneOptions(choice.value.options),
    }, context)
    if (route.fullPath !== path) return
    dialog.value = false
    ElMessage.success('已提交重新解析')
    await load()
  } finally {
    reparsing.value = false
  }
}

async function makeCurrent(job: JobInfo) {
  if (!document.value) return
  const documentId = String(route.params.id)
  const path = route.fullPath
  const context = documentContext(document.value)
  const { data } = await documentsApi.setCurrentJob(documentId, job.id, context)
  ElMessage.success('已切换当前版本，索引将重建')
  if (route.fullPath !== path) return
  await router.replace({ query: { ...route.query, ...documentContext(data) } })
  if (route.fullPath === path) await load()
}

watch(() => [route.params.id, route.query.resource_id, route.query.version_id], () => {
  document.value = undefined
  jobs.value = []
  dialog.value = false
  errorText.value = ''
  loadedOnce = false
  void load()
}, { immediate: true })

onBeforeUnmount(() => {
  loadGeneration++
  stopPolling()
})
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <el-button link :disabled="!document" @click="router.push({ name: 'workbench',
          params: { id: route.params.id }, query: document
            ? { ...documentContext(document), job: document.current_job_id ?? undefined } : {} })">← 工作台</el-button>
        <span class="name">{{ document?.filename }}</span>
      </div>
      <el-button :loading="loading" @click="load()">刷新</el-button>
      <el-button v-if="!onLocalSource" type="primary" :disabled="!document || !auth.canUpload || !document.can_delete"
                 @click="dialog = true">换参数重新解析</el-button>
    </div>
    <p v-if="onReadOnlySource" class="readonly-hint" role="note">{{ approvedPlanLabel() }}</p>
    <el-alert v-if="errorText" type="warning" :closable="false" class="poll-error" role="status"
              :title="`版本状态刷新失败，正在重试：${errorText}`" />
    <el-table :data="jobs" v-loading="loading">
      <el-table-column label="版本" width="200">
        <template #default="{ row }">
          <code>v{{ row.document_version }} · {{ row.id.slice(0, 8) }}</code>
          <StatusTag v-if="row.is_current" label="当前" type="success" class="tag" />
        </template>
      </el-table-column>
      <el-table-column prop="engine" label="引擎" width="100" />
      <el-table-column label="参数" min-width="200">
        <template #default="{ row }">
          <code>{{ Object.keys(row.options).length ? JSON.stringify(row.options) : '默认' }}</code>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <StatusTag :meta="parseStatusOf(row.status)" />
        </template>
      </el-table-column>
      <el-table-column prop="page_count" label="页数" width="80" align="right" class-name="ddp-num" />
      <el-table-column label="完成时间" width="206">
        <template #default="{ row }">
          <span class="ddp-num">{{ row.archived_at ? new Date(row.archived_at).toLocaleString('zh-CN') : '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="!onLocalSource" label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary"
                     :disabled="row.is_current || row.status !== 'succeeded' || !auth.canUpload || !document?.can_delete"
                     @click="makeCurrent(row)">设为当前</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="换参数重新解析" width="460px">
      <EngineOptionsForm v-model="choice" />
      <el-alert type="info" :closable="false"
                title="同一组参数已经解析过时会直接复用，不会重复消耗额度" />
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="reparsing" @click="reparse">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.name {
  font-size: 16px;
  font-weight: 600;
  margin-left: 8px;
}
.tag {
  margin-left: 6px;
}
.readonly-hint { color: var(--ddp-ink-3); font-size: 13px; }
.poll-error { margin-bottom: 12px; }
</style>
