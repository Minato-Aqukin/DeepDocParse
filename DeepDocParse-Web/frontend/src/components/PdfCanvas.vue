<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef, watch } from 'vue'

import type { Highlight } from '@/types/workbench'

/**
 * 用 pdf.js 把原件渲染到 canvas，并在上面叠一层可点击的高亮框。
 *
 * 为什么不用 <iframe>：iframe 里是浏览器自带的 PDF 阅读器，拿不到坐标系，
 * 没法把 layout 的 bbox 叠上去——而"点出处跳到原文并高亮"正是这一层存在的意义。
 *
 * 高亮框用**百分比**定位而不是像素：bbox 基于 layout 的 page_size，
 * 换算成百分比后与渲染倍率、容器宽度全都无关，缩放时不用重算。
 */
const props = defineProps<{
  src: string
  pageIdx: number
  pageSize: [number, number] | null
  highlights: Highlight[]
}>()

const emit = defineEmits<{
  (e: 'loaded', pageCount: number): void
  (e: 'highlightClick', highlight: Highlight): void
}>()

const canvas = ref<HTMLCanvasElement>()
const wrapper = ref<HTMLDivElement>()
const loading = ref(false)
const failed = ref('')
const pdf = shallowRef<import('pdfjs-dist').PDFDocumentProxy>()
// 释放要走 loadingTask（它才管着 worker 与网络请求），不是 document 本身
let loadingTask: import('pdfjs-dist').PDFDocumentLoadingTask | null = null
let renderTask: { cancel: () => void } | null = null

async function loadPdfjs() {
  const pdfjs = await import('pdfjs-dist')
  if (!pdfjs.GlobalWorkerOptions.workerPort) {
    // ?worker 让 Vite 把 worker 打进产物，不依赖 CDN（离线自部署必须）
    const Worker = (await import('pdfjs-dist/build/pdf.worker.min.mjs?worker')).default
    pdfjs.GlobalWorkerOptions.workerPort = new Worker()
  }
  return pdfjs
}

async function openDocument() {
  failed.value = ''
  if (!props.src) return
  loading.value = true
  try {
    const pdfjs = await loadPdfjs()
    await loadingTask?.destroy()
    loadingTask = pdfjs.getDocument({ url: props.src, withCredentials: false })
    pdf.value = await loadingTask.promise
    emit('loaded', pdf.value.numPages)
    await renderPage()
  } catch (error) {
    failed.value = `无法渲染原件：${(error as Error).message}`
  } finally {
    loading.value = false
  }
}

async function renderPage() {
  const doc = pdf.value
  const el = canvas.value
  if (!doc || !el) return
  const pageNumber = Math.min(Math.max(props.pageIdx + 1, 1), doc.numPages)

  renderTask?.cancel()
  const page = await doc.getPage(pageNumber)
  const width = wrapper.value?.clientWidth || 800
  const base = page.getViewport({ scale: 1 })
  // 按容器宽度自适应，再乘 devicePixelRatio 保证高分屏不糊
  const scale = (width / base.width) * Math.min(window.devicePixelRatio || 1, 2)
  const viewport = page.getViewport({ scale })

  el.width = Math.floor(viewport.width)
  el.height = Math.floor(viewport.height)
  const context = el.getContext('2d')
  if (!context) return
  const task = page.render({ canvas: el, canvasContext: context, viewport })
  renderTask = task
  try {
    await task.promise
  } catch (error) {
    if ((error as Error).name !== 'RenderingCancelledException') throw error
  }
}

function boxStyle(highlight: Highlight) {
  const size = highlight.pageSize || props.pageSize
  if (!highlight.bbox || !size) return { display: 'none' }
  const [x0, y0, x1, y1] = highlight.bbox
  const [w, h] = size
  return {
    left: `${(x0 / w) * 100}%`,
    top: `${(y0 / h) * 100}%`,
    width: `${((x1 - x0) / w) * 100}%`,
    height: `${((y1 - y0) / h) * 100}%`,
  }
}

watch(() => props.src, openDocument, { immediate: true })
watch(() => props.pageIdx, renderPage)
onBeforeUnmount(() => {
  renderTask?.cancel()
  void loadingTask?.destroy()
})

defineExpose({ rerender: renderPage })
</script>

<template>
  <div ref="wrapper" class="pdf-canvas" v-loading="loading">
    <el-alert v-if="failed" :title="failed" type="warning" :closable="false" />
    <div v-else class="stage">
      <canvas ref="canvas" />
      <div class="overlay">
        <div
          v-for="(highlight, i) in highlights"
          :key="i"
          class="box"
          :class="highlight.kind"
          :style="boxStyle(highlight)"
          :title="highlight.label"
          @click="emit('highlightClick', highlight)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.pdf-canvas {
  width: 100%;
}
.stage {
  position: relative;
  line-height: 0;
}
canvas {
  width: 100%;
  height: auto;
  display: block;
  box-shadow: 0 0 0 1px var(--el-border-color-light);
}
.overlay {
  position: absolute;
  inset: 0;
}
.box {
  position: absolute;
  cursor: pointer;
  border-radius: 2px;
  transition: background-color 0.15s;
}
/* 出处：明确的强调色；选中：轻量提示。两者都不能挡住底下的字 */
.box.citation {
  background: rgba(42, 120, 214, 0.22);
  outline: 2px solid rgba(42, 120, 214, 0.85);
}
.box.selected {
  background: rgba(235, 104, 52, 0.18);
  outline: 1px solid rgba(235, 104, 52, 0.7);
}
.box:hover {
  background: rgba(42, 120, 214, 0.32);
}
</style>
