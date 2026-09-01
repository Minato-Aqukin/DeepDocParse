<script setup lang="ts">
import { computed, ref } from 'vue'

import CitationChip from '@/components/ask/CitationChip.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { degradedLabelOf, fieldStatusOf, similarityText } from '@/constants/status'
import type { ExtractionItem, FieldResult } from '@/types/api'

/**
 * 抽取结果表格：**行 = 文档 × 记录序号，列 = schema 字段**。
 *
 * 这个组件的全部意义在于：**每个单元格都点得开它的出处**。
 * 抽取产品普遍只给"字段 + 置信度"，指不回原文；这里点一下就能看到
 * 页码、bbox 与从原件裁出来的那块图 —— 出处从 chunk 级下沉到了字段级。
 *
 * 三态必须一眼分得开（constants/status.ts 里写了理由）：
 *   已抽取     值 + 相关度
 *   文档中未提及  一句明确的话，**不是空白**（空白让人以为是没渲染出来）
 *   抽取失败    红字 + 原因，**绝不显示成"未提及"**（那是把故障伪装成事实）
 */
const props = defineProps<{
  items: ExtractionItem[]
  fieldNames: string[]
  /** 出处截图的取回函数：截图受 JWT 保护，<img src> 直接取不到 */
  cropUrlOf?: (documentId: string, cropUrl: string) => string | undefined
}>()

const emit = defineEmits<{ (e: 'locate', documentId: string, citation: unknown): void }>()

const active = ref<{ item: ExtractionItem; name: string } | null>(null)
// el-drawer 的 v-model 要一个布尔。直接把 active 绑上去能跑，但那是靠"对象为真"
// 的巧合 —— 关闭时它会把 active 写成 false，类型就此崩坏。用一个显式的开关
const drawerOpen = computed({
  get: () => active.value !== null,
  set: (open: boolean) => {
    if (!open) active.value = null
  },
})

const activeField = computed<FieldResult | null>(() => {
  if (!active.value) return null
  return active.value.item.fields[active.value.name] ?? null
})

function cellOf(item: ExtractionItem, name: string): FieldResult | undefined {
  return item.fields[name]
}

function display(cell: FieldResult | undefined): string {
  if (!cell) return '—'
  if (cell.status !== 'found') return fieldStatusOf(cell.status).label
  if (cell.value === null) return '—'
  if (typeof cell.value === 'boolean') return cell.value ? '是' : '否'
  return String(cell.value)
}

function open(item: ExtractionItem, name: string) {
  const cell = cellOf(item, name)
  // 没有出处就没什么可看的（未提及 / 失败），不弹一个空抽屉
  if (cell?.citations?.length) active.value = { item, name }
}
</script>

<template>
  <div class="record-table">
    <el-table :data="props.items" size="small" border stripe>
      <el-table-column label="文档" min-width="180" fixed>
        <template #default="{ row }">
          <div class="doc-cell">
            <span class="filename" :title="row.filename">{{ row.filename }}</span>
            <StatusTag v-if="row.status !== 'ok'" :label="row.status === 'partial' ? '部分' : '失败'"
                       :type="row.status === 'partial' ? 'warning' : 'danger'" />
          </div>
          <div v-if="row.degraded" class="degraded">{{ degradedLabelOf(row.degraded) }}</div>
          <div v-if="row.error" class="degraded">{{ row.error }}</div>
        </template>
      </el-table-column>

      <el-table-column v-if="props.items.some((i) => i.record_index > 0)" label="#" width="52">
        <template #default="{ row }">{{ row.record_index + 1 }}</template>
      </el-table-column>

      <el-table-column v-for="name in props.fieldNames" :key="name" :label="name" min-width="150">
        <template #default="{ row }">
          <button
            type="button"
            class="cell"
            :class="[
              `is-${cellOf(row, name)?.status ?? 'not_found'}`,
              { clickable: (cellOf(row, name)?.citations?.length ?? 0) > 0 },
            ]"
            @click="open(row, name)"
          >
            <span class="value">{{ display(cellOf(row, name)) }}</span>
            <span v-if="cellOf(row, name)?.status === 'found'" class="meta">
              <template v-if="cellOf(row, name)!.citations.length">
                p.{{ cellOf(row, name)!.citations[0]!.page_idx + 1 }}
              </template>
              <template v-if="similarityText(cellOf(row, name)!.confidence?.top_similarity)">
                · {{ similarityText(cellOf(row, name)!.confidence.top_similarity) }}
              </template>
              <template v-if="cellOf(row, name)!.verified"> · 已核对</template>
            </span>
          </button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawerOpen" :with-header="false" size="420px">
      <template v-if="active && activeField">
        <h3 class="drawer-title">{{ active.name }}</h3>
        <p class="drawer-value">{{ display(activeField) }}</p>
        <div class="drawer-tags">
          <StatusTag :meta="fieldStatusOf(activeField.status)" />
          <StatusTag v-if="activeField.verified" label="已视觉核对" type="success" />
          <StatusTag v-if="activeField.degraded"
                     :label="degradedLabelOf(activeField.degraded) ?? ''" type="warning" />
        </div>
        <h4 class="drawer-sub">出处</h4>
        <CitationChip
          v-for="(citation, i) in activeField.citations"
          :key="i"
          :citation="citation as never"
          :index="i + 1"
          :crop-url="citation.crop_url
            ? props.cropUrlOf?.(active.item.document_id, citation.crop_url)
            : undefined"
          :warn-below="activeField.confidence?.warn_below"
          @locate="emit('locate', active!.item.document_id, citation)"
        />
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.record-table {
  overflow-x: auto;
}
.doc-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.filename {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.degraded {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 2px 0;
  border: 0;
  background: none;
  font: inherit;
  text-align: left;
  color: var(--el-text-color-primary);
  cursor: default;
}
.cell.clickable {
  cursor: pointer;
}
.cell.clickable:hover .value {
  text-decoration: underline;
}
/* 未提及是一个**结论**，不是缺数据：用次要色而不是留白 */
.cell.is-not_found .value {
  color: var(--el-text-color-secondary);
  font-style: italic;
}
/* 红只属于出处与出错（视觉规范准则一） */
.cell.is-error .value {
  color: var(--ddp-cite);
}
.value {
  overflow-wrap: anywhere;
}
.meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.drawer-title {
  margin: 0 0 4px;
  font-size: 15px;
}
.drawer-value {
  margin: 0 0 10px;
  overflow-wrap: anywhere;
}
.drawer-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
}
.drawer-sub {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>
