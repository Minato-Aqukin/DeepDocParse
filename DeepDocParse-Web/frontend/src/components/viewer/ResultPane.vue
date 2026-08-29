<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import type { Block, PageBlocks } from '@/types/api'
import { renderMarkdown, resolveAuthedImages } from '@/utils/markdown'

/**
 * 解析结果。两个视图：
 * - 按页：块级渲染，每块带页码/bbox，可点击联动左栏（对齐粒度到页，见 DESIGN.md §6.4）
 * - Markdown：整篇渲染，保留表格/公式的排版
 *
 * 长文档只渲染当前页附近的几页，否则 200 页文档的 DOM 直接把浏览器拖垮。
 */
const props = defineProps<{
  pages: PageBlocks[]
  markdown: string
  activePage: number
  selectedChunkId: string | null
}>()

const emit = defineEmits<{
  (e: 'blockClick', block: Block): void
  (e: 'pageChange', pageIdx: number): void
}>()

const view = ref<'pages' | 'markdown'>('pages')
const body = ref<HTMLElement>()
const WINDOW = 2 // 当前页 ±2 页

const visiblePages = computed(() =>
  props.pages.filter((p) => Math.abs(p.page_idx - props.activePage) <= WINDOW),
)
const html = computed(() => renderMarkdown(props.markdown))

let revoke: (() => void) | undefined
watch([html, view], async () => {
  if (view.value !== 'markdown') return
  await nextTick()
  if (body.value) {
    revoke?.()
    revoke = await resolveAuthedImages(body.value)
  }
})
</script>

<template>
  <div class="result-pane">
    <div class="toolbar">
      <el-radio-group v-model="view" size="small">
        <el-radio-button value="pages">按页</el-radio-button>
        <el-radio-button value="markdown">Markdown</el-radio-button>
      </el-radio-group>
      <span class="hint">共 {{ pages.length }} 页 · 点击段落可在左栏定位</span>
    </div>

    <div v-if="view === 'pages'" class="pages">
      <section v-for="page in visiblePages" :key="page.page_idx" class="page">
        <header :class="{ active: page.page_idx === activePage }" @click="emit('pageChange', page.page_idx)">
          第 {{ page.page_idx + 1 }} 页
        </header>
        <p
          v-for="block in page.blocks"
          :key="block.seq"
          class="block"
          :class="{ selected: block.chunk_id && block.chunk_id === selectedChunkId }"
          @click="emit('blockClick', block)"
        >
          {{ block.text }}
        </p>
      </section>
      <el-empty v-if="!visiblePages.length" description="这一页没有可显示的文本块" />
    </div>

    <!-- v-html 的内容已在 renderMarkdown 里过 DOMPurify（文档内容是不可信输入） -->
    <div v-else ref="body" class="markdown-body" v-html="html" />
  </div>
</template>

<style scoped>
.result-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  /* 同上：这一层也是 flex 项，`min-width: auto` 会把宽表的宽度继续往上传 */
  min-width: 0;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 8px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.pages,
.markdown-body {
  overflow: auto;
  flex: 1;
}
.page header {
  position: sticky;
  top: 0;
  background: var(--el-bg-color);
  padding: 4px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
  z-index: 1;
}
.page header.active {
  color: var(--el-color-primary);
}
.block {
  margin: 8px 0;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
  line-height: 1.7;
}
.block:hover {
  background: var(--el-fill-color-light);
}
/* 与 PdfCanvas 的 .box.selected 保持同一套语义：选中是墨色，红只给出处 */
.block.selected {
  background: color-mix(in srgb, var(--ddp-ink) 8%, transparent);
  outline: 1px solid var(--ddp-ink-3);
}
</style>
