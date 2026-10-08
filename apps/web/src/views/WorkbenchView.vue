<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { conversationsApi, documentsApi, downloadAs, downloadViaSignedUrl } from '@/api'
import { documentContext } from '@/api/resource-context'
import AskPanel from '@/components/ask/AskPanel.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import EvidencePreview from '@/components/evidence/EvidencePreview.vue'
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
import { apiUrl, approvedPlanLabel, onLocalSource, onReadOnlySource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'
import type { Highlight } from '@/types/workbench'
import { validateAndReindex } from '@/utils/reindex'

/**
 * 三栏工作台：原文 / 解析结果 / 问答。
 * 三者共享 activePage 与 highlights —— 点问答出处、点结果段落，左栏都要跟着定位。
 */
const route = useRoute()
const router = useRouter()

const auth = useAuthStore()
const document = ref<DocumentInfo>()
const pages = ref<PageBlocks[]>([])
const markdown = ref('')
const sourcePath = ref('')
const activePage = ref(0)
const highlights = ref<Highlight[]>([])
const selectedChunkId = ref<string | null>(null)
const selectedCitation = ref<Citation | null>(null)
const showChunks = ref(false)
const loading = ref(true)
const loadError = ref(false)
const sourceFocusError = ref(false)
let loadGeneration = 0
let reloadGeneration = 0
let alive = true
const validation = ref<IndexValidation>()

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
  const generation = ++loadGeneration
  const job = typeof route.query.job === 'string' ? route.query.job : undefined
  loadError.value = false
  loading.value = true
  try {
    const response = await documentsApi.get(id)
    if (generation !== loadGeneration) return
    document.value = response.data
    if (document.value.status !== 'succeeded') return
    const [result, pageData, source] = await Promise.all([
      documentsApi.result(id, job),
      documentsApi.pages(id, job),
      documentsApi.sourceViewUrl(id).catch(() => null),
    ])
    if (generation !== loadGeneration) return
    markdown.value = result.data.markdown
    pages.value = pageData.data.pages
    // 桌面中心源：宿主把预签名地址改写成 `ddp://app/_object/...`，
    // 直接可用；本机/浏览器：相对地址经宿主或同源解析，一律走 `apiUrl`。
    sourcePath.value = source?.data.url ? apiUrl(source.data.url) : ''
    focusRequestedSource()
  } catch {
    if (generation !== loadGeneration) return
    document.value = undefined
    pages.value = []
    markdown.value = ''
    sourcePath.value = ''
    loadError.value = true
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

/** 解析或索引还在跑时轮询，两者都落定就停 —— 判断依据统一在 constants/status.ts 的 active 标记。 */
const polling = usePolling(load, () => {
  const doc = document.value
  if (!doc) return false
  return Boolean(parseStatusOf(doc.status).active || indexStatusOf(doc.index_status).active ||
    compileStatusOf(doc.compile_status).active)
})

async function reload() {
  const mine = ++reloadGeneration
  await load()
  // load 跨了 await：卸载发生在飞行途中时后面的 start 会建一个没人清的
  // interval（DocumentsView 同款竞态）。只有还活着且是最新一次 reload 才起轮询。
  if (!alive || mine !== reloadGeneration) return
  polling.start()
}

function locate(citation: Citation) {
  selectedCitation.value = citation
  activePage.value = citation.page_idx
  selectedChunkId.value = citation.chunk_id
  highlights.value = [{
    pageIdx: citation.page_idx,
    // 历史 bbox 必须使用引用当时的坐标基准。page_size 缺失时不猜当前版本，
    // 否则 CropBox / 旋转归一化变化会把红框画到错误位置。
    bbox: citation.page_size ? citation.bbox : null,
    pageSize: citation.page_size,
    kind: 'citation',
    label: citation.snippet,
  }]
}

function selectBlock(block: Block) {
  selectedCitation.value = null
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

/**
 * 外部跳进来的定位（检索命中 / 抽取引用的出处）。
 *
 * 优先级：evidence_id > (parse_job_id, seq) > chunk_id > page。
 * 稳定定位键是后者。evidence_id 更进一步：连 bbox/page_size 都按
 * 引用当时的快照拿，不拿当前版本的尺寸猜（猜错会把红框画到错误位置）。
 */
function focusRequestedSource() {
  sourceFocusError.value = false
  const evidence = typeof route.query.evidence === 'string' && route.query.evidence
    ? route.query.evidence : undefined
  if (evidence) {
    void focusEvidence(evidence)
    return
  }
  focusBlockOrPage()
}

/** evidence/resolve 路径：按证据快照定位，失败回退到 chunk/page 参数。 */
async function focusEvidence(evidenceId: string) {
  const generation = loadGeneration
  try {
    const detail = (await conversationsApi.evidence(evidenceId)).data
    if (generation !== loadGeneration) return
    activePage.value = detail.page_idx
    selectedChunkId.value = detail.chunk_id
    selectedCitation.value = {
      evidence_id: detail.id, source_type: detail.source_type, derived_from: detail.derived_from,
      chunk_id: detail.chunk_id, parse_job_id: detail.parse_job_id, seq: detail.seq,
      page_idx: detail.page_idx, printed_page_label: detail.printed_page_label,
      bbox: detail.bbox, page_size: detail.page_size, crop_url: detail.crop_url,
      snippet: detail.content.slice(0, 200), score: 0, similarity: null, resolved: true,
    }
    highlights.value = [{
      pageIdx: detail.page_idx,
      // 与 locate() 同一条规则：page_size 缺失时不猜当前版本，bbox 置空。
      bbox: detail.page_size ? detail.bbox : null,
      pageSize: detail.page_size,
      kind: 'citation',
      label: detail.content.slice(0, 80),
    }]
  } catch {
    if (generation !== loadGeneration) return
    focusBlockOrPage()
  }
}

function focusBlockOrPage() {
  if (route.query.page !== undefined) {
    const page = typeof route.query.page === 'string' ? Number(route.query.page) : NaN
    if (Number.isSafeInteger(page) && page >= 1 && page <= (document.value?.page_count ?? 0)) {
      activePage.value = page - 1
    } else {
      sourceFocusError.value = true
    }
  }
  // seq 是稳定键（不随 reindex 重铸），优先于 chunk_id 匹配。
  const seqRaw = route.query.seq
  const seq = typeof seqRaw === 'string' && seqRaw !== '' ? Number(seqRaw) : NaN
  const chunk = route.query.chunk
  if (seqRaw === undefined && chunk === undefined) return
  if (Number.isSafeInteger(seq)) {
    for (const page of pages.value) {
      const block = page.blocks.find(candidate => candidate.seq === seq)
      if (block) {
        selectBlock(block)
        sourceFocusError.value = false
        return
      }
    }
  }
  if (typeof chunk === 'string' && chunk) {
    for (const page of pages.value) {
      const block = page.blocks.find(candidate => candidate.chunk_id === chunk)
      if (block) {
        selectBlock(block)
        sourceFocusError.value = false
        return
      }
    }
  }
  selectedCitation.value = null
  selectedChunkId.value = null
  highlights.value = []
  sourceFocusError.value = true
}

async function download(format: DownloadFormat) {
  const id = String(route.params.id)
  // 原件走签名直读，产物走应用进程 —— 两条路刻意不同（不变式 6）
  if (format === 'source') return downloadViaSignedUrl(id, document.value?.filename)
  await downloadAs(documentsApi.exportUrl(id, format,
    typeof route.query.job === 'string' ? route.query.job : undefined), document.value?.filename)
}

async function reindex() {
  validation.value = await validateAndReindex(String(route.params.id))
  ElMessage.success('已重新排队建立索引')
  await reload()
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
  () => [route.params.id, route.query.resource_id, route.query.version_id, route.query.job],
  async () => {
    document.value = undefined
    pages.value = []
    markdown.value = ''
    sourcePath.value = ''
    activePage.value = 0
    highlights.value = []
    selectedCitation.value = null
    selectedChunkId.value = null
    sourceFocusError.value = false
    await reload()
  },
  { immediate: true },
)

watch(() => [route.query.evidence, route.query.seq, route.query.chunk, route.query.page], () => {
  if (!loading.value && document.value?.status === 'succeeded') focusRequestedSource()
})
onUnmounted(() => {
  alive = false
  loadGeneration++
  reloadGeneration++
})
</script>

<template>
  <div class="workbench" v-loading="loading">
    <div class="head">
      <div class="title">
        <el-button link @click="router.push('/documents')">← 文档库</el-button>
        <span class="name">{{ document?.filename }}</span>
        <!-- 同一资源各版本文件名通常相同：不写版本号，用户分不清正在问的是哪一版 -->
        <span v-if="document?.source_version_no" class="meta">第 {{ document.source_version_no }} 版</span>
        <span class="meta">{{ document?.page_count }} 页</span>
        <el-tooltip v-if="document && document.index_status !== 'ready'"
                    :content="document.index_error" :disabled="!document.index_error">
          <StatusTag
            :meta="indexStatusOf(document.index_status)"
          />
        </el-tooltip>
      </div>
      <div class="actions">
        <el-button size="small" :disabled="!document"
          @click="router.push({ name: 'versions', params: { id: document?.id },
            query: { resource_id: document?.resource_id ?? undefined } })">
          解析版本
        </el-button>
        <template v-if="!onLocalSource">
          <!-- validate-index is a POST: a read-only desktop center rejects it (host 403) -->
          <el-button size="small" :disabled="!document || auth.readOnly" @click="validateIndex">校验版本</el-button>
          <el-button size="small" :disabled="!document || !auth.canUpload || !document.can_delete"
                     @click="reindex">重建索引</el-button>
        </template>
        <el-dropdown @command="download">
          <el-button size="small" :disabled="!document">下载<el-icon class="el-icon--right">▾</el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <template v-if="!onLocalSource">
                <el-dropdown-item command="md">Markdown</el-dropdown-item>
                <el-dropdown-item command="json">版面 JSON</el-dropdown-item>
                <el-dropdown-item command="zip">打包（含图片）</el-dropdown-item>
              </template>
              <el-dropdown-item command="source">原件</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>
    <p v-if="onReadOnlySource" class="readonly-hint" role="note">{{ approvedPlanLabel() }}</p>

    <el-alert v-if="loadError" type="error" :closable="false"
              title="文档读取失败。请重试，或从资源库选择固定版本。">
      <el-button @click="reload">重试读取</el-button>
      <el-button @click="router.push('/resources')">选择资源版本</el-button>
    </el-alert>
    <el-alert v-else-if="document?.status === 'failed'" type="error" :closable="false"
              :title="`解析失败：${document.error}`" />
    <el-alert v-else-if="document && document.status !== 'succeeded'" type="info" :closable="false"
              title="解析中，完成后自动刷新" />
    <el-alert v-if="sourceFocusError && !loadError" type="warning" :closable="false"
              title="无法定位所选的原文区域，请重新检索或选择解析段落。" />

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
            :current-page="activePage + 1"
            :page-count="document?.page_count ?? 0"
            :pager-count="5"
            layout="prev, pager, next"
            size="small"
            @update:current-page="activePage = $event - 1; highlights = []"
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
        <!-- 证据预览比格子高时在格内滚动；否则在两栏布局（≤1400px）里会盖住下方的问答栏。 -->
        <EvidencePreview
          v-if="selectedCitation?.evidence_id"
          :evidence-id="selectedCitation.evidence_id"
          :context="document ? documentContext(document) : undefined"
          @close="selectedCitation = null"
        />
        <ResultPane
          v-else
          :pages="pages"
          :markdown="markdown"
          :active-page="activePage"
          :selected-chunk-id="selectedChunkId"
          @block-click="selectBlock"
          @page-change="activePage = $event"
        />
      </section>

      <section class="pane ask">
        <p v-if="route.query.job && document?.current_job_id !== route.query.job">
          当前显示固定的历史解析修订。问答索引已切换，请从资源库选择当前修订开始新的问答。
        </p>
        <AskPanel v-else-if="document" :document="document" @locate="locate" />
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
  color: var(--ddp-ink-2);
  font-size: 13px;
}
.compile-degraded {
  border-left: 2px solid var(--ddp-warn);
  padding: 2px 10px;
  color: var(--ddp-ink-2);
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
/* 版本号、页数是元信息不是状态，按准则二排成普通文字 */
.meta {
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
  /* **栅格项默认 `min-width: auto`，即"不许窄于内容"。** 少了这一行，
     一张 200 列的表会把 `1fr` 这一列撑到七千多像素，整个工作台跟着横向滑
     —— 而 `.markdown-body` 明明写着 `overflow: auto`，只是永远轮不到它生效。
     阶段 8 的不破版门禁抓到的就是这个（实测 main 7520 > 1072）。 */
  min-width: 0;
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
.pane.result {
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
.readonly-hint { color: var(--ddp-ink-3); font-size: 13px; margin: 0; }
</style>
