<script setup lang="ts">
import { ElMessage, ElMessageBox, type UploadRequestOptions } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { TOKEN_KEY, api, http, type DocumentInfo } from '@/api/client'

const router = useRouter()
const documents = ref<DocumentInfo[]>([])
const keyword = ref('')
const loading = ref(false)
let timer: number | undefined

const PARSE_STATUS = {
  pending: { label: '排队中', type: 'info' },
  running: { label: '解析中', type: 'warning' },
  archiving: { label: '归档中', type: 'warning' },
  succeeded: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
} as const

const INDEX_STATUS = {
  none: { label: '未索引', type: 'info' },
  pending: { label: '待索引', type: 'info' },
  indexing: { label: '索引中', type: 'warning' },
  ready: { label: '可问答', type: 'success' },
  failed: { label: '索引失败', type: 'danger' },
} as const

// 有任务没落定才轮询，全完成就停下来
const hasActive = computed(() =>
  documents.value.some(
    (d) => !['succeeded', 'failed'].includes(d.status) ||
      ['pending', 'indexing'].includes(d.index_status),
  ),
)

async function refresh() {
  loading.value = true
  try {
    documents.value = (await api.listDocuments({ q: keyword.value })).data
  } finally {
    loading.value = false
  }
}

/** 批量上传：并发 3，单个失败不影响整批。 */
async function upload(options: UploadRequestOptions) {
  const form = new FormData()
  form.append('file', options.file)
  try {
    await http.post('/api/documents', form, {
      headers: { Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}` },
      onUploadProgress: (e) => {
        if (e.total) options.onProgress({ percent: Math.round((e.loaded / e.total) * 100) } as never)
      },
    })
    ElMessage.success(`${options.file.name} 已提交解析`)
    await refresh()
  } catch (error) {
    options.onError(error as never)
  }
}

async function remove(document: DocumentInfo) {
  await ElMessageBox.confirm(`删除「${document.filename}」及其解析结果？`, '确认', {
    type: 'warning',
  })
  await api.deleteDocument(document.id)
  await refresh()
}

function open(document: DocumentInfo) {
  if (document.status !== 'succeeded') {
    ElMessage.info('解析尚未完成')
    return
  }
  router.push({ name: 'workbench', params: { id: document.id } })
}

function search() {
  if (keyword.value.trim()) router.push({ name: 'search', query: { q: keyword.value } })
}

onMounted(async () => {
  await refresh()
  timer = window.setInterval(() => hasActive.value && refresh(), 3000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <el-upload
    drag
    multiple
    :limit="0"
    :show-file-list="false"
    :http-request="upload"
    accept=".pdf,.png,.jpg,.jpeg,.webp,.docx,.pptx,.xlsx"
  >
    <div class="tip">把文件拖到这里，或<em>点击上传</em></div>
    <template #tip>
      <div class="hint">支持 PDF / 图片 / Office 文档；同一文件重复上传会直接复用已有结果</div>
    </template>
  </el-upload>

  <div class="bar">
    <el-input
      v-model="keyword"
      placeholder="按文件名筛选，回车进入全文检索"
      clearable
      class="search"
      @input="refresh"
      @keyup.enter="search"
    />
    <el-button :loading="loading" @click="refresh">刷新</el-button>
  </div>

  <el-table :data="documents" v-loading="loading" @row-dblclick="open">
    <el-table-column prop="filename" label="文件" min-width="220" show-overflow-tooltip />
    <el-table-column label="解析" width="100">
      <template #default="{ row }">
        <el-tooltip :content="row.error" :disabled="!row.error">
          <el-tag :type="PARSE_STATUS[row.status as keyof typeof PARSE_STATUS].type" size="small">
            {{ PARSE_STATUS[row.status as keyof typeof PARSE_STATUS].label }}
          </el-tag>
        </el-tooltip>
      </template>
    </el-table-column>
    <el-table-column label="问答" width="110">
      <template #default="{ row }">
        <el-tooltip :content="row.index_error" :disabled="!row.index_error">
          <el-tag :type="INDEX_STATUS[row.index_status as keyof typeof INDEX_STATUS].type"
                  size="small">
            {{ INDEX_STATUS[row.index_status as keyof typeof INDEX_STATUS].label }}
          </el-tag>
        </el-tooltip>
      </template>
    </el-table-column>
    <el-table-column prop="page_count" label="页数" width="80" />
    <el-table-column label="大小" width="100">
      <template #default="{ row }">{{ (row.size_bytes / 1024).toFixed(0) }} KB</template>
    </el-table-column>
    <el-table-column label="提交时间" width="170">
      <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
    </el-table-column>
    <el-table-column label="操作" width="150">
      <template #default="{ row }">
        <el-button link type="primary" :disabled="row.status !== 'succeeded'" @click="open(row)">
          打开
        </el-button>
        <el-button link type="danger" @click="remove(row)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.tip {
  padding: 28px 0;
  color: var(--el-text-color-regular);
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-top: 6px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0 8px;
}
.search {
  max-width: 420px;
}
</style>
