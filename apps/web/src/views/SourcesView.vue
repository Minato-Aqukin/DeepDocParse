<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessageBox } from 'element-plus'

import { sourceErrorLabel, sourceStateLabel } from '@/platform/desktop'
import type { SourceSummary } from '@/platform/desktop'

/**
 * 数据源（桌面首运落点，plan §1.6）。
 *
 * 两件事：打开本机工作区（原生目录框 → 起运行时 → 接上 → 激活），或连接中心
 * （地址 + 账号 + 密码 + 是否保存登录）。列表显示每源状态，可切换、可移除；
 * 移除同时断开该连接并清掉存着的中心登录，但**不删本机资料**（T65）。
 * 每次成功激活/打开/连接后整页重载（切换即清空页面状态，不跨源串会话）。
 */
const route = useRoute()

const sources = ref<SourceSummary[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref('')

const activeSource = computed(() => sources.value.find((s) => s.active) ?? null)
const reasonCode = computed(() => typeof route.query.reason === 'string' ? route.query.reason : '')
// 当前源还在（只是连不上）时，"还没有选择数据源"是错话：说清是哪一个、为什么、怎么办。
const reasonText = computed(() => {
  const active = activeSource.value
  if (active && active.state !== 'ready') {
    const why = active.reason ? `（${sourceErrorLabel(active.reason)}）` : ''
    return `当前数据源「${active.label}」${sourceStateLabel(active.state)}${why}：点「重新连接」，或切换到其他数据源。`
  }
  return reasonCode.value ? sourceErrorLabel(reasonCode.value) : ''
})

const center = ref({ endpoint: '', username: '', password: '', persist: false, advanced: false, storageOrigin: '' })
const centerBusy = ref(false)
const centerError = ref('')

function host(): {
  sourceList?: () => Promise<{ ok: boolean; value?: SourceSummary[]; error?: { code?: string } }>
  sourceActivate?: (input: { sourceId: string }) => Promise<{ ok: boolean; value?: SourceSummary; error?: { code?: string } }>
  sourceRemove?: (input: { sourceId: string }) => Promise<{ ok: boolean; error?: { code?: string } }>
  workspaceOpen?: () => Promise<{ ok: boolean; value?: SourceSummary | null; error?: { code?: string } }>
  centerConnect?: (input: Record<string, unknown>) => Promise<{ ok: boolean; value?: SourceSummary; error?: { code?: string } }>
  hostStatus?: () => Promise<{ ok: boolean; value?: { secrets?: { backend?: string; persistentAvailable?: boolean } } }>
} | undefined {
  return window.ddpDesktop as unknown as ReturnType<typeof host> | undefined
}

function problem(code: string | undefined, fallback: string): string {
  return code ? `${fallback}：${sourceErrorLabel(code)}` : fallback
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await host()?.sourceList?.()
    if (!result) {
      error.value = '宿主暂未提供数据源接口，请更新桌面端后重试'
      return
    }
    if (!result.ok) {
      error.value = problem(result.error?.code, '数据源列表读取失败')
      return
    }
    sources.value = result.value ?? []
  } catch (cause) {
    error.value = `数据源列表读取失败：${cause instanceof Error ? cause.message : String(cause)}`
  } finally {
    loading.value = false
  }
}

// 切换即整页重载（页面状态不跨源）；换源成功后落在内容页，而不是回到数据源页。
function reloadAfterSwitch(landing = '#/resources') {
  location.hash = landing
  location.reload()
}

async function activate(sourceId: string) {
  busy.value = sourceId
  error.value = ''
  try {
    const result = await host()?.sourceActivate?.({ sourceId })
    if (!result) {
      error.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      error.value = problem(result.error?.code, '切换失败')
      return
    }
    reloadAfterSwitch()
  } finally {
    busy.value = ''
  }
}

async function remove(source: SourceSummary) {
  const detail = source.kind === 'local'
    ? '移除后本机工作区的资料保留在原目录（可重新打开），只断开当前连接。'
    : '移除后该中心的登记与保存的登录一并清除。'
  try {
    await ElMessageBox.confirm(detail, `移除「${source.label}」`, {
      type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch { return }
  busy.value = source.sourceId
  error.value = ''
  try {
    const result = await host()?.sourceRemove?.({ sourceId: source.sourceId })
    if (!result) {
      error.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      error.value = problem(result.error?.code, '移除失败')
      return
    }
    await load()
    if (source.active) reloadAfterSwitch('#/sources')
  } finally {
    busy.value = ''
  }
}

async function openWorkspace() {
  busy.value = '__open__'
  error.value = ''
  try {
    const result = await host()?.workspaceOpen?.()
    if (!result) {
      error.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      error.value = problem(result.error?.code, '打开本机工作区失败')
      return
    }
    // null = 用户在原生目录框里点了取消：留在本页，不报错。
    if (result.value) reloadAfterSwitch()
  } finally {
    busy.value = ''
  }
}

const persistHint = ref('正在读取凭证存储方式…')
// 宿主报告没有持久密钥库时不给勾选：登录只能留在本次会话（宿主同样会拒绝落盘）。
const persistAvailable = ref(false)

async function loadPersistHint() {
  try {
    const result = await host()?.hostStatus?.()
    const secrets = result?.ok ? result.value?.secrets : undefined
    if (!secrets) {
      persistHint.value = '是否保存登录取决于系统密钥库是否可用。'
      return
    }
    persistAvailable.value = secrets.persistentAvailable === true
    persistHint.value = secrets.persistentAvailable
      ? `勾选后登录由系统密钥库保存（${secrets.backend ?? '系统密钥库'}）。`
      : '当前环境没有可用的持久密钥库，登录只保留在本次会话。'
  } catch {
    persistHint.value = '是否保存登录取决于系统密钥库是否可用。'
  }
}

async function connect() {
  centerError.value = ''
  if (!center.value.endpoint.trim() || !center.value.username.trim() || !center.value.password) {
    centerError.value = '请填写中心地址、账号与密码'
    return
  }
  centerBusy.value = true
  try {
    const result = await host()?.centerConnect?.({
      endpoint: center.value.endpoint.trim(),
      username: center.value.username.trim(),
      password: center.value.password,
      persist: center.value.persist && persistAvailable.value,
      ...(center.value.storageOrigin.trim() ? { storageOrigin: center.value.storageOrigin.trim() } : {}),
    })
    // 密码只进这一次调用，不存任何变量、不进日志。
    center.value.password = ''
    if (!result) {
      centerError.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      centerError.value = problem(result.error?.code, '连接中心失败')
      return
    }
    reloadAfterSwitch()
  } finally {
    centerBusy.value = false
  }
}

onMounted(() => {
  void load()
  void loadPersistHint()
})
</script>

<template>
  <div class="sources" v-loading="loading">
    <header>
      <div>
        <h1>数据源</h1>
        <p>同一时刻只有一个当前数据源。切换后页面整体重载，另一源的会话、草稿与引用不会串过来。</p>
      </div>
      <el-button :disabled="loading" @click="load">刷新</el-button>
    </header>

    <el-alert v-if="reasonText" type="warning" :closable="false" :title="reasonText" class="reason" />
    <p v-if="error" role="alert" class="error">{{ error }}</p>

    <section class="actions">
      <el-card shadow="never" class="block">
        <template #header>本机工作区</template>
        <p class="muted">打开一个本机目录作为工作区：解析、索引、问答都在本机执行，原件不外发。</p>
        <el-button type="primary" :loading="busy === '__open__'" @click="openWorkspace">打开本机工作区…</el-button>
      </el-card>

      <el-card shadow="never" class="block">
        <template #header>连接中心</template>
        <p class="muted">中心在桌面里是只读镜像：浏览、检索、看原文与引用；写操作须作为联邦任务发起并经批准派发。</p>
        <el-form label-position="top" @submit.prevent="connect">
          <el-form-item label="中心地址">
            <el-input v-model="center.endpoint" placeholder="https://center.example.com" autocomplete="url" />
          </el-form-item>
          <el-form-item label="账号">
            <el-input v-model="center.username" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="center.password" type="password" autocomplete="current-password" />
          </el-form-item>
          <el-form-item>
            <el-checkbox v-model="center.persist" :disabled="!persistAvailable">保存登录</el-checkbox>
          </el-form-item>
          <p class="muted">{{ persistHint }}</p>
          <el-form-item label="对象存储源站（高级，可空）">
            <el-input v-model="center.storageOrigin" placeholder="默认与中心同源" />
          </el-form-item>
          <p v-if="centerError" role="alert" class="error">{{ centerError }}</p>
          <el-button type="primary" native-type="submit" :loading="centerBusy">连接</el-button>
        </el-form>
      </el-card>
    </section>

    <section>
      <h2>已登记的数据源</h2>
      <p v-if="!loading && !sources.length" class="muted">还没有数据源：先打开本机工作区，或连接一个中心。</p>
      <article v-for="s in sources" :key="s.sourceId" class="source-row">
        <div>
          <h3>{{ s.label }}{{ s.active ? '（当前）' : '' }}</h3>
          <p class="muted">
            {{ s.kind === 'local' ? '本机工作区' : '中心（只读）' }} ·
            {{ sourceStateLabel(s.state) }}{{ s.reason ? ` · ${sourceErrorLabel(s.reason)}` : '' }}
          </p>
        </div>
        <div class="row-actions">
          <el-button v-if="!s.active || s.state !== 'ready'" size="small" :loading="busy === s.sourceId"
                     @click="activate(s.sourceId)">{{ s.active ? '重新连接' : '切换' }}</el-button>
          <el-button size="small" type="danger" plain :loading="busy === s.sourceId" @click="remove(s)">移除</el-button>
        </div>
      </article>
      <p class="muted">移除只断开连接并清除保存的登录：本机工作区的资料保留在原目录，可重新打开。</p>
    </section>
  </div>
</template>

<style scoped>
.sources { max-width: 960px; margin: auto; display: grid; gap: 16px; }
header { display: flex; align-items: center; justify-content: space-between; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 16px 0 8px; }
h3 { font-size: 15px; font-weight: 600; margin: 0; }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.block p { margin-top: 0; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.error { color: var(--el-color-danger); }
.reason { margin: 0; }
.source-row {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 12px 0; border-bottom: 1px solid var(--el-border-color-lighter);
}
.row-actions { display: flex; gap: 8px; flex: none; }
@media (max-width: 760px) { .actions { grid-template-columns: 1fr; } }
</style>
