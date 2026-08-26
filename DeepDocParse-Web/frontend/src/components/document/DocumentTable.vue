<script setup lang="ts">
import StatusTag from '@/components/common/StatusTag.vue'
import type { DocumentInfo, DownloadFormat } from '@/types/api'
import { indexStatusOf, parseStatusOf } from '@/constants/status'

/**
 * 文档表格。
 *
 * 两个预留的扩展点（本轮已接上，供后续文档组织功能复用）：
 * - `selectable`：打开后出现多选列并向上抛 selection，批量操作走 #toolbar 插槽
 * - `#toolbar`：表格上方的操作条，父组件想放什么都行
 */
withDefaults(
  defineProps<{
    items: DocumentInfo[]
    loading?: boolean
    selectable?: boolean
  }>(),
  { selectable: false },
)

const emit = defineEmits<{
  (e: 'open', doc: DocumentInfo): void
  (e: 'download', doc: DocumentInfo, format: DownloadFormat): void
  (e: 'reparse', doc: DocumentInfo): void
  (e: 'reindex', doc: DocumentInfo): void
  (e: 'remove', doc: DocumentInfo): void
  (e: 'selection', docs: DocumentInfo[]): void
}>()

function onCommand(command: string, doc: DocumentInfo) {
  if (command.startsWith('dl:')) emit('download', doc, command.slice(3) as DownloadFormat)
  else if (command === 'reparse') emit('reparse', doc)
  else if (command === 'reindex') emit('reindex', doc)
  else if (command === 'remove') emit('remove', doc)
}
</script>

<template>
  <div class="wrap">
    <div v-if="$slots.toolbar" class="toolbar"><slot name="toolbar" /></div>

    <el-table
      :data="items"
      v-loading="loading"
      row-key="id"
      @row-dblclick="emit('open', $event)"
      @selection-change="emit('selection', $event)"
    >
      <el-table-column v-if="selectable" type="selection" width="44" />

      <el-table-column prop="filename" label="文件" min-width="220" show-overflow-tooltip />

      <!-- 语料是整个部署共享的（1b），文档库里会有别人传的东西 ——
           不显示上传者的话，用户根本分不清哪份是自己的。
           同一份文件可能被好几个人先后传过，那仍是同一份语料 -->
      <el-table-column label="上传者" width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.uploaders?.length">{{ row.uploaders.join('、') }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>

      <el-table-column label="解析" width="100">
        <template #default="{ row }">
          <el-tooltip :content="row.error" :disabled="!row.error">
            <StatusTag :meta="parseStatusOf(row.status)" />
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="问答" width="110">
        <template #default="{ row }">
          <el-tooltip :content="row.index_error" :disabled="!row.index_error">
            <StatusTag :meta="indexStatusOf(row.index_status)" />
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column prop="page_count" label="页数" width="76" align="right" class-name="ddp-num" />

      <!-- 等宽下 100px 只够到 100MB；1048576 KB 会溢出，留到 112px -->
      <el-table-column label="大小" width="112" align="right" class-name="ddp-num">
        <template #default="{ row }">{{ (row.size_bytes / 1024).toFixed(0) }} KB</template>
      </el-table-column>

      <!-- 等宽比比例字宽，170px 装不下"2026/8/22 20:49:20"，会被截成 20:49… -->
      <el-table-column label="提交时间" width="196">
        <template #default="{ row }">
          <span class="ddp-num">{{ new Date(row.created_at).toLocaleString('zh-CN') }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="row.status !== 'succeeded'"
                     @click="emit('open', row)">打开</el-button>
          <el-dropdown @command="onCommand($event, row)">
            <el-button link type="primary">更多<el-icon><component is="ArrowDown" /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="dl:md" :disabled="row.status !== 'succeeded'">
                  下载 Markdown
                </el-dropdown-item>
                <el-dropdown-item command="dl:json" :disabled="row.status !== 'succeeded'">
                  下载版面 JSON
                </el-dropdown-item>
                <el-dropdown-item command="dl:zip" :disabled="row.status !== 'succeeded'">
                  下载打包（含图片）
                </el-dropdown-item>
                <el-dropdown-item command="dl:source">下载原件</el-dropdown-item>
                <el-dropdown-item command="reparse" divided>换参数重解析</el-dropdown-item>
                <el-dropdown-item command="reindex" :disabled="row.status !== 'succeeded'">
                  重建索引
                </el-dropdown-item>
                <!-- 语料共享后文档库里会有别人传的东西，而删除是全站唯一还判权限的
                     动作。禁用而不是让它 403：用户点之前就该知道自己删不了 -->
                <el-dropdown-item command="remove" divided :disabled="!row.can_delete">
                  {{ row.can_delete ? '删除' : '删除（只有上传者能删）' }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>

      <template #empty>
        <el-empty description="还没有文档，拖一个文件上来试试" />
      </template>
    </el-table>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}
.muted { color: var(--el-text-color-placeholder); }
</style>
