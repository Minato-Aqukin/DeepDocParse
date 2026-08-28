<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { documentsApi, downloadAs } from '@/api'
import AskPanel from '@/components/ask/AskPanel.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import PdfCanvas from '@/components/viewer/PdfCanvas.vue'
import ResultPane from '@/components/viewer/ResultPane.vue'
import { usePolling } from '@/composables/usePolling'
import { indexStatusOf, parseStatusOf } from '@/constants/status'
import {
  COMPILE_DEGRADED, codeDetectionOf, compileStatusOf,
} from '@/constants/compilation'
import type {
  Block, Citation, DocumentInfo, DownloadFormat, IndexValidation, PageBlocks,
} from '@/types/api'
import type { Highlight } from '@/types/workbench'
import { validateAndReindex } from '@/utils/reindex'

/**
 * 三栏工作台：原文 / 解析结果 / 问答。
 * 三者共享 activePage 与 highlights —— 点问答出处、点结果段落，左栏都要跟着定位。
 */
const route = useRoute()
const router = useRouter()

const document = ref<DocumentInfo>()
const pages = ref<PageBlocks[]>([])
const markdown = ref('')
const sourcePath = ref('')
const activePage = ref(0)
const highlights = ref<Highlight[]>([])
const selectedChunkId = ref<string | null>(null)
const showChunks = ref(false)
const loading = ref(true)
const validation = ref<IndexValidation>()
let poller: number | undefined

const pageSize = computed(
  () => pages.value.find((p) => p.page_idx === activePage.value)?.page_size ?? null,
)
const isPdf = computed(() => (document.value?.mime || '').includes('pdf'))

/**
 * 分块边界的只读叠加层（A3）。
 *
 * 两个用处：给要标注评测集的人一眼看清"这段答案落在哪个块里"，
 * 以及让新用户看见检索粒度、对出处建立信任。
 * **只读** —— 不做 RAGFlow 那样的人工编辑，理由见 types/workbench.ts。
 *
 * 只铺当前页：PdfCanvas 一次只渲一页，200 页文档也就是这一页的几十个框。
 */
const chunkBoundaries = computed<Highlight[]>(() => {
  if (!showChunks.value) return []
  const page = pages.value.find((p) => p.page_idx === activePage.value)
  if (!page) return []
  return page.blocks
    .filter((block) => block.bbox)
    .map((block) => ({
      pageIdx: block.page_idx,
      bbox: block.bbox,
      pageSize: block.page_size ?? page.page_size,
      kind: 'chunk' as const,
      label: `#${block.seq} ${block.text.slice(0, 40)}`,
    }))
})

// 边界层垫在底下，出处/选中框画在上面（后画的在上）
const overlays = computed<Highlight[]>(() => [...chunkBoundaries.value, ...highlights.value])

async function load() {
  const id = String(route.params.id)
  loading.value = true
  try {
    document.value = (await documentsApi.get(id)).data
    if (document.value.status !== 'succeeded') return
    const [result, pageData, source] = await Promise.all([
      documentsApi.result(id),
      documentsApi.pages(id),
      documentsApi.sourceUrl(id).catch(() => null),
    ])
    markdown.value = result.data.markdown
    pages.value = pageData.data.pages
    sourcePath.value = source?.data.path ?? ''
  } finally {
    loading.value = false
  }
}

/** 解析或索引还在跑时轮询，两者都落定就停 —— 判断依据统一在 constants/status.ts 的 active 标记。 */
const polling = usePolling(load, () => {
  const doc = document.value
  if (!doc) return false
  return Boolean(parseStatusOf(doc.status).active || indexStatusOf(doc.index_status).active ||
    compileStatusOf(doc.compile_status).active)
})

function locate(citation: Citation) {
  activePage.value = citation.page_idx
  selectedChunkId.value = citation.chunk_id
  highlights.value = [{
    pageIdx: citation.page_idx,
    bbox: citation.bbox,
    pageSize: pages.value.find((p) => p.page_idx === citation.page_idx)?.page_size ?? null,
    kind: 'citation',
    label: citation.snippet,
  }]
}

function selectBlock(block: Block) {
  activePage.value = block.page_idx
  selectedChunkId.value = block.chunk_id
  highlights.value = [{
    pageIdx: block.page_idx,
    bbox: block.bbox,
    pageSize: block.page_size,
    kind: 'selected',
    label: block.text.slice(0, 40),
  }]
}

async function download(format: DownloadFormat) {
  const id = String(route.params.id)
  await downloadAs(documentsApi.downloadUrl(id, format), document.value?.filename)
}

async function reindex() {
  validation.value = await validateAndReindex(String(route.params.id))
  ElMessage.success('已重新排队建立索引')
  await load()
  polling.start()
}

async function validateIndex() {
  validation.value = (await documentsApi.validateIndex(String(route.params.id))).data
  const current = validation.value.status === 'current' ? '当前版本一致' :
    validation.value.status === 'stale' ? '索引版本已过期' :
      validation.value.status === 'unresolved' ? '上游实际模型未解析，版本不可比较' : '尚未编译'
  ElMessage.info(
    `${current}；可接回 ${validation.value.citation_reconnectable} 条，` +
    `会失效 ${validation.value.citation_invalidations} 条出处`,
  )
}

watch(
  () => route.params.id,
  async () => {
    await load()
    polling.start()
  },
  { immediate: true },
)
</script>

<template>
  <div class="workbench" v-loading="loading">
    <div class="head">
      <div class="title">
        <el-button link @click="router.push('/documents')">← 文档库</el-button>
        <span class="name">{{ document?.filename }}</span>
        <span class="pages">{{ document?.page_count }} 页</span>
        <el-tooltip :content="document?.index_error" :disabled="!document?.index_error">
          <StatusTag
            v-if="document && document.index_status !== 'ready'"
            :meta="indexStatusOf(document.index_status)"
          />
        </el-tooltip>
      </div>
      <div class="actions">
        <el-button size="small" @click="router.push(`/documents/${document?.id}/versions`)">
          解析版本
        </el-button>
        <el-button size="small" @click="validateIndex">校验版本</el-button>
        <el-button size="small" @click="reindex">重建索引</el-button>
        <el-dropdown @command="download">
          <el-button size="small">下载<el-icon class="el-icon--right">▾</el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="md">Markdown</el-dropdown-item>
              <el-dropdown-item command="json">版面 JSON</el-dropdown-item>
              <el-dropdown-item command="zip">打包（含图片）</el-dropdown-item>
              <el-dropdown-item command="source">原件</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-alert v-if="document?.status === 'failed'" type="error" :closable="false"
              :title="`解析失败：${document.error}`" />
    <el-alert v-else-if="document?.status !== 'succeeded'" type="info" :closable="false"
              title="解析中，完成后自动刷新" />

    <div v-if="document?.status === 'succeeded'" class="compile-line">
      <StatusTag :meta="compileStatusOf(document.compile_status)" />
      <StatusTag :meta="codeDetectionOf(document.code_detection)" />
      <span v-if="document.layout_version" class="ddp-num">{{ document.layout_version }}</span>
      <span v-if="validation" class="validation ddp-num">
        版本 {{ validation.status }} · 可接回 {{ validation.citation_reconnectable }} ·
        将失效 {{ validation.citation_invalidations }}
      </span>
    </div>
    <div v-for="reason in document?.compile_degraded || []" :key="reason"
         class="compile-degraded">
      {{ COMPILE_DEGRADED[reason] || reason }}
    </div>

    <div v-if="document?.status === 'succeeded'" class="panes">
      <section class="pane source">
        <div class="pane-head">
          <span>原文</span>
          <el-checkbox v-if="isPdf" v-model="showChunks" size="small" class="chunk-toggle">
            分块边界
          </el-checkbox>
          <el-pagination
            v-model:current-page="activePage"
            :page-count="document?.page_count ?? 0"
            :pager-count="5"
            layout="prev, pager, next"
            small
            @update:current-page="highlights = []"
          />
        </div>
        <div class="pane-body">
          <PdfCanvas
            v-if="isPdf && sourcePath"
            :src="sourcePath"
            :page-idx="activePage"
            :page-size="pageSize"
            :highlights="overlays"
          />
          <img v-else-if="sourcePath" :src="sourcePath" class="image-source" alt="原件" />
          <el-empty v-else description="原件不可预览" />
        </div>
      </section>

      <section class="pane result">
        <ResultPane
          :pages="pages"
          :markdown="markdown"
          :active-page="activePage"
          :selected-chunk-id="selectedChunkId"
          @block-click="selectBlock"
          @page-change="activePage = $event"
        />
      </section>

      <section class="pane ask">
        <AskPanel v-if="document" :document="document" @locate="locate" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 100px);
  gap: 10px;
}
.compile-line {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 24px;
  color: var(--ink-2);
  font-size: 13px;
}
.compile-degraded {
  border-left: 2px solid var(--warn);
  padding: 2px 10px;
  color: var(--ink-2);
  font-size: 13px;
}
.validation { margin-left: auto; }
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.name {
  font-size: 16px;
  font-weight: 600;
}
/* 页数是元信息不是状态，按准则二排成普通文字 */
.pages {
  font-family: var(--ddp-font-mono);
  font-size: 12px;
  color: var(--ddp-ink-3);
}
.actions {
  display: flex;
  gap: 8px;
}
.panes {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr 380px;
  gap: 10px;
  min-height: 0;
}
.pane {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  padding-bottom: 6px;
}
.pane-body {
  flex: 1;
  overflow: auto;
}
.image-source {
  max-width: 100%;
}
.chunk-toggle {
  margin-left: auto;
  margin-right: 8px;
}
@media (max-width: 1400px) {
  .panes {
    grid-template-columns: 1fr 1fr;
  }
  .pane.ask {
    grid-column: span 2;
    height: 420px;
  }
}
</style>
