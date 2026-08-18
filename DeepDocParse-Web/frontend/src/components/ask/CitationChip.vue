<script setup lang="ts">
import { computed } from 'vue'

import { similarityText } from '@/constants/status'
import type { Citation } from '@/types/api'

/**
 * 一条出处：截图 + 页码 + 片段 + **相关度**。
 *
 * 相关度显示的是 `similarity`（余弦相似度）而不是 `score`：
 * score 是 RRF 融合分，只由名次决定，两路都排第一就恒为 0.0328 ——
 * 绝佳命中和勉强及格长得一模一样，摆上去等于给用户一个假的确定感。
 *
 * 量不到相似度时（关键词路命中 / 向量化挂了）显示"—"，**不显示 0%**：
 * 0% 的意思是"完全不相关"，那是另一回事。
 *
 * `cropUrl` 由父组件传入：出处截图受 JWT 保护，要先取回来换成 blob URL。
 * `warnBelow` 也由父组件从后端的 confidence 里透传：**校准值只能有一处**
 * （backend/app/config.py::qa_low_similarity），前端再抄一份迟早会漂。
 */
const props = withDefaults(
  defineProps<{ citation: Citation; index: number; cropUrl?: string; warnBelow?: number }>(),
  { warnBelow: 0.6 },   // 后端没给时的兜底，与 qa_low_similarity 的默认值一致
)
defineEmits<{ (e: 'locate'): void }>()

const percent = computed(() => similarityText(props.citation.similarity))
const tagType = computed(() => {
  const value = props.citation.similarity
  if (value === null || value === undefined) return 'info'
  return value >= props.warnBelow ? 'success' : 'warning'
})
const tooltip = computed(() =>
  percent.value
    ? `与问题的语义相似度 ${percent.value}（融合名次分 ${props.citation.score}）`
    : '本条由关键词路命中，未测得语义相似度',
)
</script>

<template>
  <div class="citation" :class="{ stale: citation.resolved === false }" @click="$emit('locate')">
    <img v-if="cropUrl" :src="cropUrl" alt="出处截图" />
    <div class="cite-text">
      <div class="line">
        <b>[{{ index }}] 第 {{ citation.page_idx + 1 }} 页</b>
        <el-tooltip :content="tooltip">
          <el-tag size="small" :type="tagType" effect="plain">相关度 {{ percent ?? '—' }}</el-tag>
        </el-tooltip>
        <el-tooltip v-if="citation.resolved === false"
                    content="这条出处指向的分块已随重建索引失效，无法再定位到原文">
          <el-tag size="small" type="danger" effect="plain">出处已失效</el-tag>
        </el-tooltip>
      </div>
      <span class="snippet">{{ citation.snippet }}</span>
    </div>
  </div>
</template>

<style scoped>
.citation {
  display: flex;
  gap: 8px;
  padding: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.citation:hover {
  border-color: var(--el-color-primary);
}
.citation.stale {
  opacity: 0.7;
  border-style: dashed;
}
.citation img {
  width: 88px;
  max-height: 60px;
  object-fit: cover;
  border-radius: 2px;
}
.cite-text {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--el-text-color-regular);
  min-width: 0;
}
.line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.snippet {
  overflow-wrap: anywhere;
}
</style>
