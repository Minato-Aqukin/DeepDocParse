<script setup lang="ts">
import { computed, ref } from 'vue'

import { tasksApi } from '@/api/tasks'
import StatusTag from '@/components/common/StatusTag.vue'
import { DELIVERY_STATE, metaOf } from '@/constants/federation'

/**
 * Web 交付下载 + 摘要校验 + ack（`GET /api/v1/deliveries/{id}` + `POST .../ack`）。
 *
 * 顺序钉死：先下载交付字节 → 本地用规范 JSON 重算
 * `content_digest(canonical result)`（与后端 `plans.canonical_bytes` 同一口径：
 * UTF-8、`sort_keys`、`separators=(",", ":")`、`ensure_ascii=False`）→
 * 对上 `result_manifest_digest` 才允许 ack。`result=null`（超界未持久化）、
 * 摘要对不上、410 `delivery_expired` 时一律不许确认、不显示“已保存本地”。
 * 同一 delivery 的重复确认复用同一幂等键，不换键重发。
 */
const props = defineProps<{ deliveryId: string | null; deliveryState: string; readOnly?: boolean }>()

const body = ref<Record<string, unknown> | null>(null)
const manifest = ref<string | null>(null)
const expiresAt = ref<string | null>(null)
const localDigest = ref<string | null>(null)
const receipt = ref<Record<string, unknown> | null>(null)
const busy = ref<'' | 'fetch' | 'ack'>('')
const error = ref('')
const ackKey = ref('')

/** 与后端 `plans.canonical_bytes` 同一口径的规范序列化（JSON 值必须是有限值）。 */
function canonical(value: unknown): string {
  return JSON.stringify(sortKeys(value))
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    const out: Record<string, unknown> = {}
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortKeys((value as Record<string, unknown>)[key])
    }
    return out
  }
  return value
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

const match = computed(() =>
  localDigest.value && manifest.value ? localDigest.value === manifest.value : null)

function problem(cause: unknown, fallback: string): string {
  const response = (cause as { response?: { status?: number; data?: { error?: { code?: string; message?: string } } } })?.response
  if (response?.status === 410) return `${fallback}：交付已过期（410 delivery_expired），结果没有保存到本地。`
  if (response?.status === 404) return `${fallback}：交付不存在或对你不可见（404）。`
  const detail = response?.data?.error
  if (detail?.code === 'input_not_verified') {
    return `${fallback}：提交的摘要与已交付结果不一致（409 input_not_verified），没有确认。`
  }
  if (detail?.code) return `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
  return `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

async function fetch() {
  if (!props.deliveryId || busy.value) return
  busy.value = 'fetch'
  error.value = ''
  try {
    const { data } = await tasksApi.delivery(props.deliveryId)
    manifest.value = data.result_manifest_digest
    expiresAt.value = data.expires_at
    body.value = data.result
    if (data.result == null) {
      localDigest.value = null
      error.value = '交付文档超过字节上限没有持久化（result=null）：不能校验，不能确认，也不得显示“已保存”。'
      return
    }
    localDigest.value = `sha256:${await sha256Hex(new TextEncoder().encode(canonical(data.result)))}`
  } catch (cause) {
    error.value = problem(cause, '交付下载失败')
  } finally {
    busy.value = ''
  }
}

async function ack() {
  if (!props.deliveryId || !manifest.value || busy.value || props.readOnly) return
  if (localDigest.value !== manifest.value) {
    error.value = '本地重算摘要与中心声明不一致：不能确认。'
    return
  }
  busy.value = 'ack'
  error.value = ''
  try {
    ackKey.value ||= `ack-${props.deliveryId}-${crypto.randomUUID()}`
    const { data } = await tasksApi.ackDelivery(props.deliveryId, manifest.value, ackKey.value)
    receipt.value = data as unknown as Record<string, unknown>
  } catch (cause) {
    error.value = problem(cause, '交付确认失败')
  } finally {
    busy.value = ''
  }
}

function download() {
  if (!body.value) return
  const blob = new Blob([canonical(body.value)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${props.deliveryId ?? 'delivery'}.result.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <section class="delivery" aria-label="交付下载与确认">
    <p class="meta">
      交付 <span class="ddp-mono">{{ deliveryId ?? '—' }}</span> ·
      <StatusTag :meta="metaOf(DELIVERY_STATE, deliveryState)" />
      <span v-if="expiresAt" class="muted">有效至 <span class="ddp-mono">{{ expiresAt }}</span></span>
    </p>
    <div class="actions">
      <el-button :disabled="!deliveryId || !!busy" :loading="busy === 'fetch'" @click="fetch">
        下载交付字节
      </el-button>
      <el-button :disabled="!body" text @click="download">另存结果 JSON</el-button>
      <el-button :disabled="!manifest || match !== true || !!busy || readOnly" :loading="busy === 'ack'" type="primary" @click="ack">
        校验通过，确认交付
      </el-button>
    </div>
    <p v-if="manifest" class="meta">
      中心声明 <span class="ddp-mono">{{ manifest }}</span><br />
      本地重算 <span class="ddp-mono">{{ localDigest ?? '（尚无可校验字节）' }}</span> ·
      {{ match === true ? '一致，可以确认' : match === false ? '不一致，不能确认' : '等待下载' }}
    </p>
    <p v-if="receipt" class="meta">
      回执状态 <span class="ddp-mono">{{ String(receipt.state) }}</span> ·
      校验时间 <span class="ddp-mono">{{ String(receipt.verified_at ?? '—') }}</span>
    </p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-else class="hint">读取不是确认：下载后本地重算摘要，对上才允许确认；过期件永远不可确认。</p>
  </section>
</template>

<style scoped>
.delivery { display: grid; gap: 12px; }
.meta, .hint, .muted { margin: 0; color: var(--ddp-ink-2); font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.muted { color: var(--ddp-ink-3); }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
</style>
