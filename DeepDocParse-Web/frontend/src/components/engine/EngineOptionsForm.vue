<script setup lang="ts">
import { computed, watch } from 'vue'

import { ENGINES, defaultOptions, schemaOf } from '@/constants/engines'
import type { EngineChoice } from '@/types/api'

/**
 * 解析参数表单：按 constants/engines.ts 的 schema 渲染。
 *
 * 上传对话框与"换参数重解析"共用它，两处口径永远一致；
 * 加引擎或加参数只改 schema，这个组件不用动。
 */
const model = defineModel<EngineChoice>({ required: true })
withDefaults(defineProps<{ labelWidth?: string }>(), { labelWidth: '96px' })

const schema = computed(() => schemaOf(model.value.engine))

// 换引擎时把参数重置成新引擎的默认值——旧引擎的参数对新引擎没有意义
watch(
  () => model.value.engine,
  (engine, previous) => {
    if (previous !== undefined) model.value = { engine, options: defaultOptions(engine) }
  },
)

function setOption(key: string, value: unknown) {
  model.value = { ...model.value, options: { ...model.value.options, [key]: value } }
}
</script>

<template>
  <el-form :label-width="labelWidth" label-position="left">
    <el-form-item label="解析引擎">
      <el-select v-model="model.engine" class="control">
        <el-option v-for="e in ENGINES" :key="e.engine" :value="e.engine" :label="e.label" />
      </el-select>
      <div v-if="schema?.description" class="hint">{{ schema.description }}</div>
    </el-form-item>

    <el-form-item v-for="field in schema?.fields ?? []" :key="field.key" :label="field.label">
      <el-select
        v-if="field.type === 'select'"
        :model-value="model.options[field.key]"
        class="control"
        @update:model-value="setOption(field.key, $event)"
      >
        <el-option v-for="c in field.choices" :key="c.value" :value="c.value" :label="c.label" />
      </el-select>

      <el-switch
        v-else-if="field.type === 'switch'"
        :model-value="Boolean(model.options[field.key])"
        @update:model-value="setOption(field.key, $event)"
      />

      <el-input-number
        v-else-if="field.type === 'number'"
        :model-value="(model.options[field.key] as number)"
        @update:model-value="setOption(field.key, $event)"
      />

      <el-input
        v-else
        :model-value="(model.options[field.key] as string)"
        :placeholder="field.placeholder"
        class="control"
        @update:model-value="setOption(field.key, $event)"
      />

      <div v-if="field.hint" class="hint">{{ field.hint }}</div>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.control {
  width: 100%;
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
</style>
