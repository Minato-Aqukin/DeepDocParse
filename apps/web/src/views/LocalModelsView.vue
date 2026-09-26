<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { unwrap, workspaceError } from '@/platform/desktop'
import type { Json } from '@/platform/desktop'

/**
 * 本地模型（桌面专有页，plan §1.6）。
 *
 * 同一套桥接调用（`clientQuery:models.list` / `clientCommand:models.install|start|stop`），
 * 同一套展示（安装进度就地显示，不单列"任务"一节）。
 */
const bridge = window.ddpDesktop
const connectionId = ref('')
const catalog = ref<Record<string, Json> | null>(null)
const choices = ref<Record<string, string>>({})
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const pendingKey = ref('')
let alive = true

const recordOf = (value: unknown): Record<string, Json> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, Json>) : {}
const rowsOf = (value: unknown): Record<string, Json>[] =>
  Array.isArray(value) ? value.map(recordOf) : []
const textOf = (value: unknown) => (typeof value === 'string' ? value : '')

const models = computed(() => rowsOf(catalog.value?.items))
const runtime = computed(() => recordOf(catalog.value?.runtime))
const modelStatus: Record<string, string> = {
  not_installed: '未安装', partial: '下载未完成', installed: '已安装并校验',
  failed: '失败', verification_required: '需要校验',
  stopped: '未启动', starting: '启动中', ready: '运行中',
}

function backendsOf(model: Record<string, Json>): Record<string, Json>[] {
  const manifest = recordOf(model.manifest)
  const allowed = Array.isArray(manifest.runtime_ids) ? manifest.runtime_ids : [manifest.runtime_id]
  return models.value.filter((item) => allowed.includes(recordOf(item.manifest).id))
}

function backendReady(model: Record<string, Json>): boolean {
  return backendsOf(model).some((item) =>
    recordOf(item.manifest).id === choices.value[textOf(recordOf(model.manifest).id)]
    && item.status === 'installed')
}

function fileSize(value: unknown): string {
  const bytes = Number(value)
  return Number.isFinite(bytes) ? `${(bytes / 1024 / 1024).toFixed(1)} MiB` : '未知大小'
}

async function resolveLocalConnection(): Promise<string> {
  if (!bridge) throw new Error('connection_not_current')
  const host = bridge as unknown as {
    sourceList?: () => Promise<{ ok: boolean; value?: { sourceId: string; kind: string; active: boolean }[] }>
  }
  const result = await host.sourceList?.()
  if (!result || !result.ok) throw new Error('connection_not_current')
  const local = (result.value ?? []).find((s) => s.active && s.kind === 'local')
    ?? (result.value ?? []).find((s) => s.kind === 'local')
  if (!local) throw new Error('connection_not_current')
  // sourceId 即 connectionId（HostProxy 确认）：直接用作 clientQuery/Command 的连接标识。
  return local.sourceId
}

async function loadModels() {
  if (!bridge || loading.value) return
  loading.value = true
  error.value = ''
  try {
    connectionId.value ||= await resolveLocalConnection()
    const value = unwrap(await bridge.clientQuery({ connectionId: connectionId.value, name: 'models.list', payload: {} }))
    if (!alive) return
    catalog.value = recordOf(value)
    for (const item of models.value) {
      const manifest = recordOf(item.manifest)
      const id = textOf(manifest.id)
      if (manifest.kind === 'model'
        && !backendsOf(item).some((b) => recordOf(b.manifest).id === choices.value[id])) {
        choices.value[id] = textOf(manifest.runtime_id)
      }
    }
  } catch (cause) {
    error.value = workspaceError(cause)
  } finally {
    loading.value = false
  }
}

async function command(action: 'install' | 'start' | 'stop', modelId?: string, runtimeId?: string) {
  if (!bridge || busy.value) return
  busy.value = true
  error.value = ''
  try {
    if (pendingKey.value) throw new Error('receipt_required')
    pendingKey.value = crypto.randomUUID()
    unwrap(await bridge.clientCommand({
      connectionId: connectionId.value, name: `models.${action}`,
      payload: modelId ? { model_id: modelId, ...(runtimeId ? { runtime_id: runtimeId } : {}) } : {},
      idempotencyKey: pendingKey.value,
    }))
    pendingKey.value = ''
    await loadModels()
  } catch (cause) {
    error.value = workspaceError(cause)
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void loadModels()
})
onBeforeUnmount(() => {
  alive = false
})
</script>

<template>
  <div class="models">
    <header>
      <div>
        <h1>本地模型</h1>
        <p class="muted">模型按工作区安装。只有点击下载才会联网获取下列文件；文档和问题保留在本机。下载可在任务中取消或继续。</p>
      </div>
      <el-button :loading="loading" @click="loadModels">刷新状态</el-button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="runtime.status" class="scope-line">
      运行状态：{{ modelStatus[textOf(runtime.status)] || textOf(runtime.status) }}
      <span v-if="runtime.model_id"> · {{ runtime.model_id }}</span>
      <el-button v-if="['ready', 'starting'].includes(textOf(runtime.status))" link
                 :disabled="busy || !!pendingKey" @click="command('stop')">停止模型</el-button>
    </p>
    <p v-if="runtime.backend" class="muted">
      实际后端：{{ (runtime.backend as Record<string, Json>).device }} · {{ runtime.runtime_id }}
    </p>
    <p v-if="runtime.error" role="alert" class="error">{{ workspaceError(new Error(textOf(runtime.error))) }}</p>
    <article v-for="model in models" :key="textOf(recordOf(model.manifest).id)" class="model-row">
      <h2>{{ textOf(recordOf(model.manifest).name) || textOf(recordOf(model.manifest).id) }}</h2>
      <p class="mono">{{ recordOf(model.manifest).id }}</p>
      <p class="muted">
        {{ fileSize(recordOf(model.manifest).bytes) }} · {{ recordOf(model.manifest).license }} ·
        {{ recordOf(model.manifest).kind === 'model' ? '共享 GGUF 权重，运行时单独选择' : recordOf(model.manifest).device }} ·
        {{ modelStatus[textOf(model.status)] || textOf(model.status) }}
      </p>
      <p v-if="model.downloaded_bytes" class="muted">
        已下载 {{ fileSize(model.downloaded_bytes) }} / {{ fileSize(recordOf(model.manifest).bytes) }}
      </p>
      <p v-if="model.error" role="alert" class="error">{{ workspaceError(new Error(textOf(model.error))) }}</p>
      <el-form-item v-if="recordOf(model.manifest).kind === 'model'" label="运行时" class="runtime-pick">
        <el-select v-model="choices[textOf(recordOf(model.manifest).id)]" :disabled="busy" size="small">
          <el-option v-for="backend in backendsOf(model)" :key="textOf(recordOf(backend.manifest).id)"
                     :value="textOf(recordOf(backend.manifest).id)"
                     :label="`${recordOf(backend.manifest).name} · ${modelStatus[textOf(backend.status)] || backend.status}`" />
        </el-select>
      </el-form-item>
      <p v-if="recordOf(model.manifest).kind === 'model' && !backendReady(model)" class="muted">
        先安装并校验所选运行包。GPU 启动会核对物理设备和实际层卸载；失败不自动退回 CPU。
      </p>
      <div class="actions">
        <el-button v-if="model.status !== 'installed'" :disabled="busy || !!pendingKey"
                   @click="command('install', textOf(recordOf(model.manifest).id))">
          {{ model.status === 'partial' ? '继续下载并校验' : model.status === 'verification_required' ? '校验已下载文件' : '下载并校验' }}
        </el-button>
        <el-button v-if="model.status === 'installed' && recordOf(model.manifest).kind === 'model'"
                   :disabled="busy || !!pendingKey || runtime.status === 'starting' || !backendReady(model)"
                   @click="command('start', textOf(recordOf(model.manifest).id), choices[textOf(recordOf(model.manifest).id)])">
          在本机启动所选后端
        </el-button>
      </div>
    </article>
    <p v-if="!catalog" class="muted">{{ loading ? '正在读取模型目录…' : '当前没有本机工作区：先到数据源页打开一个。' }}</p>
  </div>
</template>

<style scoped>
.models { max-width: 960px; margin: auto; display: grid; gap: 12px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 16px; font-weight: 600; margin: 0; }
.mono { font-family: var(--ddp-font-mono); font-size: 12px; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.error { color: var(--el-color-danger); }
.model-row { border-top: 1px solid var(--el-border-color-lighter); padding: 12px 0; display: grid; gap: 6px; }
.model-row p { margin: 0; }
.actions { display: flex; gap: 8px; }
.scope-line { margin: 0; }
</style>
