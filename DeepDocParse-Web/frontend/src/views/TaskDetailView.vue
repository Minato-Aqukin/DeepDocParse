<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { api, http, type TaskInfo } from '@/api/client'
import { renderMarkdown, resolveAuthedImages } from '@/utils/markdown'

const route = useRoute()
const task = ref<TaskInfo>()
const html = ref('')
const rawMarkdown = ref('')
const sourcePath = ref('')
const view = ref<'render' | 'source'>('render')
const body = ref<HTMLElement>()
const loading = ref(true)
let revoke: (() => void) | undefined

async function load() {
  const id = String(route.params.id)
  loading.value = true
  try {
    task.value = (await api.getTask(id)).data
    const [result, source] = await Promise.all([
      api.getResult(id),
      http.get<{ path: string }>(`/api/tasks/${id}/source-url`).catch(() => null),
    ])
    rawMarkdown.value = result.data.markdown
    html.value = renderMarkdown(result.data.markdown)
    sourcePath.value = source?.data.path || ''
    await nextTick()
    if (body.value) {
      revoke?.()
      revoke = await resolveAuthedImages(body.value)
    }
  } finally {
    loading.value = false
  }
}

function download(format: 'md' | 'json' | 'source') {
  // 下载走 axios（要带 JWT），拿到 blob 再触发浏览器保存
  http.get(api.downloadUrl(String(route.params.id), format), { responseType: 'blob' }).then(
    ({ data, headers }) => {
      const url = URL.createObjectURL(data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download =
        /filename="([^"]+)"/.exec(String(headers['content-disposition'] || ''))?.[1] || 'download'
      a.click()
      URL.revokeObjectURL(url)
    },
  )
}

watch(() => route.params.id, load, { immediate: true })
onUnmounted(() => revoke?.())
</script>

<template>
  <div class="head">
    <div>
      <span class="name">{{ task?.filename }}</span>
      <el-tag size="small" type="info">{{ task?.page_count }} 页</el-tag>
    </div>
    <div class="actions">
      <el-radio-group v-model="view" size="small">
        <el-radio-button value="render">渲染</el-radio-button>
        <el-radio-button value="source">Markdown 源码</el-radio-button>
      </el-radio-group>
      <el-button size="small" @click="download('md')">下载 .md</el-button>
      <el-button size="small" @click="download('json')">下载版面 JSON</el-button>
      <el-button size="small" @click="download('source')">下载原件</el-button>
    </div>
  </div>

  <el-row :gutter="12" class="panes">
    <el-col :span="10">
      <el-card shadow="never" class="pane">
        <template #header>原文</template>
        <iframe v-if="sourcePath" :src="sourcePath" class="viewer" />
        <el-empty v-else description="原件不可预览" />
      </el-card>
    </el-col>
    <el-col :span="14">
      <el-card shadow="never" class="pane" v-loading="loading">
        <template #header>解析结果</template>
        <!-- v-html 的内容已在 renderMarkdown 里过 DOMPurify（文档内容是不可信输入） -->
        <div v-if="view === 'render'" ref="body" class="markdown-body" v-html="html" />
        <pre v-else class="source">{{ rawMarkdown }}</pre>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  gap: 12px;
  flex-wrap: wrap;
}
.name {
  font-size: 16px;
  font-weight: 600;
  margin-right: 8px;
}
.actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.panes {
  height: calc(100vh - 190px);
}
.pane {
  height: 100%;
  overflow: auto;
}
.viewer {
  width: 100%;
  height: calc(100vh - 300px);
  border: none;
}
.source {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
}
</style>
