<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { documentsApi } from '@/api'
import EngineOptionsForm from '@/components/engine/EngineOptionsForm.vue'
import { pruneOptions } from '@/constants/engines'
import { parseStatusOf } from '@/constants/status'
import type { DocumentInfo, EngineChoice, JobInfo } from '@/types/api'
import { loadEnginePreference } from '@/utils/preferences'

/**
 * 解析版本：同一份文件换引擎/参数会产生新的 ParseJob，两个版本并存。
 * 切换"当前版本"会连带重建索引——否则问答会引用到旧版本的块。
 */
const route = useRoute()
const router = useRouter()

const document = ref<DocumentInfo>()
const jobs = ref<JobInfo[]>([])
const loading = ref(false)
const dialog = ref(false)
const choice = ref<EngineChoice>(loadEnginePreference())

async function load() {
  const id = String(route.params.id)
  loading.value = true
  try {
    document.value = (await documentsApi.get(id)).data
    jobs.value = (await documentsApi.listJobs(id)).data
  } finally {
    loading.value = false
  }
}

async function reparse() {
  await documentsApi.reparse(String(route.params.id), {
    engine: choice.value.engine,
    options: pruneOptions(choice.value.options),
  })
  dialog.value = false
  ElMessage.success('已提交重新解析')
  await load()
}

async function makeCurrent(job: JobInfo) {
  await documentsApi.setCurrentJob(String(route.params.id), job.id)
  ElMessage.success('已切换当前版本，索引将重建')
  await load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <el-button link @click="router.push(`/documents/${route.params.id}`)">← 工作台</el-button>
        <span class="name">{{ document?.filename }}</span>
      </div>
      <el-button type="primary" @click="dialog = true">换参数重新解析</el-button>
    </div>

    <el-table :data="jobs" v-loading="loading">
      <el-table-column label="版本" width="200">
        <template #default="{ row }">
          <code>{{ row.id.slice(0, 8) }}</code>
          <el-tag v-if="row.is_current" size="small" type="success" class="tag">当前</el-tag>
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
          <el-tag :type="parseStatusOf(row.status).type" size="small">
            {{ parseStatusOf(row.status).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="page_count" label="页数" width="80" />
      <el-table-column label="完成时间" width="180">
        <template #default="{ row }">
          {{ row.archived_at ? new Date(row.archived_at).toLocaleString() : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="row.is_current || row.status !== 'succeeded'"
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
        <el-button type="primary" @click="reparse">提交</el-button>
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
</style>
