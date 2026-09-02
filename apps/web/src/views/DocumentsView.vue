<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { documentsApi, downloadAs } from '@/api'
import StatCard from '@/components/common/StatCard.vue'
import DocumentFilters from '@/components/document/DocumentFilters.vue'
import DocumentTable from '@/components/document/DocumentTable.vue'
import UploadDialog from '@/components/document/UploadDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { usePolling } from '@/composables/usePolling'
import { useDocumentsStore } from '@/stores/documents'
import type { DocumentInfo, DownloadFormat } from '@/types/api'
import type { DocumentFilters as Filters } from '@/stores/documents'
import { validateAndReindex } from '@/utils/reindex'

const router = useRouter()
const store = useDocumentsStore()

const auth = useAuthStore()
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
  await downloadAs(documentsApi.exportUrl(doc.id, format), doc.filename)
}

async function reindex(doc: DocumentInfo) {
  await validateAndReindex(doc.id)
  ElMessage.success('已排队重建索引')
  await reload()
}

async function remove(doc: DocumentInfo) {
  // 说清楚这是从**整个服务器的语料**里移除，不是"删掉我的那份副本" ——
  // 语料共享之后这两件事已经不是一回事了
  await ElMessageBox.confirm(
    `从本服务器语料中移除「${doc.filename}」及其解析结果？其他人也将不再看得到它。`,
    '确认', { type: 'warning' })
  await documentsApi.remove(doc.id)
  await reload()
}

async function removeSelected() {
  await ElMessageBox.confirm(
    `从本服务器语料中移除选中的 ${selected.value.length} 份文档？其他人也将不再看得到它们。`,
    '确认', { type: 'warning' })
  // **一份删不掉不能拖垮其余的。** 语料共享之后（plan.md §2 已定 2）文档库里
  // 会有别人传的东西，而删除是全站唯一还判权限的动作 —— 批量选中里混进一份
  // 别人的会 403。以前那种 `for ... await` 一抛就整个中断，表现是
  // "点了批量删除，删了一半，只弹一句看不懂的错" —— 剩下哪些没删、为什么，
  // 用户完全不知道。现在逐个记结果，最后一次性说清。
  const failed: string[] = []
  for (const doc of selected.value) {
    try {
      await documentsApi.remove(doc.id)
    } catch {
      // http 拦截器已经弹过每条的具体原因，这里只统计
      failed.push(doc.filename)
    }
  }
  if (failed.length) {
    ElMessage.warning(
      `${selected.value.length - failed.length} 份已删除；${failed.length} 份没能删除` +
      `（只有上传者或管理员能删）：${failed.slice(0, 3).join('、')}` +
      (failed.length > 3 ? ` 等 ${failed.length} 份` : ''),
    )
  }
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
        <!-- 按**能力**显示，不按角色名。只读成员看不到这个按钮 ——
             让他点进去再吃一个 403 也能工作，但那会让人以为是自己操作错了 -->
        <el-tooltip v-if="!auth.canUpload" content="只读成员不能上传文档">
          <span><el-button type="primary" disabled>
            <el-icon><component is="Upload" /></el-icon> 上传
          </el-button></span>
        </el-tooltip>
        <el-button v-else type="primary" @click="uploadVisible = true">
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
/* 三张卡等高：只有第三张带 hint，行内不拉伸的话高度会差一截。
   以前有投影盖着看不出来，改成 1px 描边的平面之后一眼就能看到参差。 */
.stats :deep(.el-col) {
  display: flex;
}
.stats :deep(.el-card) {
  width: 100%;
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
