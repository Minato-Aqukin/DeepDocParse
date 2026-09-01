<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import { computed } from 'vue'

import { FORMAT_OPTIONS, LEAF_TYPE_OPTIONS } from '@/constants/status'
import type { SchemaField, SchemaKind } from '@/types/api'

/**
 * 抽取 schema 的编辑器 —— 一个**扁平**的字段列表，故意不做树。
 *
 * 后端只收受限 JSON Schema：不支持嵌套 object、oneOf、$ref。
 * 那不是没来得及做——每加一层嵌套，检索次数与出处归属的歧义都乘一次
 * （见 ../DeepDocParse/docs/extract-format.md）。编辑器长成扁平的样子，
 * 是为了让这条边界在界面上就是显然的，而不是提交后才被后端拒绝。
 *
 * **description 是必填**：它同时是这个字段的检索 query。少了它只能拿字段名去检索，
 * `amt` 这种名字必然打偏，而失败会表现成"抽不到"，看起来像模型不行。
 * 所以这里把它标成必填并给出提示，而不是让人事后困惑。
 */
const fields = defineModel<SchemaField[]>('fields', { required: true })
const kind = defineModel<SchemaKind>('kind', { required: true })

const problems = computed(() => {
  const out: string[] = []
  const seen = new Set<string>()
  for (const field of fields.value) {
    const name = field.name.trim()
    if (!name) {
      out.push('有字段还没填名称')
      continue
    }
    if (seen.has(name)) out.push(`字段名重复：${name}`)
    seen.add(name)
    if (!field.description.trim()) out.push(`「${name}」缺少含义说明（它是这个字段的检索依据）`)
  }
  if (!fields.value.length) out.push('至少要有一个字段')
  return out
})
defineExpose({ problems })

function add() {
  fields.value = [
    ...fields.value,
    { name: '', type: 'string', description: '', format: '', enum: [], required: false },
  ]
}

function remove(index: number) {
  fields.value = fields.value.filter((_, i) => i !== index)
}
</script>

<template>
  <div class="schema-editor">
    <el-radio-group v-model="kind" size="small">
      <el-radio-button value="object">每份文档一条记录</el-radio-button>
      <el-radio-button value="array">每份文档多条记录（表格）</el-radio-button>
    </el-radio-group>
    <p class="hint">
      {{
        kind === 'object'
          ? '适合合同、发票、报告封面这类“一份文件对应一组字段”的场景。'
          : '适合从表格里抽多行记录。每一行的出处会落到它所在的那个块，表格跨页也能看出哪一行来自哪一页。'
      }}
    </p>

    <el-table :data="fields" size="small" class="field-table">
      <el-table-column label="字段名" width="160">
        <template #default="{ row }">
          <el-input v-model="row.name" placeholder="buyer_name" />
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <el-select v-model="row.type">
            <el-option v-for="o in LEAF_TYPE_OPTIONS" :key="o.value" :value="o.value"
                       :label="o.label" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="格式" width="150">
        <template #default="{ row }">
          <el-select v-model="row.format">
            <el-option v-for="o in FORMAT_OPTIONS" :key="o.value" :value="o.value"
                       :label="o.label" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column min-width="240">
        <template #header>
          <span>含义</span>
          <el-tooltip content="这段话就是该字段的检索依据，写清楚比字段名更重要">
            <span class="required-mark"> *</span>
          </el-tooltip>
        </template>
        <template #default="{ row }">
          <el-input v-model="row.description" placeholder="买方（甲方）单位全称" />
        </template>
      </el-table-column>
      <el-table-column label="必填" width="70" align="center">
        <template #default="{ row }">
          <el-checkbox v-model="row.required" />
        </template>
      </el-table-column>
      <el-table-column width="52" align="center">
        <template #default="{ $index }">
          <el-button :icon="Delete" text size="small" @click="remove($index)" />
        </template>
      </el-table-column>
    </el-table>

    <el-button :icon="Plus" size="small" @click="add">添加字段</el-button>

    <ul v-if="problems.length" class="problems">
      <li v-for="p in problems" :key="p">{{ p }}</li>
    </ul>
  </div>
</template>

<style scoped>
.schema-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hint {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.field-table :deep(.el-input__wrapper),
.field-table :deep(.el-select) {
  width: 100%;
}
/* 红只属于出处与出错（视觉规范准则一） —— 校验问题属于“出错” */
.required-mark {
  color: var(--ddp-cite);
}
.problems {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--ddp-cite);
}
</style>
