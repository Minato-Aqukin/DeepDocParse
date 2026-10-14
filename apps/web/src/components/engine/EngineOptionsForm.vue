<script setup lang="ts">
import { computed, watch } from 'vue'

import { useParseEngines } from '@/composables/useParseEngines'
import { defaultOptions, schemaOf } from '@/constants/engines'
import type { EngineChoice, ParseEngineEntry } from '@/types/api'

/**
 * 解析参数表单：选项来自 `GET /api/parse-engines`（注册表驱动），参数字段按
 * constants/engines.ts 的 schema 渲染。
 *
 * 上传对话框、"换参数重解析"与设置页共用它，三处口径永远一致。
 * 服务端清单里有但 schema 目录里没有的引擎仍然可选（按名字本身、无参数渲染）。
 */
const model = defineModel<EngineChoice>({ required: true })
withDefaults(defineProps<{ labelWidth?: string }>(), { labelWidth: '96px' })

const { engines, defaultEngine, status } = useParseEngines()

const schema = computed(() => schemaOf(model.value.engine))

interface EngineOption {
  entry: ParseEngineEntry
  /** 显示名：目录中文名（模型 id 与引擎名不同时缀上它）或注册表名本身 */
  label: string
  /** unhealthy / draining 现在提交必然失败，直接禁选 */
  disabled: boolean
}

const options = computed<EngineOption[]>(() =>
  engines.value.map((entry) => {
    const label = schemaOf(entry.engine)?.label ?? entry.engine
    return {
      entry,
      label: entry.model && entry.model !== entry.engine ? `${label}（${entry.model}）` : label,
      disabled: entry.readiness === 'unhealthy' || entry.readiness === 'draining',
    }
  }))

const usableFallback = computed(() => {
  const listed = options.value.find((o) => o.entry.engine === defaultEngine.value && !o.disabled)
  return listed?.entry.engine ?? options.value.find((o) => !o.disabled)?.entry.engine
})

function settleEngine() {
  if (status.value === 'loading') return
  if (status.value === 'unavailable') {
    // 清单拿不到：不自己猜引擎，传空串让服务端用它的缺省引擎
    if (model.value.engine !== '') model.value = { engine: '', options: {} }
    return
  }
  const current = options.value.find((o) => o.entry.engine === model.value.engine)
  // 空串（=用服务端缺省）只在清单可用时才落到具体引擎上；
  // 不在清单里（例如别处部署存下的 mineru 偏好）或当前不可选的，同样迁走
  if ((!current || current.disabled) && usableFallback.value !== undefined) {
    model.value = { engine: usableFallback.value, options: defaultOptions(usableFallback.value) }
  }
}

// 清单是异步到的：loading 结束那一刻（ready/unavailable 任一）结算一次。
// 用户换引擎只重置参数（上面 settle 的是"当前值不在清单里"，用户刚选的不可能不在）。
watch([status, options], settleEngine, { immediate: true })
watch(
  () => model.value.engine,
  (engine, previous) => {
    if (previous !== undefined && engine !== previous) {
      model.value = { engine, options: defaultOptions(engine) }
    }
  },
)

function setOption(key: string, value: unknown) {
  model.value = { ...model.value, options: { ...model.value.options, [key]: value } }
}
</script>

<template>
  <el-form :label-width="labelWidth" label-position="left">
    <el-form-item label="解析引擎">
      <el-select v-if="status !== 'unavailable'" v-model="model.engine" class="control">
        <el-option
          v-for="o in options"
          :key="o.entry.engine"
          :value="o.entry.engine"
          :label="o.disabled ? `${o.label}（当前不可用）` : o.label"
          :disabled="o.disabled"
        />
      </el-select>
      <!-- 清单拿不到（桌面本地运行时没有这个端点、网关不可达）：不画选择器，
           不自己猜引擎 —— 提交时传空串，服务端用它的缺省引擎解析 -->
      <div v-else class="hint">引擎列表暂时不可用，将使用服务端的默认引擎解析</div>
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
