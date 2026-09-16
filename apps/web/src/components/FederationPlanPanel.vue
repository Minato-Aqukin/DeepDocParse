<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import StatusTag from '@/components/common/StatusTag.vue'
import type { TagType } from '@/constants/status'
import { unwrap, workspaceError, type ConnectionSummary, type DesktopBridge, type Json, type PlanDetail } from '@/platform/desktop'
import { DraftWriter } from '@/platform/draft-writer'

type Row = Record<string, Json>
type Tag = { label: string; type: TagType; active: boolean }
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

const PLANNING: Record<string, Tag> = {
  ready: { label: '待批准', type: 'info', active: true }, exploring: { label: '已批准探索', type: 'info', active: true },
  approved: { label: '已批准执行', type: 'success', active: false }, invalidated: { label: '已撤销或过期', type: 'primary', active: false },
}
const FEDERATION: Record<string, Tag> = {
  prepared: { label: '尚未派发', type: 'primary', active: false }, exploring: { label: '中心规划中', type: 'info', active: true },
  planned: { label: '中心计划已就绪', type: 'info', active: true }, explore_unknown: { label: '探索结果未知 · 需对账', type: 'warning', active: true },
  submitted: { label: '中心执行中', type: 'info', active: true }, submit_unknown: { label: '提交结果未知 · 需对账', type: 'warning', active: true },
  approved: { label: '中心已批准', type: 'info', active: true }, succeeded: { label: '中心已完成', type: 'success', active: false },
  failed: { label: '失败', type: 'danger', active: false }, cancelled: { label: '已取消', type: 'primary', active: false },
  delivered: { label: '已交付', type: 'success', active: false },
}
const DELIVERY: Record<string, Tag> = {
  not_requested: { label: '未请求交付', type: 'primary', active: false }, pending: { label: '待取回确认', type: 'info', active: true },
  transferring: { label: '传输中', type: 'info', active: true }, confirmed: { label: '已确认交付', type: 'success', active: false },
  expired: { label: '交付已过期', type: 'danger', active: false },
}
const VERIFY: Record<string, Tag> = {
  passed: { label: '本地重算摘要一致', type: 'success', active: false }, failed: { label: '本地重算摘要不一致', type: 'danger', active: false },
  unavailable: { label: '尚无可校验的本地结果', type: 'primary', active: false },
}
const tag = (table: Record<string, Tag>, value: string): Tag => table[value] ?? { label: value || '未知', type: 'warning', active: false }
const PAYLOAD: Record<string, string> = { query_text: '检索词原文', source_files: '原始文件', evidence_excerpts: '证据片段' }
const PHASE: Record<string, string> = { exploration: '探索', execution: '执行' }
const RETENTION: Record<string, string> = { temporary: '临时（任务结束即可清理）', task_pinned: '任务期间保留', persistent: '长期保留' }
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
        <StatusTag v-bind="tag(PLANNING, text(item.planning_state))" />
        <StatusTag v-if="item.federation" v-bind="tag(FEDERATION, text(row(item.federation).state))" />
        <StatusTag v-if="row(item.federation).delivery_state" v-bind="tag(DELIVERY, text(row(item.federation).delivery_state))" />
      </button>
      <p v-if="!rows(listing.items).length" class="muted">尚无计划。</p>
    </section>

    <article v-if="detail" class="plan-review" aria-label="计划审阅">
      <div class="heading"><h2>审阅 <code class="ddp-mono">{{ plan.plan_id }}</code></h2><StatusTag v-bind="tag(PLANNING, text(plan.planning_state))" /></div>
      <dl class="facts">
        <dt>范围摘要</dt><dd class="ddp-mono">{{ plan.scope_digest }}</dd>
        <dt>有效期至</dt><dd class="ddp-mono">{{ row(scope.plan).valid_until }}</dd>
        <dt>保留策略</dt><dd>{{ RETENTION[text(scope.retention)] || scope.retention }}</dd>
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

      <h3>数据边</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>编号</th><th>来源 → 去向</th><th>内容</th><th>保留</th></tr></thead>
        <tbody><tr v-for="edge in rows(row(scope.plan).data_edges)" :key="text(edge.edge_id)">
          <td class="ddp-mono">{{ edge.edge_id }}</td><td class="ddp-mono">{{ edge.from_node_id }} → {{ edge.to_node_id }}</td>
          <td>{{ PAYLOAD[text(edge.payload_kind)] || edge.payload_kind }}</td><td>{{ RETENTION[text(edge.retention)] || edge.retention }}</td>
        </tr></tbody>
      </table></div>

      <h3>锁定的本地输入 · 不外发</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>版本</th><th class="num">字节</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="item in rows(scope.input_manifest)" :key="text(item.ref)"><td class="ddp-mono">{{ item.ref }}</td>
          <td class="ddp-num num">{{ count(item.size_bytes) }}</td><td class="ddp-mono">{{ item.digest }}</td></tr></tbody>
      </table></div>
      <p v-if="!rows(scope.input_manifest).length" class="muted">未锁定本地输入。</p>

      <h3>预算</h3>
      <dl class="facts budget">
        <dt>请求上限</dt><dd class="ddp-num">{{ count(row(row(scope.plan).budget).max_requests) }}</dd>
        <dt>外发字节上限</dt><dd class="ddp-num">{{ count(row(row(scope.plan).budget).max_bytes) }}</dd>
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
      <p class="status-line"><StatusTag v-if="federation" v-bind="tag(FEDERATION, fedState)" /><span v-else class="muted">尚未派发</span>
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
      <p class="status-line"><StatusTag v-if="delivery.state" v-bind="tag(DELIVERY, text(delivery.state))" /><span v-else class="muted">尚无交付</span>
        <span v-if="delivery.id" class="muted">交付 <code class="ddp-mono">{{ delivery.id }}</code></span></p>
      <p v-if="delivery.reason" role="alert" class="error">{{ workspaceError(new Error(text(delivery.reason))) }} <code class="ddp-mono">{{ delivery.reason }}</code></p>
      <dl v-if="detail.verification.expected || detail.verification.state !== 'unavailable'" class="facts">
        <dt>本地校验</dt><dd><StatusTag v-bind="tag(VERIFY, detail.verification.state)" /></dd>
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
