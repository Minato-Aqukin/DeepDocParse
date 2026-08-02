<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { documentsApi, downloadAs } from '@/api'
import StatCard from '@/components/common/StatCard.vue'
import DocumentFilters from '@/components/document/DocumentFilters.vue'
import DocumentTable from '@/components/document/DocumentTable.vue'
import UploadDialog from '@/components/document/UploadDialog.vue'
import { usePolling } from '@/composables/usePolling'
import { useDocumentsStore } from '@/stores/documents'
import type { DocumentInfo, DownloadFormat } from '@/types/api'
import type { DocumentFilters as Filters } from '@/stores/documents'

const router = useRouter()
const store = useDocumentsStore()

const uploadVisible = ref(false)
const selected = ref<DocumentInfo[]>([])

// 有任务在动才轮询，全落定就停（判断依据在 constants/status.ts 的 active 标记）
const polling = usePolling(() => store.refresh(), () => store.hasActive)

async function reload() {
  await store.refresh()
  polling.start()
}

function open(doc: DocumentInfo) {
  if (doc.status !== 'succeeded') return ElMessage.info('解析尚未完成')
  router.push({ name: 'workbench', params: { id: doc.id } })
}

async function download(doc: DocumentInfo, format: DownloadFormat) {
  await downloadAs(documentsApi.downloadUrl(doc.id, format), doc.filename)
}

async function reindex(doc: DocumentInfo) {
  await documentsApi.reindex(doc.id)
  ElMessage.success('已排队重建索引')
  await reload()
}

async function remove(doc: DocumentInfo) {
  await ElMessageBox.confirm(`删除「${doc.filename}」及其解析结果？`, '确认', { type: 'warning' })
  await documentsApi.remove(doc.id)
  await reload()
}

async function removeSelected() {
  await ElMessageBox.confirm(`删除选中的 ${selected.value.length} 份文档？`, '确认', {
    type: 'warning',
  })
  for (const doc of selected.value) await documentsApi.remove(doc.id)
  selected.value = []
  await reload()
}

async function onFilterChange(patch: Partial<Filters>) {
  await store.applyFilters(patch)
}

onMounted(reload)
</script>

<template>
  <div class="page">
    <el-row :gutter="12" class="stats">
      <el-col :span="8">
        <StatCard label="文档" :value="store.stats.documents" icon="Files" />
      </el-col>
      <el-col :span="8">
        <StatCard label="已解析页数" :value="store.stats.pages" icon="Document" />
      </el-col>
      <el-col :span="8">
        <StatCard label="可问答" :value="store.stats.askable" hint="索引已就绪的文档"
                  icon="ChatDotRound" />
      </el-col>
    </el-row>

    <div class="bar">
      <DocumentFilters
        :model-value="store.filters"
        @change="onFilterChange"
        @search="router.push({ name: 'search', query: { q: store.filters.q } })"
      />
      <div class="actions">
        <el-button :loading="store.loading" @click="reload">刷新</el-button>
        <el-button type="primary" @click="uploadVisible = true">
          <el-icon><component is="Upload" /></el-icon> 上传
        </el-button>
      </div>
    </div>

    <DocumentTable
      :items="store.items"
      :loading="store.loading"
      selectable
      @open="open"
      @download="download"
      @reparse="router.push({ name: 'versions', params: { id: $event.id } })"
      @reindex="reindex"
      @remove="remove"
      @selection="selected = $event"
    >
      <template v-if="selected.length" #toolbar>
        <span class="picked">已选 {{ selected.length }} 份</span>
        <el-button link type="danger" @click="removeSelected">批量删除</el-button>
      </template>
    </DocumentTable>

    <div class="pager">
      <el-button :disabled="store.page === 1 || store.loading" @click="store.goPage(store.page - 1)">
        上一页
      </el-button>
      <span class="page-no">第 {{ store.page }} 页</span>
      <el-button :disabled="!store.hasMore || store.loading" @click="store.goPage(store.page + 1)">
        下一页
      </el-button>
    </div>

    <UploadDialog v-model="uploadVisible" @uploaded="reload" />
  </div>
</template>

<style scoped>
.stats {
  margin-bottom: 12px;
}
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.actions {
  display: flex;
  gap: 8px;
}
.picked {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
}
.page-no {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
