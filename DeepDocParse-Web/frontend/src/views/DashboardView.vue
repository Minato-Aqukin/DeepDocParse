<script setup lang="ts">
import { ElMessage, ElMessageBox, type UploadRequestOptions } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { TOKEN_KEY, api, http, type TaskInfo } from '@/api/client'

const router = useRouter()
const tasks = ref<TaskInfo[]>([])
const loading = ref(false)
let timer: number | undefined

const STATUS = {
  pending: { label: '排队中', type: 'info' },
  running: { label: '解析中', type: 'warning' },
  archiving: { label: '归档中', type: 'warning' },
  succeeded: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
} as const

// 有任务没落终态时才轮询，全完成就停下来
const hasActive = computed(() => tasks.value.some((t) => !['succeeded', 'failed'].includes(t.status)))

async function refresh() {
  loading.value = true
  try {
    tasks.value = (await api.listTasks()).data
  } finally {
    loading.value = false
  }
}

async function upload(options: UploadRequestOptions) {
  const form = new FormData()
  form.append('file', options.file)
  try {
    await http.post('/api/tasks/upload', form, {
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

async function remove(task: TaskInfo) {
  await ElMessageBox.confirm(`删除「${task.filename}」及其解析结果？`, '确认', { type: 'warning' })
  await api.deleteTask(task.id)
  await refresh()
}

function open(task: TaskInfo) {
  if (task.status !== 'succeeded') {
    ElMessage.info('解析尚未完成')
    return
  }
  router.push({ name: 'task', params: { id: task.id } })
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
    <span class="count">共 {{ tasks.length }} 个文档</span>
    <el-button :loading="loading" @click="refresh">刷新</el-button>
  </div>

  <el-table :data="tasks" v-loading="loading" @row-dblclick="open">
    <el-table-column prop="filename" label="文件" min-width="220" show-overflow-tooltip />
    <el-table-column label="状态" width="110">
      <template #default="{ row }">
        <el-tooltip :content="row.error" :disabled="!row.error">
          <el-tag :type="STATUS[row.status as keyof typeof STATUS].type" size="small">
            {{ STATUS[row.status as keyof typeof STATUS].label }}
          </el-tag>
        </el-tooltip>
      </template>
    </el-table-column>
    <el-table-column prop="page_count" label="页数" width="80" />
    <el-table-column label="大小" width="110">
      <template #default="{ row }">{{ (row.size_bytes / 1024).toFixed(0) }} KB</template>
    </el-table-column>
    <el-table-column label="提交时间" width="180">
      <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
    </el-table-column>
    <el-table-column label="操作" width="160">
      <template #default="{ row }">
        <el-button link type="primary" :disabled="row.status !== 'succeeded'" @click="open(row)">
          查看
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
  justify-content: space-between;
  margin: 16px 0 8px;
}
.count {
  color: var(--el-text-color-secondary);
}
</style>
