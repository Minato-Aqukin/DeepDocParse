<script setup lang="ts">
import { onMounted, ref } from 'vue'

/**
 * 更新（桌面专有页，plan §1.6）。
 *
 * Linux 没有 Electron autoUpdater（HOST-CONTRACT/v3 §3.6）：更新走软件包，
 * 这里给出当前版本与更新命令。版本以宿主 `hostStatus().version`
 * （= Electron `app.getVersion()`）为准；更新前先确认没有进行中的任务。
 */
const version = ref('正在读取…')
const error = ref('')

const updateCommand = 'scripts/update_check.py status --root <安装目录>'
const verifyCommand = 'scripts/update_check.py verify --root <安装目录> --manifest <发布清单> --archive <安装包> --allowed-signers <签名者名单>'
const applyCommand = 'scripts/update_check.py apply --root <安装目录> --manifest <发布清单> --archive <安装包> --allowed-signers <签名者名单>'

async function load() {
  const host = window.ddpDesktop as unknown as {
    hostStatus?: () => Promise<{ ok: boolean; value?: { version?: string } }>
  } | undefined
  try {
    const status = await host?.hostStatus?.()
    version.value = status?.ok && status.value?.version
      ? String(status.value.version)
      : '未知（宿主暂未提供版本）'
  } catch (cause) {
    error.value = `版本读取失败：${cause instanceof Error ? cause.message : String(cause)}`
    version.value = '未知'
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="updates">
    <h1>更新</h1>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <el-card shadow="never" class="block">
      <template #header>当前版本</template>
      <p class="version">{{ version }}</p>
    </el-card>
    <el-card shadow="never" class="block">
      <template #header>Linux 更新方式</template>
      <p>桌面端在 Linux 上没有自动更新器：更新走软件包，先检查，再校验签名，最后应用。</p>
      <p>检查更新前，请先确认没有进行中的任务（解析、索引、模型下载、联邦任务派发）。</p>
      <p>检查状态：<code class="ddp-mono">{{ updateCommand }}</code></p>
      <p>校验签名：<code class="ddp-mono">{{ verifyCommand }}</code></p>
      <p>应用更新：<code class="ddp-mono">{{ applyCommand }}</code></p>
      <p class="muted">--allowed-signers 指向随发布分发的签名者名单（发布锚）：先固定它，再校验、再应用；签名不对直接拒绝。</p>
      <p class="muted">--allow-unsigned 只接受校验和、不做签名认证，仅限演练使用，正式更新绝不加它。</p>
      <p class="muted">应用更新会在工作区有排队/执行中任务、或模型有未下完的分片时拒绝（除非显式加 --allow-active）。</p>
    </el-card>
  </div>
</template>

<style scoped>
.updates { max-width: 960px; margin: auto; display: grid; gap: 16px; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
.block p { margin: 0 0 8px; }
.version { font-size: 18px; font-weight: 600; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.error { color: var(--el-color-danger); }
</style>
