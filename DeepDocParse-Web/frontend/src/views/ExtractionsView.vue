<script setup lang="ts">
import { Delete, Download, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { buildSchema, documentsApi, downloadAs, extractionsApi, readSchema } from '@/api'
import RecordTable from '@/components/extract/RecordTable.vue'
import SchemaEditor from '@/components/extract/SchemaEditor.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { RUN_STATUS, runStatusOf } from '@/constants/status'
import type {
  DocumentInfo,
  ExtractionItem,
  ExtractionRun,
  ExtractionTemplate,
  SchemaField,
  SchemaKind,
} from '@/types/api'
import { fetchAuthedImage } from '@/utils/markdown'

/**
 * 结构化抽取：模板 -> 选文档 -> 批量跑 -> 结果表格 -> 导出。
 *
 * **这是单一操作的批量化，不是工作流编排。** 不做条件分支、不做步骤串联、
 * 不做触发器 —— 那是另一个品类，在 README 的「明确不做」里写着"永不"。
 * 界面上刻意只有"跑一次"这一个动作，让这条边界是显然的。
 */
const router = useRouter()

const templates = ref<ExtractionTemplate[]>([])
const runs = ref<ExtractionRun[]>([])
const documents = ref<DocumentInfo[]>([])
const activeRun = ref<ExtractionRun | null>(null)
const items = ref<ExtractionItem[]>([])
const cropUrls = ref<Record<string, string>>({})
const loading = ref(false)

/* ---------- 模板编辑 ---------- */
const editing = ref(false)
const editId = ref<string | null>(null)
const form = ref({ name: '', description: '' })
const fields = ref<SchemaField[]>([])
const kind = ref<SchemaKind>('object')
const editor = ref<InstanceType<typeof SchemaEditor> | null>(null)

/* ---------- 发起抽取 ---------- */
const launching = ref(false)
const pick = ref<{ templateId: string; documentIds: string[]; name: string }>({
  templateId: '',
  documentIds: [],
  name: '',
})

// 只有索引就绪的文档能抽取。**在选择阶段就滤掉**，而不是让它们跑完变成一堆空结果 ——
// 空值看起来像"文档里没有"，那是抽取里最危险的误导
const selectable = computed(() => documents.value.filter((d) => d.index_status === 'ready'))
const notReadyCount = computed(() => documents.value.length - selectable.value.length)

let timer: number | undefined
// 组件是否还活着。**光靠 onUnmounted 里 clearTimeout 是不够的**：
// 定时器一旦已经触发进了 openRun（async），卸载时清掉的是那个已经跑完的 timer，
// 而 openRun 结束后又会 scheduleRefresh() 排一个新的 —— 于是永久轮询。
// 同一竞态下 loadCrops 还会继续 createObjectURL，而 revoke 只在 onUnmounted 发生一次
let alive = true

async function loadAll() {
  loading.value = true
  try {
    const [t, r, d] = await Promise.all([
      extractionsApi.listTemplates(),
      extractionsApi.listRuns(),
      documentsApi.list({ limit: 200 }),
    ])
    templates.value = t
    runs.value = r
    documents.value = d.data
  } finally {
    loading.value = false
  }
}

async function openRun(run: ExtractionRun) {
  activeRun.value = run
  const detail = await extractionsApi.getRun(run.id)
  if (!alive) return          // 请求飞行途中组件被卸载了，后面全都别做
  activeRun.value = detail.run
  items.value = detail.items
  await loadCrops()
  scheduleRefresh()
}

/** 跑动中的 run 要轮询。终态就停 —— 无限轮询会把后端与电池一起耗掉。 */
function scheduleRefresh() {
  window.clearTimeout(timer)
  if (!alive) return
  const status = activeRun.value?.status
  if (status && RUN_STATUS[status]?.active) {
    timer = window.setTimeout(() => alive && activeRun.value && openRun(activeRun.value), 2000)
  }
}

async function loadCrops() {
  const pending = items.value
    .flatMap((i) => Object.values(i.fields))
    .flatMap((f) => f.citations || [])
    .filter((c) => c.crop_url && !cropUrls.value[c.crop_url])
  await Promise.all(
    pending.map(async (c) => {
      const objectUrl = await fetchAuthedImage(c.crop_url!)
      if (!objectUrl) return
      // 卸载后到手的 blob 立刻回收：存进 cropUrls 的话 onUnmounted 已经跑过了，
      // 再也没有人 revoke 它
      if (!alive) URL.revokeObjectURL(objectUrl)
      else cropUrls.value[c.crop_url!] = objectUrl
    }),
  )
}

function cropUrlOf(_documentId: string, cropUrl: string) {
  return cropUrls.value[cropUrl]
}

/** 点单元格的出处 -> 跳到那份文档的工作台并定位到该页。 */
function locate(documentId: string, citation: unknown) {
  const page = (citation as { page_idx?: number }).page_idx ?? 0
  router.push({ name: 'workbench', params: { id: documentId }, query: { page: String(page + 1) } })
}

/* ---------- 模板 ---------- */

function newTemplate() {
  editId.value = null
  form.value = { name: '', description: '' }
  fields.value = [
    { name: '', type: 'string', description: '', format: '', enum: [], required: false },
  ]
  kind.value = 'object'
  editing.value = true
}

function editTemplate(row: ExtractionTemplate) {
  editId.value = row.id
  form.value = { name: row.name, description: row.description }
  const parsed = readSchema(row.schema_json)
  fields.value = parsed.fields
  kind.value = parsed.kind
  editing.value = true
}

async function saveTemplate() {
  const problems = editor.value?.problems ?? []
  if (problems.length) {
    ElMessage.error(problems[0])
    return
  }
  if (!form.value.name.trim()) {
    ElMessage.error('模板需要一个名字')
    return
  }
  const payload = {
    name: form.value.name.trim(),
    description: form.value.description,
    schema_json: buildSchema(fields.value, kind.value),
  }
  if (editId.value) await extractionsApi.updateTemplate(editId.value, payload)
  else await extractionsApi.createTemplate(payload)
  editing.value = false
  await loadAll()
}

async function removeTemplate(row: ExtractionTemplate) {
  await ElMessageBox.confirm(
    `删除模板「${row.name}」？已经跑过的抽取结果不受影响（它们存的是 schema 快照）。`,
    '确认删除',
    { type: 'warning' },
  )
  await extractionsApi.deleteTemplate(row.id)
  await loadAll()
}

/* ---------- 发起 ---------- */

function openLaunch() {
  if (!templates.value.length) {
    ElMessage.warning('先建一个抽取模板')
    return
  }
  pick.value = {
    templateId: templates.value[0]!.id,
    documentIds: [],
    name: '',
  }
  launching.value = true
}

async function launch() {
  if (!pick.value.documentIds.length) {
    ElMessage.error('至少选一份文档')
    return
  }
  const run = await extractionsApi.createRun({
    document_ids: pick.value.documentIds,
    template_id: pick.value.templateId,
    name: pick.value.name || templates.value.find((t) => t.id === pick.value.templateId)?.name,
  })
  launching.value = false
  await loadAll()
  await openRun(run)
}

async function removeRun(row: ExtractionRun) {
  await ElMessageBox.confirm(`删除抽取任务「${row.name}」及其全部结果？`, '确认删除', {
    type: 'warning',
  })
  await extractionsApi.deleteRun(row.id)
  if (activeRun.value?.id === row.id) {
    activeRun.value = null
    items.value = []
  }
  await loadAll()
}

function exportCsv() {
  if (!activeRun.value) return
  downloadAs(extractionsApi.exportUrl(activeRun.value.id), 'extraction.csv')
}

watch(() => activeRun.value?.status, scheduleRefresh)

onUnmounted(() => {
  alive = false
  window.clearTimeout(timer)
  Object.values(cropUrls.value).forEach((u) => URL.revokeObjectURL(u))
})

loadAll()
</script>

<template>
  <div class="extractions">
    <section class="panel">
      <header class="panel-head">
        <h2>抽取模板</h2>
        <div>
          <el-button :icon="Plus" size="small" type="primary" @click="newTemplate">
            新建模板
          </el-button>
          <el-button :icon="Refresh" size="small" @click="loadAll">刷新</el-button>
        </div>
      </header>
      <el-table :data="templates" size="small" v-loading="loading">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="字段" width="70">
          <template #default="{ row }">{{ row.field_count }}</template>
        </el-table-column>
        <el-table-column label="形态" width="120">
          <template #default="{ row }">
            {{ row.kind === 'array' ? '多条记录' : '单条记录' }}
          </template>
        </el-table-column>
        <el-table-column width="110" align="right">
          <template #default="{ row }">
            <el-button :icon="Edit" text size="small" @click="editTemplate(row)" />
            <el-button :icon="Delete" text size="small" @click="removeTemplate(row)" />
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !templates.length" description="还没有模板" :image-size="60" />
    </section>

    <section class="panel">
      <header class="panel-head">
        <h2>抽取任务</h2>
        <el-button :icon="Plus" size="small" type="primary" @click="openLaunch">
          发起抽取
        </el-button>
      </header>
      <el-table :data="runs" size="small" highlight-current-row
                @current-change="(r: ExtractionRun) => r && openRun(r)">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <StatusTag :meta="runStatusOf(row.status)" />
          </template>
        </el-table-column>
        <el-table-column label="进度" width="100">
          <template #default="{ row }">{{ row.done_count }} / {{ row.document_count }}</template>
        </el-table-column>
        <el-table-column label="发起时间" width="170">
          <template #default="{ row }">
            {{ new Date(row.created_at).toLocaleString('zh-CN') }}
          </template>
        </el-table-column>
        <el-table-column width="60" align="right">
          <template #default="{ row }">
            <el-button :icon="Delete" text size="small" @click.stop="removeRun(row)" />
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !runs.length" description="还没有抽取任务" :image-size="60" />
    </section>

    <section v-if="activeRun" class="panel">
      <header class="panel-head">
        <h2>
          {{ activeRun.name }}
          <StatusTag :meta="runStatusOf(activeRun.status)" />
        </h2>
        <el-button :icon="Download" size="small" @click="exportCsv">导出 CSV</el-button>
      </header>
      <p v-if="activeRun.error" class="run-error">{{ activeRun.error }}</p>
      <p class="hint">
        点任意单元格看它的出处：页码、区域截图，以及这个值到底是从原件的哪一块抽出来的。
      </p>
      <RecordTable
        :items="items"
        :field-names="activeRun.field_names"
        :crop-url-of="cropUrlOf"
        @locate="locate"
      />
    </section>

    <el-dialog v-model="editing" :title="editId ? '编辑模板' : '新建模板'" width="820px">
      <el-form label-width="72px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="采购合同要素" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="用于批量抽取采购合同的关键条款" />
        </el-form-item>
      </el-form>
      <SchemaEditor ref="editor" v-model:fields="fields" v-model:kind="kind" />
      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="launching" title="发起抽取" width="620px">
      <el-form label-width="72px">
        <el-form-item label="模板">
          <el-select v-model="pick.templateId" class="fill">
            <el-option v-for="t in templates" :key="t.id" :value="t.id"
                       :label="`${t.name}（${t.field_count} 个字段）`" />
          </el-select>
        </el-form-item>
        <el-form-item label="任务名">
          <el-input v-model="pick.name" placeholder="留空则用模板名" />
        </el-form-item>
        <el-form-item label="文档">
          <el-select v-model="pick.documentIds" multiple filterable class="fill"
                     placeholder="选择要抽取的文档">
            <el-option v-for="d in selectable" :key="d.id" :value="d.id" :label="d.filename" />
          </el-select>
          <p v-if="notReadyCount" class="hint">
            另有 {{ notReadyCount }} 份文档索引未就绪，暂时不能抽取（抽取依赖检索定位）。
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="launching = false">取消</el-button>
        <el-button type="primary" @click="launch">开始</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.extractions {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.panel-head h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 15px;
}
.hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
/* 红只属于出处与出错（视觉规范准则一） */
.run-error {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--ddp-cite);
}
.fill {
  width: 100%;
}
</style>
