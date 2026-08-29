<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { knowledgeApi } from '@/api'
import type { KnowledgeReviewItem } from '@/types/api'

const items = ref<KnowledgeReviewItem[]>([])
const loading = ref(false)
const errorText = ref('')
const truncated = ref(false)
const visibleLimit = ref(200)
const reason = ref('')

const labels: Record<KnowledgeReviewItem['target_kind'], string> = {
  graph_edge: '图谱边', wiki_sentence: 'Wiki 句', entity_merge: '实体合并', extract_field: '抽取字段',
}

async function load() {
  loading.value = true
  errorText.value = ''
  truncated.value = false
  try {
    const response = (await knowledgeApi.reviews()).data
    items.value = response.items
    truncated.value = response.truncated
    visibleLimit.value = response.limit
  } catch (error) {
    errorText.value = `复核队列加载失败：${String(error)}`
  } finally {
    loading.value = false
  }
}

async function review(item: KnowledgeReviewItem, action: 'pass' | 'reject' | 'question') {
  try {
    await knowledgeApi.review(item.target_kind, item.target_id, {
      action,
      reason_code: action === 'pass' ? 'source_checked' : action === 'reject' ? 'unsupported' : 'needs_follow_up',
      reason_text: reason.value.trim() || undefined,
    })
    items.value = items.value.filter((row) => row !== item)
    ElMessage.success('复核标注已保存，将进入评测样本导出')
  } catch (error) {
    ElMessage.error(`复核失败：${String(error)}`)
  }
}

onMounted(load)
</script>

<template>
  <section class="review-queue" v-loading="loading">
    <header><h3>复核队列</h3><span>{{ items.length }} 项</span></header>
    <el-alert v-if="errorText" :title="errorText" type="error" :closable="false" />
    <el-alert v-if="truncated" :title="`待复核项较多；当前只显示 ${visibleLimit} 项，处理后刷新可继续。`"
      type="warning" :closable="false" />
    <el-input v-model="reason" placeholder="可选：本次复核理由" clearable />
    <el-empty v-if="!items.length && !loading && !errorText" description="没有待复核项" :image-size="64" />
    <div class="items">
      <article v-for="item in items" :key="`${item.target_kind}:${item.target_id}`">
        <div><code>{{ labels[item.target_kind] }}</code><p>{{ item.label }}</p></div>
        <div class="actions">
          <el-button size="small" @click="review(item, 'pass')">通过</el-button>
          <el-button size="small" @click="review(item, 'question')">标疑</el-button>
          <el-button size="small" type="danger" plain @click="review(item, 'reject')">驳回</el-button>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.review-queue { display: grid; gap: 10px; min-height: 0; }
header, .actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
h3 { margin: 0; font-size: 15px; }
header span { color: var(--ddp-ink-3); font-size: 12px; }
.items { display: grid; gap: 1px; max-height: 360px; overflow: auto; border-block: 1px solid var(--ddp-line); }
article { display: grid; gap: 8px; padding: 10px 2px; border-bottom: 1px solid var(--ddp-line); }
article p { margin: 4px 0 0; line-height: 1.45; overflow-wrap: anywhere; }
code { color: var(--ddp-ink-3); font-family: var(--ddp-font-mono); font-size: 11px; }
.actions { justify-content: flex-end; }
</style>
