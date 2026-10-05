<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { conversationsApi } from '@/api'
import type { ResourceContext } from '@/api/resource-context'
import StatusTag from '@/components/common/StatusTag.vue'
import type { EvidenceDetail } from '@/types/api'
import { fetchAuthedImage } from '@/utils/markdown'
import { formatPageLocator } from '@/utils/page-locator'
import { approvedPlanLabel, onLocalSource, onReadOnlySource } from '@/platform/desktop'

const props = withDefaults(defineProps<{
  evidenceId: string; context?: ResourceContext; closeLabel?: string
}>(), {
  closeLabel: '返回解析结果',
})
defineEmits<{ (e: 'close'): void }>()

const detail = ref<EvidenceDetail>()
const cropObjectUrl = ref('')
const reasonText = ref('')
const loading = ref(false)
const reviewing = ref(false)
const errorText = ref('')
let alive = true
let loadGeneration = 0

const reviewMeta = computed(() => ({
  unreviewed: { label: '待人工复核', type: 'info' as const },
  passed: { label: '人工通过', type: 'success' as const },
  rejected: { label: '人工驳回', type: 'danger' as const },
  questioned: { label: '人工标疑', type: 'warning' as const },
})[detail.value?.review_state ?? 'unreviewed'])

const sourceLink = computed(() => {
  const source = detail.value
  if (!source) return undefined
  return {
    path: `/documents/${source.document.id}`,
    query: {
      resource_id: source.resource_id ?? undefined,
      version_id: source.source_version_id ?? undefined,
      job: source.parse_job_id,
      page: String(source.page_idx + 1),
      chunk: source.chunk_id ?? undefined,
    },
  }
})

function revokeCrop() {
  if (cropObjectUrl.value) URL.revokeObjectURL(cropObjectUrl.value)
  cropObjectUrl.value = ''
}

async function load() {
  const mine = ++loadGeneration
  loading.value = true
  errorText.value = ''
  revokeCrop()
  detail.value = undefined
  try {
    const fetched = (await conversationsApi.evidence(props.evidenceId, props.context)).data
    if (!alive || mine !== loadGeneration) return
    detail.value = fetched
    if (fetched.crop_url) {
      const objectUrl = await fetchAuthedImage(fetched.crop_url, props.context)
      if (!objectUrl) return
      if (!alive || mine !== loadGeneration) URL.revokeObjectURL(objectUrl)
      else cropObjectUrl.value = objectUrl
    }
  } catch (error) {
    if (!alive || mine !== loadGeneration) return
    detail.value = undefined
    errorText.value = `证据加载失败：${String(error)}`
  } finally {
    if (alive && mine === loadGeneration) loading.value = false
  }
}

async function review(verdict: 'pass' | 'reject' | 'question') {
  // 核对是 POST：桌面中心只读，宿主会拒绝；表单已禁用，这里再挡一次。
  if (!detail.value || reviewing.value || onReadOnlySource.value) return
  reviewing.value = true
  const reasonCode = {
    pass: 'source_checked', reject: 'evidence_incorrect', question: 'needs_follow_up',
  }[verdict]
  try {
    await conversationsApi.verifyEvidence(detail.value.id, {
      verdict, reason_code: reasonCode, reason_text: reasonText.value.trim() || undefined,
    }, props.context)
    reasonText.value = ''
    await load()
    ElMessage.success(verdict === 'pass' ? '已标记通过' : verdict === 'reject' ? '已驳回' : '已标记存疑')
  } catch (error) {
    ElMessage.error(`核对提交失败：${String(error)}`)
  } finally {
    reviewing.value = false
  }
}

watch(() => [props.evidenceId, props.context?.resource_id, props.context?.version_id],
  load, { immediate: true })
onBeforeUnmount(() => {
  alive = false
  loadGeneration++
  revokeCrop()
})
</script>

<template>
  <div class="evidence-preview" v-loading="loading">
    <header class="preview-head">
      <div>
        <h3>证据预览</h3>
        <StatusTag v-if="detail" :meta="reviewMeta" />
      </div>
      <el-button link @click="$emit('close')">{{ props.closeLabel }}</el-button>
    </header>

    <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" />

    <template v-if="detail">
      <nav class="layers" aria-label="证据定位层级">
        <span><b>文档</b>{{ detail.document.filename }}</span>
        <span title="印刷标签只用于显示；打开与高亮始终使用 PDF 物理页序"><b>PDF 页</b>{{ formatPageLocator(detail.page_idx, detail.printed_page_label) }}</span>
        <span><b>块</b><code>#{{ detail.seq }}</code></span>
        <span><b>原子</b>{{ detail.kind }} · {{ detail.source_type === 'generated' ? '生成理解' : '原文' }}</span>
      </nav>
      <router-link v-if="sourceLink" :to="sourceLink">打开固定版本原文</router-link>
      <p v-if="!detail.chunk_id" class="locator-note" role="note">
        这条证据不在当前索引里（历史出处或索引已按新规则重建）：内容与坐标按它自己的固定解析版本保留，
        当前检索不会再命中它，也不会被接到新的分块上。
      </p>

      <section class="source-block">
        <div class="section-title">
          <span>块 / 原子内容</span>
          <code>{{ detail.parse_job_id }} · v{{ detail.doc_version }}</code>
        </div>
        <p>{{ detail.content || '该视觉原子没有可读文本，请直接核对裁图。' }}</p>
        <p v-if="detail.derived_from" class="derived">
          这是生成理解；最终依据回到源 Evidence <code>{{ detail.derived_from }}</code>
        </p>
      </section>

      <section class="crop-section">
        <div class="section-title">
          <span>原子裁图 · 1:1 原始像素</span>
          <code v-if="detail.bbox">bbox {{ detail.bbox.join(', ') }}</code>
        </div>
        <div class="crop-scroll">
          <img v-if="cropObjectUrl" :src="cropObjectUrl" alt="证据原子 1:1 裁图" />
          <el-empty v-else-if="!detail.bbox"
                    :description="`这条证据没有区域坐标（bbox 缺失），只能定位到 ${formatPageLocator(detail.page_idx, detail.printed_page_label, 'PDF 第')}，不能当作区域精确的出处`" />
          <el-empty v-else description="没有可用裁图；可打开固定版本原文，按上面的 bbox 在该页定位" />
        </div>
      </section>

      <!-- 本机工作区不记录人工核对（local-content-subset：verification 不支持），不给一个点了必失败的表单。 -->
      <section v-if="!onLocalSource" class="review">
        <div class="section-title"><span>人工核对</span><span>只做标注，不修改内容</span></div>
        <el-input
          v-model="reasonText" type="textarea" :rows="2"
          placeholder="可选：记录通过 / 驳回 / 标疑的理由"
        />
        <div class="review-actions">
          <el-button :loading="reviewing" :disabled="onReadOnlySource" @click="review('pass')">通过</el-button>
          <el-button :loading="reviewing" :disabled="onReadOnlySource" @click="review('question')">标疑</el-button>
          <el-button type="danger" plain :loading="reviewing" :disabled="onReadOnlySource" @click="review('reject')">驳回</el-button>
        </div>
        <p v-if="onReadOnlySource" class="readonly-hint" role="note">{{ approvedPlanLabel() }}</p>
      </section>

      <section v-if="detail.verifications.length" class="history">
        <div class="section-title"><span>核对记录</span><span>{{ detail.verifications.length }} 条</span></div>
        <div v-for="item in detail.verifications" :key="item.id" class="history-row">
          <code>{{ item.mode }}</code>
          <span>{{ item.verdict }}</span>
          <span>{{ item.reason_text || item.reason_code || '无补充理由' }}</span>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.evidence-preview { display: grid; gap: 20px; min-height: 0; }
.preview-head, .preview-head > div, .section-title, .review-actions, .history-row {
  display: flex; align-items: center; gap: 10px;
}
.preview-head { justify-content: space-between; }
h3 { margin: 0; font-size: 18px; font-weight: 600; }
.layers { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-block: 1px solid var(--ddp-line); }
.layers span { display: grid; gap: 2px; padding: 10px 12px; min-width: 0; color: var(--ddp-ink-2); font-size: 12px; }
.layers span + span { border-left: 1px solid var(--ddp-line); }
.layers b { color: var(--ddp-ink); font-weight: 600; }
.section-title { justify-content: space-between; color: var(--ddp-ink-2); font-size: 12px; }
.source-block, .crop-section, .review, .history { display: grid; gap: 10px; }
.source-block p { margin: 0; white-space: pre-wrap; line-height: 1.7; }
.derived { border-left: 2px solid var(--ddp-cite); padding-left: 10px; color: var(--ddp-ink-2); }
.locator-note { margin: 0; border-left: 2px solid var(--ddp-warn); padding-left: 10px; color: var(--ddp-ink-2); font-size: 13px; }
code { font-family: var(--ddp-font-mono); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.crop-scroll { overflow: auto; min-height: 140px; max-height: 360px; border: 1px solid var(--ddp-line); background: var(--ddp-panel); }
/* 1 CSS px 对应图片 1 原始像素；绝不 max-width:100% 或 object-fit 缩放。 */
.crop-scroll img { display: block; width: auto; height: auto; max-width: none; }
.review-actions { justify-content: flex-end; }
.review-actions :deep(.el-button) { min-height: 44px; }
.readonly-hint { color: var(--ddp-ink-3); font-size: 13px; margin: 6px 0 0; }
.history-row { padding-block: 8px; border-top: 1px solid var(--ddp-line); font-size: 13px; }
.history-row span:last-child { color: var(--ddp-ink-2); }
@media (max-width: 900px) {
  .layers { grid-template-columns: 1fr 1fr; }
  .layers span:nth-child(3) { border-left: 0; border-top: 1px solid var(--ddp-line); }
  .layers span:nth-child(4) { border-top: 1px solid var(--ddp-line); }
}
</style>
