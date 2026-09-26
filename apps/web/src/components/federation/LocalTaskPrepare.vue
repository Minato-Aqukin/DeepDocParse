<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { resourcesApi } from '@/api/resources'
import { getActiveSource, takeTaskPrefill, unwrap, workspaceError, type DesktopBridge, type Json } from '@/platform/desktop'
import { DraftWriter } from '@/platform/draft-writer'

type Row = Record<string, Json>

const rowOf = (value: unknown): Row =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Row) : {}
const textOf = (value: unknown): string => (typeof value === 'string' ? value : '')

/**
 * 桌面本机源的联邦任务准备页（`/tasks/new` 在 local 源下的形态）。
 *
 * 旧工作台的准备节搬入 AppShell：问题/用途（回答 | Wiki）、
 * 接收中心（已连接且就绪的中心源）、可信联邦参与者、锁定的本地输入（只锁摘要、
 * 不传原件）、保留策略与有效期。query 参数（`query` / `purpose` / `title`）预填，
 * 与「作为联邦任务发起」入口对应（AskPanel / WikiView / ResourcesView）。
 * 草稿走 `clientReadDraft` / `clientSaveDraft`（键 `federation-plan`），与旧面板同键。
 */
const bridge = window.ddpDesktop as DesktopBridge | undefined
const route = useRoute()
const router = useRouter()

const sourceId = computed(() => getActiveSource()?.sourceId ?? '')
const query = ref(typeof route.query.query === 'string' ? route.query.query : '')
const purpose = ref<'answer' | 'wiki'>(route.query.purpose === 'wiki' ? 'wiki' : 'answer')
const wikiTitle = ref(typeof route.query.title === 'string' ? route.query.title : '')
const wikiMaxPages = ref(4)
/** 任务类型：问答/Wiki 走 `clientPlanPropose`，本地文件远端解析走 `clientPlanProposeFile`。 */
const operation = ref<'query' | 'file'>('query')
const fileRef = ref('')
const centerId = ref('')
const template = ref<'center_only' | 'trusted_federation'>('center_only')
const participantIds = ref<string[]>([])
const inputs = ref<string[]>([])
const retention = ref<'temporary' | 'task_pinned'>('temporary')
const validMinutes = ref(120)
const centers = shallowRef<{ sourceId: string; label: string }[]>([])
const readyInputs = shallowRef<{ ref: string; filename: string; label: string; digest: string; sizeBytes: number }[]>([])
const error = ref('')
const notice = ref('')
const busy = ref(false)
const saved = ref('')
let alive = true
let loading = true
let writer: DraftWriter | undefined

const wikiParamsValid = computed(() => purpose.value === 'answer'
  || (!!wikiTitle.value.trim() && wikiTitle.value.trim().length <= 255
    && Number.isInteger(wikiMaxPages.value) && wikiMaxPages.value >= 1 && wikiMaxPages.value <= 12))
const fileEntry = computed(() => readyInputs.value.find((item) => item.ref === fileRef.value) ?? null)
const fileEntryValid = computed(() => operation.value === 'query'
  || (!!fileEntry.value && fileEntry.value.filename.length > 0 && fileEntry.value.filename.length <= 255))
const canSubmit = computed(() => !!bridge && !!sourceId.value && !busy.value
  && !!centerId.value && fileEntryValid.value
  && (operation.value === 'file' || (!!query.value.trim() && wikiParamsValid.value)))

function draft(): Json {
  return {
    query: query.value, purpose: purpose.value, wikiTitle: wikiTitle.value, wikiMaxPages: wikiMaxPages.value,
    operation: operation.value, fileRef: fileRef.value,
    centerId: centerId.value, template: template.value, participantConnectionIds: participantIds.value,
    inputs: inputs.value, retention: retention.value, validMinutes: validMinutes.value,
  }
}

function persist() {
  return loading || !writer ? Promise.resolve() : writer.write(draft())
}

watch([query, purpose, wikiTitle, wikiMaxPages, operation, fileRef, centerId, template, participantIds, inputs, retention, validMinutes],
  () => { void persist() }, { flush: 'sync', deep: true })

async function loadCenters() {
  if (!bridge) return
  try {
    const host = bridge as unknown as {
      sourceList?: () => Promise<{ ok: boolean; value?: { sourceId: string; kind: string; state: string; label: string }[] }>
    }
    const result = await host.sourceList?.()
    if (!result || !result.ok || !alive) return
    centers.value = (result.value ?? [])
      .filter((s) => s.kind === 'center' && s.state === 'ready')
      .map((s) => ({ sourceId: s.sourceId, label: s.label }))
    if (centerId.value && !centers.value.some((c) => c.sourceId === centerId.value)) centerId.value = ''
  } catch (cause) {
    if (alive) error.value = workspaceError(cause)
  }
}

/** 锁定的本地输入：只读已就绪固定版本的摘要与大小（本机源 `/api/resources`），不读原件字节。 */
async function loadReadyInputs() {
  if (!sourceId.value) return
  try {
    const ready: { ref: string; filename: string; label: string; digest: string; sizeBytes: number }[] = []
    for (let offset = 0; ; offset += 50) {
      const { data } = await resourcesApi.list('mine', offset)
      for (const resource of data.items) {
        for (const version of resource.versions) {
          if (version.parse_status === 'succeeded' && version.index_status === 'ready'
            && /^[0-9a-f]{64}$/.test(version.source_digest)
            && version.size_bytes > 0 && version.size_bytes <= 32 * 1024 * 1024) {
            // Two resources may hold files with the same name: label by resource + version.
            ready.push({ ref: version.id, filename: version.filename,
              label: `${resource.display_name} · v${version.version_no} · ${version.filename}`,
              digest: `sha256:${version.source_digest}`, sizeBytes: version.size_bytes })
          }
        }
      }
      if (!data.has_more || offset >= 1000) break
    }
    if (alive) readyInputs.value = ready
  } catch {
    // 没有已就绪输入不挡准备：计划可以不锁定输入。
    readyInputs.value = []
  }
}

async function propose() {
  if (!bridge || !canSubmit.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const key = crypto.randomUUID()
    const common = {
      connectionId: sourceId.value, centerConnectionId: centerId.value,
      retention: retention.value, validMinutes: validMinutes.value, idempotencyKey: key,
    }
    const entry = fileEntry.value
    if (operation.value === 'file' && !entry) return
    const created = rowOf(unwrap(operation.value === 'file' && entry
      ? await bridge.clientPlanProposeFile({
        ...common,
        // 文件名只是展示标签：必须与锁定的本地版本存量文件名逐字相同，后端会拒绝改名。
        filename: entry.filename,
        inputs: [{ ref: entry.ref, digest: entry.digest, sizeBytes: entry.sizeBytes }],
      })
      : await bridge.clientPlanPropose({
        ...common,
        query: query.value,
        purpose: purpose.value,
        ...(purpose.value === 'wiki'
          ? { wiki: { title: wikiTitle.value.trim(), max_pages: wikiMaxPages.value } }
          : {}),
        template: template.value,
        participantConnectionIds: template.value === 'trusted_federation' ? participantIds.value : [],
        inputs: readyInputs.value.filter((item) => inputs.value.includes(item.ref))
          .map(({ ref, digest, sizeBytes }) => ({ ref, digest, sizeBytes })),
      })))
    const planId = textOf(created.plan_id)
    if (!planId) throw new Error('invalid_response')
    notice.value = '已生成待审阅计划，正在打开审阅页。'
    await router.push({ name: 'federation-task-local', params: { planId } })
  } catch (cause) {
    if (alive) error.value = workspaceError(cause)
  } finally {
    if (alive) busy.value = false
  }
}

onMounted(async () => {
  if (!bridge || !sourceId.value) {
    loading = false
    return
  }
  try {
    const value = unwrap(await bridge.clientReadDraft({ connectionId: sourceId.value, key: 'federation-plan' }))
    if (!alive) return
    const data = rowOf(value?.value)
    const id = sourceId.value
    writer = new DraftWriter(value?.revision ?? 0, async (expectedRevision, next) =>
      unwrap(await bridge.clientSaveDraft({ connectionId: id,
        key: 'federation-plan', expectedRevision, value: next })).revision, (state) => {
      if (!alive) return
      saved.value = state.error ? '任务草稿保存失败' : state.pending ? '任务草稿保存中' : '任务草稿已保存在此工作区'
      if (state.error) error.value = workspaceError(state.error)
    })
    // 预填优先级：切源暂存（sessionStorage，按目标源）> URL query 参数 > 存量草稿。
    // 切源即整页重载：URL 是另一源的，暂存才是切源入口带过来的那一份。
    const stashed = takeTaskPrefill(id)
    if (stashed && (stashed.query || stashed.title)) {
      if (typeof stashed.query === 'string' && stashed.query) query.value = stashed.query
      if (stashed.purpose === 'wiki') {
        purpose.value = 'wiki'
        if (typeof stashed.title === 'string' && stashed.title) wikiTitle.value = stashed.title.slice(0, 255)
      } else {
        purpose.value = 'answer'
      }
    } else if (typeof route.query.query === 'string' && route.query.query) {
      // query 参数优先于存量草稿：入口带问题来就是要发起这件事。
      query.value = route.query.query
      if (route.query.purpose === 'wiki') {
        purpose.value = 'wiki'
        if (typeof route.query.title === 'string' && route.query.title) wikiTitle.value = route.query.title
      } else {
        purpose.value = data.purpose === 'wiki' ? 'wiki' : 'answer'
        if (typeof data.wikiTitle === 'string') wikiTitle.value = data.wikiTitle.slice(0, 255)
      }
    } else {
      query.value = textOf(data.query)
      purpose.value = data.purpose === 'wiki' ? 'wiki' : 'answer'
      if (typeof data.wikiTitle === 'string') wikiTitle.value = data.wikiTitle.slice(0, 255)
    }
    if (Number.isInteger(data.wikiMaxPages) && (data.wikiMaxPages as number) >= 1 && (data.wikiMaxPages as number) <= 12) {
      wikiMaxPages.value = data.wikiMaxPages as number
    }
    operation.value = data.operation === 'file' ? 'file' : 'query'
    fileRef.value = typeof data.fileRef === 'string' ? data.fileRef : ''
    centerId.value = textOf(data.centerId)
    template.value = data.template === 'trusted_federation' ? 'trusted_federation' : 'center_only'
    participantIds.value = Array.isArray(data.participantConnectionIds)
      ? data.participantConnectionIds.filter((item): item is string => typeof item === 'string') : []
    inputs.value = Array.isArray(data.inputs)
      ? data.inputs.filter((item): item is string => typeof item === 'string') : []
    retention.value = data.retention === 'task_pinned' ? 'task_pinned' : 'temporary'
    validMinutes.value = [30, 120, 1440].includes(Number(data.validMinutes)) ? Number(data.validMinutes) : 120
    loading = false
    await Promise.all([loadCenters(), loadReadyInputs()])
  } catch (cause) {
    if (alive) {
      error.value = workspaceError(cause)
      loading = false
    }
  }
})

onBeforeUnmount(() => {
  alive = false
  void persist()
})
</script>

<template>
  <section class="prepare" aria-label="准备联邦任务">
    <header>
      <RouterLink class="back" :to="{ name: 'federation-tasks' }">← 联邦任务</RouterLink>
      <h1>发起联邦任务</h1>
      <p class="muted">先生成计划并逐项审阅实际外发内容，分阶段批准后才派发给已连接的中心。计划、批准与对账记录都保存在本机。</p>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" class="ddp-degraded" aria-live="polite">{{ notice }}</p>
    <form aria-label="准备联邦任务" @submit.prevent="propose">
      <label for="task-operation">任务类型</label>
      <select id="task-operation" v-model="operation">
        <option value="query">问答 / Wiki（发送问题原文）</option>
        <option value="file">解析本地文件（远端解析一个已就绪版本）</option>
      </select>
      <template v-if="operation === 'file'">
        <label for="task-file">本地文件（锁定一个已就绪版本，文件名必须与存量一致）</label>
        <select id="task-file" v-model="fileRef" required>
          <option value="" disabled>选择已就绪版本</option>
          <option v-for="item in readyInputs" :key="item.ref" :value="item.ref">
            {{ item.label }} · {{ item.sizeBytes }} 字节
          </option>
        </select>
        <p v-if="!readyInputs.length" class="muted">此工作区没有已就绪的固定版本；先上传并等待解析完成。</p>
        <p v-else-if="operation === 'file' && !fileEntry" role="alert" class="error">请选择一个已就绪版本：文件任务一次只锁定一个输入。</p>
      </template>
      <template v-else>
      <label for="task-query">问题（批准后会原文发送给接收中心）</label>
      <textarea id="task-query" v-model="query" rows="3" maxlength="4096" />
      <label for="task-purpose">要什么</label>
      <select id="task-purpose" v-model="purpose">
        <option value="answer">生成回答</option>
        <option value="wiki">构建 Wiki 草稿</option>
      </select>
      <template v-if="operation === 'query' && purpose === 'wiki'">
        <label for="task-wiki-title">Wiki 标题（批准后随问题发送给该中心，只建新 Wiki）</label>
        <textarea id="task-wiki-title" v-model="wikiTitle" rows="2" maxlength="255" placeholder="用固定证据解释什么？" />
        <label>页数上限（1–12）
          <select v-model.number="wikiMaxPages" aria-label="Wiki 页数上限">
            <option v-for="n in 12" :key="n" :value="n">{{ n }} 页</option>
          </select>
        </label>
        <p v-if="!wikiParamsValid" role="alert" class="error">Wiki 草稿需要填写标题（不超过 255 字）；页数上限为 1–12。</p>
      </template>
      </template>
      <label for="task-center">接收方（已连接的中心）</label>
      <select id="task-center" v-model="centerId" required>
        <option value="" disabled>选择中心</option>
        <option v-for="center in centers" :key="center.sourceId" :value="center.sourceId">{{ center.label }}</option>
      </select>
      <p v-if="!centers.length" class="muted">还没有已连接的中心。先到数据源页连接一个中心。</p>
      <template v-if="operation === 'query'">
      <label for="task-template">执行范围</label>
      <select id="task-template" v-model="template">
        <option value="center_only">仅此中心，不转发给其他节点</option>
        <option value="trusted_federation">可信联邦，仅限我勾选的已连接节点</option>
      </select>
      <fieldset v-if="template === 'trusted_federation'" class="inputs">
        <legend>允许参与的节点 · 中心自动包含在内</legend>
        <label v-for="participant in centers.filter((item) => item.sourceId !== centerId)" :key="participant.sourceId" class="check">
          <input v-model="participantIds" type="checkbox" :value="participant.sourceId" />
          <span>{{ participant.label }}</span>
        </label>
        <p class="muted">探索批准后，问题可以发送给勾选节点；这不批准原件上传。中心的实际执行图仍须重新审阅并批准。</p>
      </fieldset>
      </template>
      <fieldset v-if="operation === 'query'" class="inputs">
        <legend>锁定的本地输入（只锁定摘要，不上传原件）</legend>
        <label v-for="item in readyInputs" :key="item.ref" class="check">
          <input v-model="inputs" type="checkbox" :value="item.ref" />
          <span>{{ item.label }}</span><span class="ddp-num">{{ item.sizeBytes }} 字节</span>
        </label>
        <p v-if="!readyInputs.length" class="muted">此工作区没有已就绪的固定版本；计划可以不锁定输入。</p>
      </fieldset>
      <div class="options">
        <label>保留策略
          <select v-model="retention" aria-label="保留策略">
            <option value="temporary">临时</option>
            <option value="task_pinned">任务期间保留</option>
          </select>
        </label>
        <label>有效期
          <select v-model.number="validMinutes" aria-label="有效期">
            <option :value="30">30 分钟</option>
            <option :value="120">2 小时</option>
            <option :value="1440">24 小时</option>
          </select>
        </label>
      </div>
      <p class="muted">{{ saved }}</p>
      <el-button type="primary" native-type="submit" :disabled="!canSubmit">生成待审阅计划</el-button>
    </form>
  </section>
</template>

<style scoped>
.prepare { max-width: 920px; margin: auto; display: grid; gap: 12px; }
.back { color: var(--ddp-ink-2); text-decoration: none; font-size: 13px; width: fit-content; min-height: 24px; }
.back:hover { text-decoration: underline; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
.muted { color: var(--ddp-ink-3); font-size: 13px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); }
form { display: grid; gap: 8px; }
form label, form legend { display: block; font-size: 13px; margin: 10px 0 6px; }
form select, form textarea { box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid var(--el-border-color); border-radius: 4px; background: var(--el-bg-color); color: inherit; font: inherit; }
.inputs { border: 0; padding: 0; margin: 10px 0 0; }
.inputs .check { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.inputs .check .ddp-num { margin-left: auto; color: var(--ddp-ink-3); }
.options { display: flex; gap: 20px; flex-wrap: wrap; }
.options label { flex: 1 1 200px; }
</style>
