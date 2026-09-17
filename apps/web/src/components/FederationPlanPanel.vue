<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import StatusTag from '@/components/common/StatusTag.vue'
import PlanSummary from '@/components/federation/PlanSummary.vue'
import { DELIVERY_STATE, PLANNING_STATE, metaOf, retentionLabel } from '@/constants/federation'
import type { StatusMeta } from '@/constants/status'
import type { TaskPlan } from '@/federation/task-model'
import { unwrap, workspaceError, type ConnectionSummary, type DesktopBridge, type Json, type PlanDetail } from '@/platform/desktop'
import { DraftWriter } from '@/platform/draft-writer'

type Row = Record<string, Json>
const props = defineProps<{ bridge: DesktopBridge; connectionId: string; ready: boolean; pending: boolean
  centers: ConnectionSummary[]; resources: Row[] }>()
const row = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const rows = (value: unknown): Row[] => Array.isArray(value) ? value.map(row) : []
const text = (value: unknown) => typeof value === 'string' ? value : ''
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
const clock = (value: unknown) => typeof value === 'number' ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'

const query = ref(''), centerId = ref(''), inputs = ref<string[]>([]), retention = ref<'temporary' | 'task_pinned'>('temporary')
const validMinutes = ref(120), selectedPlan = ref(''), planKey = ref(''), planAction = ref('')
const listing = shallowRef<Row>({}), detail = shallowRef<PlanDetail | null>(null)
const error = ref(''), notice = ref(''), busy = ref(false), saved = ref('')
let alive = true, loading = true, generation = 0, writer: DraftWriter | undefined

const plan = computed(() => row(detail.value?.plan)), scope = computed(() => row(plan.value.scope))
const federation = computed(() => detail.value?.federation ? row(detail.value.federation) : null)
const delivery = computed(() => row(federation.value?.delivery))
const consents = computed(() => row(plan.value.consents))
const approvedPlanDigest = computed(() => text(row(scope.value.plan).plan_digest))
const centerPlanDigest = computed(() => text(federation.value?.center_plan_digest))
const planDiverged = computed(() => !!centerPlanDigest.value && centerPlanDigest.value !== approvedPlanDigest.value)
const readyInputs = computed(() => props.resources.filter(item => item.state === 'ready' && typeof item.source_digest === 'string'
  && typeof item.size_bytes === 'number').map(item => ({ ref: text(item.version_id) || text(item.id), filename: text(item.filename),
  digest: 'sha256:' + text(item.source_digest), sizeBytes: Number(item.size_bytes) })))
const locked = computed(() => !props.ready || props.pending || busy.value || !!planKey.value)
const revoked = computed(() => plan.value.revoked === true || plan.value.planning_state === 'invalidated')
const fedState = computed(() => text(federation.value?.state))
/**
 * 计划修订（步骤 / 数据边 / 中继 / 保留 / 预算）的展示**只有一份实现**：
 * `components/federation/PlanSummary.vue`（铁律 4）。桌面端与 Web 协调者拿到的是同一份
 * DDP TaskPlan（`ddp-plan-admission/1#TaskPlan`），所以这里只做形状收窄，不再画第二张表。
 * 桌面端多出来的是"这一步会把什么发给谁、经过谁中继、对方保留多久"里**许可**的那一半：
 * 按阶段的载荷绑定、已审阅的传输绑定、锁定但不外发的本地输入 —— 那几节在下面。
 */
const revision = computed<TaskPlan | null>(() => {
  const candidate = scope.value.plan as unknown as TaskPlan | undefined
  if (!candidate || typeof candidate !== 'object') return null
  return typeof candidate.plan_digest === 'string' && Array.isArray(candidate.steps)
    && Array.isArray(candidate.data_edges) && !!candidate.budget && typeof candidate.budget === 'object'
    ? candidate : null
})

/**
 * `planning_state` / `delivery_state` / `retention_class` 的取值与中文都是契约生成物
 * （`enums.yaml` → `@deepdocparse/contracts`），这里只查表，**不许再写第二份中文**（铁律 1）。
 * 手写过一版，四个取值里三个与契约不一样，而且漏了 `draft`/`awaiting_approval`。
 *
 * 下面两张表是**本机运行时自己的状态**，契约里没有对应枚举：
 * `FEDERATION` 是 `ddp_local` 派发状态机（`federation_dispatch.py` 的 `state`），
 * `VERIFY` 是宿主对本地交付字节重算摘要的结果（`bridge.d.ts` 的 `PlanDetail.verification`）。
 * 两张表都走 `tag()` 的兜底：认不出来的取值原样显示代码，不留白（不变式 2）。
 */
const FEDERATION: Record<string, StatusMeta> = {
  prepared: { label: '尚未派发', type: 'primary' }, exploring: { label: '中心规划中', type: 'info', active: true },
  planned: { label: '中心计划已就绪', type: 'info', active: true }, explore_unknown: { label: '探索结果未知 · 需对账', type: 'warning', active: true },
  submitted: { label: '中心执行中', type: 'info', active: true }, submit_unknown: { label: '提交结果未知 · 需对账', type: 'warning', active: true },
  approved: { label: '中心已批准', type: 'info', active: true }, succeeded: { label: '中心已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' }, cancelled: { label: '已取消', type: 'primary' },
  delivered: { label: '已交付', type: 'success' },
}
const VERIFY: Record<string, StatusMeta> = {
  passed: { label: '本地重算摘要一致', type: 'success' }, failed: { label: '本地重算摘要不一致', type: 'danger' },
  unavailable: { label: '尚无可校验的本地结果', type: 'primary' },
}
const tag = (table: Record<string, StatusMeta>, value: string): StatusMeta => table[value] ?? { label: `未知取值（${value || '—'}）`, type: 'warning' }
/** `payload_kind` 内联在 `ddp-plan-admission/1.json` 里，不是 `enums.yaml` 的枚举；认不出的原样显示。 */
const PAYLOAD: Record<string, string> = { query_text: '检索词原文', source_files: '原始文件', evidence_excerpts: '证据片段' }
const PHASE: Record<string, string> = { exploration: '探索', execution: '执行' }
// Codes where the operation may have happened: keep the key and reconcile by receipt.
const UNCERTAIN = new Set(['outcome_unknown', 'host_operation_failed', 'connection_failed', 'disposed', 'receipt_required'])

function draft(): Json { return { query: query.value, centerId: centerId.value, inputs: inputs.value, retention: retention.value,
  validMinutes: validMinutes.value, selectedPlan: selectedPlan.value, planKey: planKey.value, planAction: planAction.value } }
function persist() { return loading || !writer ? Promise.resolve() : writer.write(draft()) }
watch([query, centerId, inputs, retention, validMinutes, selectedPlan, planKey, planAction], () => { void persist() }, { flush: 'sync', deep: true })

async function list() {
  if (!props.ready) return
  const mine = generation
  try {
    const value = row(unwrap(await props.bridge.clientPlanList({ connectionId: props.connectionId })))
    if (alive && mine === generation) listing.value = value
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
}
async function open(planId: string) {
  if (!planId || !props.ready) return
  const mine = ++generation
  try {
    const value = unwrap(await props.bridge.clientPlanGet({ connectionId: props.connectionId, planId }))
    if (!alive || mine !== generation) return
    detail.value = value; selectedPlan.value = planId; error.value = ''
  } catch (cause) { if (alive && mine === generation) error.value = workspaceError(cause) }
}
/** One keyed write at a time; the key is durable in the host draft before the request leaves. */
async function keyed(action: string, run: (key: string) => Promise<unknown>) {
  if (locked.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    planKey.value = crypto.randomUUID(); planAction.value = action; await persist()
    if (!writer?.durable || !alive) throw new Error('cache_failure')
    await run(planKey.value)
    planKey.value = ''; planAction.value = ''; await persist()
  } catch (cause) {
    const code = cause instanceof Error ? cause.message : ''
    if (!UNCERTAIN.has(code)) { planKey.value = ''; planAction.value = ''; await persist() }
    if (alive) error.value = workspaceError(cause)
  } finally {
    if (alive) busy.value = false
    await list()
    if (selectedPlan.value) await open(selectedPlan.value)
  }
}
function propose() {
  const center = centerId.value, chosen = readyInputs.value.filter(item => inputs.value.includes(item.ref))
  return keyed('propose', async key => {
    const created = row(unwrap(await props.bridge.clientPlanPropose({ connectionId: props.connectionId, centerConnectionId: center,
      query: query.value, inputs: chosen.map(({ ref, digest, sizeBytes }) => ({ ref, digest, sizeBytes })), retention: retention.value,
      validMinutes: validMinutes.value, idempotencyKey: key })))
    selectedPlan.value = text(created.plan_id)
  })
}
const approve = (phase: 'exploration' | 'execution') => keyed('approve-' + phase, async key => {
  unwrap(await props.bridge.clientPlanApprove({ connectionId: props.connectionId, planId: selectedPlan.value, phase,
    scopeDigest: text(plan.value.scope_digest), userConfirmed: true, idempotencyKey: key }))
})
const revoke = () => keyed('revoke', async key => {
  unwrap(await props.bridge.clientPlanRevoke({ connectionId: props.connectionId, planId: selectedPlan.value, idempotencyKey: key }))
})
const dispatch = (phase: 'exploration' | 'execution') => keyed('dispatch-' + phase, async key => {
  const state = row(unwrap(await props.bridge.clientPlanDispatch({ connectionId: props.connectionId, planId: selectedPlan.value, phase, idempotencyKey: key })))
  // A center refusal or an unknown center outcome is persisted and returned, not thrown.
  const failure = text(row(state.error).code)
  if (failure) notice.value = `中心未确认此次派发：${workspaceError(new Error(failure))}（${failure}）`
})
const confirm = () => keyed('confirm', async key => {
  const state = row(unwrap(await props.bridge.clientPlanConfirmDelivery({ connectionId: props.connectionId, planId: selectedPlan.value,
    deliveryId: text(delivery.value.id), resultManifestDigest: text(delivery.value.result_manifest_digest), idempotencyKey: key })))
  const failure = text(row(state.error).code) || (row(state.delivery).state !== 'confirmed' ? text(row(state.delivery).reason) : '')
  if (failure) notice.value = `交付尚未确认：${workspaceError(new Error(failure))}（${failure}）。可以再次确认，不会重复发布。`
})
async function read(kind: 'reconcile' | 'fetch') {
  if (locked.value || !selectedPlan.value) return
  busy.value = true; error.value = ''; notice.value = ''
  const mine = ++generation
  try {
    const input = { connectionId: props.connectionId, planId: selectedPlan.value }
    const value = unwrap(kind === 'reconcile' ? await props.bridge.clientPlanReconcile(input) : await props.bridge.clientPlanFetchDelivery(input))
    if (!alive || mine !== generation) return
    detail.value = value
    const failure = text(row(row(value.federation).error).code)
    if (failure) notice.value = `中心暂未给出结果：${workspaceError(new Error(failure))}（${failure}）`
    await list()
  } catch (cause) { if (alive && mine === generation) error.value = workspaceError(cause) }
  finally { if (alive) busy.value = false }
}
async function receipt() {
  if (!planKey.value || !props.ready) return
  try {
    const value = unwrap(await props.bridge.clientReceipt({ connectionId: props.connectionId, idempotencyKey: planKey.value }))
    if (!alive) return
    if (value === null) { error.value = '尚未找到这次操作的回执；保留操作编号，不会自动重复发送。'; return }
    planKey.value = ''; planAction.value = ''; error.value = ''; await persist()
    await list(); if (selectedPlan.value) await open(selectedPlan.value)
  } catch (cause) { if (alive) error.value = workspaceError(cause) }
}
onMounted(async () => {
  try {
    const value = unwrap(await props.bridge.clientReadDraft({ connectionId: props.connectionId, key: 'federation-plan' }))
    if (!alive) return
    const data = row(value?.value), connectionId = props.connectionId
    writer = new DraftWriter(value?.revision ?? 0, async (expectedRevision, next) => unwrap(await props.bridge.clientSaveDraft({ connectionId,
      key: 'federation-plan', expectedRevision, value: next })).revision, state => {
      if (!alive) return
      saved.value = state.error ? '计划草稿保存失败' : state.pending ? '计划草稿保存中' : '计划草稿已保存在此工作区'
      if (state.error) error.value = workspaceError(state.error)
    })
    query.value = text(data.query); centerId.value = text(data.centerId)
    inputs.value = Array.isArray(data.inputs) ? data.inputs.filter((item): item is string => typeof item === 'string') : []
    retention.value = data.retention === 'task_pinned' ? 'task_pinned' : 'temporary'
    validMinutes.value = [30, 120, 1440].includes(Number(data.validMinutes)) ? Number(data.validMinutes) : 120
    selectedPlan.value = text(data.selectedPlan); planKey.value = text(data.planKey); planAction.value = text(data.planAction)
    loading = false
    if (props.ready) { await list(); if (selectedPlan.value) await open(selectedPlan.value) }
  } catch (cause) { if (alive) { error.value = workspaceError(cause); loading = false } }
})
watch(() => props.ready, ready => { if (ready && !loading) { void list(); if (selectedPlan.value && !detail.value) void open(selectedPlan.value) } })
onBeforeUnmount(() => { void persist(); alive = false; generation++ })
</script>

<template>
  <section class="plan-panel">
    <h1>远端计划</h1>
    <p class="muted">从本机工作区发起：先生成计划并逐项审阅实际外发内容，分阶段批准后才派发给已配对中心。计划、批准与对账记录都保存在本机。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" class="ddp-degraded" aria-live="polite">{{ notice }}</p>
    <p v-if="planKey" class="ddp-degraded pending-plan">有一项计划操作结果未知（{{ planAction }}）。<code class="ddp-mono">{{ planKey }}</code>
      <el-button text :disabled="!ready" @click="receipt">查询回执</el-button></p>

    <form class="plan-form" aria-label="准备中心计划" @submit.prevent="propose">
      <h2>准备计划</h2>
      <label for="plan-center">接收方（已配对中心）</label>
      <select id="plan-center" v-model="centerId" required>
        <option value="" disabled>选择中心</option>
        <option v-for="center in centers" :key="center.connectionId" :value="center.connectionId" :disabled="center.view.transport !== 'ready'">
          {{ center.label }} · {{ center.environment.authorityNodeId }}{{ center.view.transport !== 'ready' ? '（未连接，不能作为接收方）' : '' }}</option>
      </select>
      <p v-if="!centers.length" class="muted">尚未配对中心。先用左侧「配对中心…」核对节点身份。</p>
      <label for="plan-query">检索词（批准后会原文发送给该中心）</label>
      <textarea id="plan-query" v-model="query" rows="3" maxlength="4096" />
      <fieldset class="plan-inputs">
        <legend>锁定的本地输入（只锁定摘要，不上传原件）</legend>
        <label v-for="item in readyInputs" :key="item.ref" class="check"><input v-model="inputs" type="checkbox" :value="item.ref" />
          <span>{{ item.filename || item.ref }}</span><span class="ddp-num">{{ count(item.sizeBytes) }} 字节</span></label>
        <p v-if="!readyInputs.length" class="muted">此工作区没有已就绪的固定版本；计划可以不锁定输入。</p>
      </fieldset>
      <div class="plan-options">
        <label>保留策略<select v-model="retention" aria-label="保留策略"><option value="temporary">临时</option><option value="task_pinned">任务期间保留</option></select></label>
        <label>有效期<select v-model.number="validMinutes" aria-label="有效期"><option :value="30">30 分钟</option><option :value="120">2 小时</option><option :value="1440">24 小时</option></select></label>
      </div>
      <p class="muted">{{ saved }}</p>
      <el-button type="primary" native-type="submit" :disabled="locked || !centerId || !query.trim()">生成待审阅计划</el-button>
    </form>

    <section class="plan-list" aria-label="本机计划记录">
      <div class="heading"><h2>计划记录</h2><el-button text :disabled="!ready" @click="list">刷新</el-button></div>
      <p class="muted">下列状态来自本机镜像；中心的最新状态以「对账」结果为准。</p>
      <button v-for="item in rows(listing.items)" :key="text(item.plan_id)" class="plan-row" :aria-current="item.plan_id === selectedPlan ? 'true' : undefined" @click="open(text(item.plan_id))">
        <code class="ddp-mono">{{ item.plan_id }}</code>
        <StatusTag :meta="metaOf(PLANNING_STATE, text(item.planning_state))" />
        <StatusTag v-if="item.federation" :meta="tag(FEDERATION, text(row(item.federation).state))" />
        <StatusTag v-if="row(item.federation).delivery_state" :meta="metaOf(DELIVERY_STATE, text(row(item.federation).delivery_state))" />
      </button>
      <p v-if="!rows(listing.items).length" class="muted">尚无计划。</p>
    </section>

    <article v-if="detail" class="plan-review" aria-label="计划审阅">
      <div class="heading"><h2>审阅 <code class="ddp-mono">{{ plan.plan_id }}</code></h2><StatusTag :meta="metaOf(PLANNING_STATE, text(plan.planning_state))" /></div>
      <dl class="facts">
        <dt>范围摘要</dt><dd class="ddp-mono">{{ plan.scope_digest }}</dd>
        <dt>保留策略</dt><dd>{{ retentionLabel(text(scope.retention)) }}</dd>
        <dt>输出位置</dt><dd class="ddp-mono">{{ (scope.output_locations as string[] | undefined)?.join('、') }}</dd>
      </dl>

      <h3>实际外发内容</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>阶段</th><th>内容</th><th>接收方</th><th class="num">字节</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="item in rows(scope.payload_bindings)" :key="text(item.payload_id)">
          <td>{{ PHASE[text(item.phase)] || item.phase }}</td><td>{{ PAYLOAD[text(item.payload_kind)] || item.payload_kind }}</td>
          <td class="ddp-mono">{{ item.recipient_node_id }}</td><td class="ddp-num num">{{ count(item.size_bytes) }}</td><td class="ddp-mono">{{ item.digest }}</td>
        </tr></tbody>
      </table></div>
      <p class="quoted">检索词原文：{{ row(scope.task_spec).query }}</p>

      <h3>接收方与传输</h3>
      <dl v-for="item in rows(scope.transport_bindings)" :key="text(item.transport_ref)" class="facts">
        <dt>节点身份</dt><dd class="ddp-mono">{{ item.recipient_node_id }}</dd><dt>地址</dt><dd class="ddp-mono">{{ item.endpoint }}</dd>
        <dt>中心工作区</dt><dd class="ddp-mono">{{ item.workspace_id }}</dd><dt>用户</dt><dd class="ddp-mono">{{ item.subject }}</dd>
      </dl>

      <h3>计划修订 · 谁执行、数据流向哪里</h3>
      <!-- 与 Web 协调者同一份只读展示组件；数据边与预算不在这里重画一遍。 -->
      <PlanSummary v-if="revision" :plan="revision" />
      <p v-else class="ddp-degraded is-danger">本机镜像里的计划结构不完整，步骤与数据边无法展示。不要据此批准。</p>

      <h3>锁定的本地输入 · 不外发</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>版本</th><th class="num">字节</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="item in rows(scope.input_manifest)" :key="text(item.ref)"><td class="ddp-mono">{{ item.ref }}</td>
          <td class="ddp-num num">{{ count(item.size_bytes) }}</td><td class="ddp-mono">{{ item.digest }}</td></tr></tbody>
      </table></div>
      <p v-if="!rows(scope.input_manifest).length" class="muted">未锁定本地输入。</p>

      <!-- 计划总预算在上面的计划修订里；这里只有探索许可自己的预算，它不在 TaskPlan 上。 -->
      <h3>探索阶段预算</h3>
      <dl class="facts exploration-budget">
        <dt>探索请求上限</dt><dd class="ddp-num">{{ count(row(row(scope.exploration).budget).max_probe_requests) }}</dd>
        <dt>探索字节上限</dt><dd class="ddp-num">{{ count(row(row(scope.exploration).budget).max_egress_bytes) }}</dd>
      </dl>

      <div class="actions">
        <el-button type="primary" :disabled="locked || revoked || !!consents.exploration" @click="approve('exploration')">批准探索…</el-button>
        <el-button type="primary" :disabled="locked || revoked || !consents.exploration || !!consents.execution" @click="approve('execution')">批准执行…</el-button>
        <el-button :disabled="locked || revoked" @click="revoke">撤销批准</el-button>
      </div>
      <p class="muted">批准会弹出系统确认框，再次列出上面的接收方与外发内容；只有在系统确认框里批准才生效。</p>

      <h3>派发与对账</h3>
      <p class="status-line"><StatusTag v-if="federation" :meta="tag(FEDERATION, fedState)" /><span v-else class="muted">尚未派发</span>
        <span v-if="federation?.root_task_id" class="muted">中心任务 <code class="ddp-mono">{{ federation.root_task_id }}</code></span>
        <span v-if="federation" class="muted">最近对账 <span class="ddp-num">{{ clock(row(federation.reconcile).at) }}</span></span></p>
      <p v-if="federation?.last_error" role="alert" class="error">{{ workspaceError(new Error(text(row(federation.last_error).code))) }} <code class="ddp-mono">{{ row(federation.last_error).code }}</code></p>
      <p v-if="['explore_unknown', 'submit_unknown'].includes(fedState)" class="ddp-degraded">中心可能已受理但回执丢失。请先对账；不会自动重发。</p>
      <p v-if="planDiverged" class="ddp-degraded is-danger">中心规划的计划（<code class="ddp-mono">{{ centerPlanDigest }}</code>）与已批准计划（<code class="ddp-mono">{{ approvedPlanDigest }}</code>）不同；执行已被拒绝，需要基于中心计划重新审阅。</p>
      <div class="actions">
        <el-button :disabled="locked || revoked || !consents.exploration || (!!federation && fedState !== 'explore_unknown')" @click="dispatch('exploration')">派发探索</el-button>
        <el-button :disabled="locked || revoked || !consents.execution || !federation?.root_task_id || planDiverged || !['planned', 'submit_unknown'].includes(fedState)" @click="dispatch('execution')">派发执行</el-button>
        <el-button :disabled="locked || !federation?.root_task_id" @click="read('reconcile')">对账</el-button>
      </div>

      <h3>交付</h3>
      <p class="status-line"><StatusTag v-if="delivery.state" :meta="metaOf(DELIVERY_STATE, text(delivery.state))" /><span v-else class="muted">尚无交付</span>
        <span v-if="delivery.id" class="muted">交付 <code class="ddp-mono">{{ delivery.id }}</code></span></p>
      <p v-if="delivery.reason" role="alert" class="error">{{ workspaceError(new Error(text(delivery.reason))) }} <code class="ddp-mono">{{ delivery.reason }}</code></p>
      <dl v-if="detail.verification.expected || detail.verification.state !== 'unavailable'" class="facts">
        <dt>本地校验</dt><dd><StatusTag :meta="tag(VERIFY, detail.verification.state)" /></dd>
        <dt>中心声明摘要</dt><dd class="ddp-mono">{{ detail.verification.expected }}</dd>
        <dt>本地重算摘要</dt><dd class="ddp-mono">{{ detail.verification.actual || '—' }}</dd>
      </dl>
      <article v-if="detail.verification.state === 'passed' && text(row(delivery.result).answer)" class="generated">
        <h4>中心生成内容 · 待复核</h4><p>{{ row(delivery.result).answer }}</p>
      </article>
      <div class="actions">
        <el-button :disabled="locked || !federation?.root_task_id || delivery.state === 'confirmed' || delivery.state === 'expired'" @click="read('fetch')">取回交付并校验</el-button>
        <el-button type="primary" :disabled="locked || detail.verification.state !== 'passed' || delivery.state !== 'pending'" @click="confirm">确认交付</el-button>
      </div>
      <p class="muted">确认前本机保留已校验的结果；确认可以重复，不会重复发布。</p>
    </article>
  </section>
</template>

<style scoped>
.plan-panel { max-width: 920px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 12px; }
h2 { font-size: 15px; font-weight: 600; margin: 0; }
h3 { font-size: 13px; font-weight: 600; margin: 28px 0 8px; }
h4 { font-size: 13px; font-weight: 600; margin: 0 0 6px; }
p { line-height: 1.7; overflow-wrap: anywhere; }
.muted { color: var(--ddp-ink-3); font-size: 12px; }
.error { color: var(--ddp-danger); font-size: 13px; }
.ddp-mono, .ddp-num { font-size: 12px; overflow-wrap: anywhere; }
.plan-form, .plan-list, .plan-review { padding: 20px 0; border-top: 1px solid var(--ddp-line); margin-top: 20px; }
.plan-form label, .plan-form legend { display: block; font-size: 13px; margin: 14px 0 6px; }
.plan-form select, .plan-form textarea, .plan-options select { box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid var(--ddp-line-2);
  border-radius: var(--ddp-r); background: var(--ddp-panel); color: inherit; font: inherit; box-shadow: none; }
.plan-form select:focus, .plan-form textarea:focus, .plan-options select:focus { border-color: var(--ddp-ink); outline: none; }
.plan-inputs { border: 0; padding: 0; margin: 14px 0 0; }
.plan-inputs .check { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.plan-inputs .check .ddp-num { margin-left: auto; color: var(--ddp-ink-3); }
.plan-options { display: flex; gap: 20px; flex-wrap: wrap; }
.plan-options label { flex: 1 1 200px; }
.heading, .actions, .status-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.heading { justify-content: space-between; }
.actions { margin-top: 16px; }
.plan-row { display: flex; align-items: center; gap: 16px; width: 100%; min-height: 44px; padding: 0 8px; border: 0;
  border-left: 2px solid transparent; border-bottom: 1px solid var(--ddp-line); background: transparent; color: inherit; font: inherit;
  text-align: left; cursor: pointer; flex-wrap: wrap; transition: background var(--ddp-dur) var(--ddp-ease); }
.plan-row[aria-current=true] { background: color-mix(in srgb, var(--ddp-ink) 7%, transparent); border-left-color: var(--ddp-ink); }
.plan-row:hover { background: var(--ddp-panel-2); }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 8px 0; font-size: 13px; }
.facts dt { color: var(--ddp-ink-3); }
.facts dd { margin: 0; min-width: 0; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--ddp-ink-3); padding: 8px 14px 8px 0; border-bottom: 1px solid var(--ddp-line); white-space: nowrap; }
td { padding: 8px 14px 8px 0; border-bottom: 1px solid var(--ddp-line); vertical-align: top; white-space: nowrap; }
/* Digests stay one token per line break opportunity; labels and counts never wrap mid-word. */
td.ddp-mono { white-space: normal; word-break: break-all; min-width: 12em; }
th.num, td.num { text-align: right; }
.quoted { font-size: 13px; white-space: pre-wrap; }
.pending-plan { font-size: 12px; }
.generated { margin-top: 12px; padding: 12px 0; border-top: 1px solid var(--ddp-line); }
.generated p { white-space: pre-wrap; }
</style>
