<script setup lang="ts">
import { onMounted, ref } from 'vue'

/**
 * 桌面设置（桌面专有页，plan §1.6）。
 *
 * 四件事：凭证存哪（以 `hostStatus` 的实际返回为准，不猜）、退出会发生什么
 * （关窗即停本机任务——HOST-CONTRACT/v3 §3.3：已受理的远端任务继续且可对账）、
 * 本地运行时后端、当前本机工作区目录（以宿主说的为准，没有就不编）。
 */
const credentialText = ref('正在读取…')
const backendText = ref('正在读取…')
const workspaceText = ref('正在读取…')
const lifecycleText = '关闭窗口即停止本机任务；已受理的远端任务在中心继续，可对账。'
const error = ref('')

async function load() {
  const host = window.ddpDesktop as unknown as {
    hostStatus?: () => Promise<{ ok: boolean; value?: Record<string, unknown>; error?: { code?: string } }>
    sourceList?: () => Promise<{ ok: boolean; value?: { kind: string; active: boolean; label: string }[] }>
  } | undefined
  try {
    const status = await host?.hostStatus?.()
    if (!status) {
      error.value = '宿主暂未提供状态接口，请更新桌面端后重试'
      return
    }
    if (!status.ok) {
      error.value = `状态读取失败：${String(status.error?.code ?? 'unknown')}`
      return
    }
    const value = status.value ?? {}
    const secrets = (value.secrets ?? value.credentialStorage) as
      { backend?: string; persistentAvailable?: boolean; reason?: string | null } | undefined
    credentialText.value = !secrets
      ? '未知：宿主没有返回凭证存储信息。'
      : secrets.persistentAvailable
        ? `凭证由系统密钥库保存（${secrets.backend ?? '系统密钥库'}）。`
        : `凭证仅保留在本次会话${secrets.reason ? `（${secrets.reason}）` : ''}：当前环境没有可用的持久密钥库。`
    const backend = typeof value.runtimeBackend === 'string' ? value.runtimeBackend : ''
    const available = value.runtimeAvailable as boolean | undefined
    const reason = typeof value.runtimeReason === 'string' ? value.runtimeReason : ''
    backendText.value = backend
      ? `本地运行时后端：${backend}${available === false ? `（不可用${reason ? `：${reason}` : ''}）` : ''}`
      : '本地运行时后端：未知。'
    const isolation = typeof value.isolation === 'string' ? value.isolation : ''
    if (isolation) backendText.value += ` · 隔离：${isolation}`
    const listed = await host?.sourceList?.()
    const active = listed?.ok ? (listed.value ?? []).find((s) => s.active) : undefined
    workspaceText.value = active
      ? `当前数据源：${active.label}${active.kind === 'local' ? '（本机工作区）' : '（中心，只读）'}`
      : '当前没有数据源：请到数据源页打开本机工作区或连接中心。'
  } catch (cause) {
    error.value = `状态读取失败：${cause instanceof Error ? cause.message : String(cause)}`
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="settings">
    <h1>设置</h1>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <el-card shadow="never" class="block">
      <template #header>凭证存储</template>
      <p>{{ credentialText }}</p>
      <p class="muted">密码从不落盘、不进日志；保存与否按系统密钥库的实际可用情况决定。</p>
    </el-card>
    <el-card shadow="never" class="block">
      <template #header>退出行为</template>
      <p>{{ lifecycleText }}</p>
      <p class="muted">刷新页面与断开中心不断开本机运行时；挂起会停掉已起的本机实例，恢复时只重启之前归宿主所有的实例。</p>
    </el-card>
    <el-card shadow="never" class="block">
      <template #header>本地运行时</template>
      <p>{{ backendText }}</p>
    </el-card>
    <el-card shadow="never" class="block">
      <template #header>本机工作区</template>
      <p>{{ workspaceText }}</p>
    </el-card>
  </div>
</template>

<style scoped>
.settings { max-width: 960px; margin: auto; display: grid; gap: 16px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
.block p { margin: 0 0 8px; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.error { color: var(--el-color-danger); }
</style>
