<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import PlanSummary from '@/components/federation/PlanSummary.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import {
  DELIVERY_STATE,
  LOCAL_DISPATCH_STATE,
  LOCAL_TRANSFER_STATE,
  LOCAL_VERIFY_STATE,
  PLANNING_STATE,
  metaOf,
  payloadKindLabel,
  retentionLabel,
} from '@/constants/federation'
import type { TaskPlan } from '@/federation/task-model'
import { getActiveSource, unwrap, workspaceError, type DesktopBridge, type Json, type PlanDetail } from '@/platform/desktop'

type Row = Record<string, Json>

const rowOf = (value: unknown): Row =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Row) : {}
const rowsOf = (value: unknown): Row[] => (Array.isArray(value) ? value.map(rowOf) : [])
const textOf = (value: unknown): string => (typeof value === 'string' ? value : '')
const countOf = (value: unknown): string =>
  typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
const clockOf = (value: unknown): string =>
  typeof value === 'number' ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'

/**
 * 桌面本机源的联邦任务详情（`/tasks/:planId` 在 local 源下的形态）。
 *
 * 旧工作台的审阅节搬入 AppShell：审阅（`PlanSummary` 复用 +
 * 实际外发内容表 + 接收方/传输 + 锁定输入 + 探索预算）、两阶段批准（经宿主原生
 * 对话框，`userConfirmed: true` + 当前 `scopeDigest`）、派发、「结果未确认 ·
 * 重新核对」（对账/回执）、取消/撤销/继续、交付取回 + 本地校验 + 仅校验通过才
 * 可确认、交付的回答/Wiki 打开。状态按契约轴摆；计划 id、范围摘要与幂等键只进
 * 「技术信息」折叠区。用词统一为「联邦任务」。
 */
const bridge = window.ddpDesktop as DesktopBridge | undefined
const route = useRoute()
const router = useRouter()

const planId = computed(() => String(route.params.planId ?? ''))
const sourceId = computed(() => getActiveSource()?.sourceId ?? '')
const detail = shallowRef<PlanDetail | null>(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const planKey = ref('')
const planAction = ref('')
const stopKey = ref('')
const stopAction = ref('')
let alive = true
let generation = 0

const plan = computed(() => rowOf(detail.value?.plan))
const scope = computed(() => rowOf(plan.value.scope))
const federation = computed(() => (detail.value?.federation ? rowOf(detail.value.federation) : null))
const delivery = computed(() => rowOf(federation.value?.delivery))
const consents = computed(() => rowOf(plan.value.consents))
const centerExecution = computed(() => rowOf(scope.value.center_execution))
const approvedPlanDigest = computed(() => textOf(rowOf(centerExecution.value.plan || scope.value.plan).plan_digest))
const centerPlanDigest = computed(() => textOf(federation.value?.center_plan_digest))
const planDiverged = computed(() => !!centerPlanDigest.value && centerPlanDigest.value !== approvedPlanDigest.value)
const filePlan = computed(() => rowOf(scope.value.task_spec).operation === 'corpus.parse')
const planPurpose = computed(() => textOf(rowOf(scope.value.task_spec).operation) === 'wiki.pages' ? 'wiki' : 'answer')
const planWiki = computed(() => rowOf(rowOf(rowOf(scope.value.task_spec).requirements).wiki))
const deliveryWiki = computed(() => rowOf(rowOf(delivery.value.result).wiki))
const fedState = computed(() => textOf(federation.value?.state))
const verified = computed(() => detail.value?.verification.state ?? 'unavailable')
const hasRemoteRecord = computed(() => !!federation.value?.root_task_id || !!federation.value?.remote_compute_id)
const revoked = computed(() => plan.value.revoked === true || plan.value.planning_state === 'invalidated')
const locked = computed(() => busy.value || !!planKey.value)
const needsCenterReview = computed(() => !filePlan.value && !!centerPlanDigest.value
  && (!centerExecution.value.plan || planDiverged.value))
const executable = computed(() => hasRemoteRecord.value && !planDiverged.value
  && (filePlan.value
    ? ['waiting_input', 'uploading', 'submit_unknown'].includes(fedState.value)
    : ['planned', 'submit_unknown'].includes(fedState.value)))

/**
 * 计划修订（步骤 / 数据边 / 中继 / 保留 / 预算）的展示只有一份实现：
 * `components/federation/PlanSummary.vue`（与 Web 协调者同一份只读展示）。
 * 这里只做形状收窄，不再画第二张表。
 */
const planRevision = computed<TaskPlan | null>(() => {
  const candidate = (centerExecution.value.plan || scope.value.plan) as unknown as TaskPlan | undefined
  if (!candidate || typeof candidate !== 'object') return null
  return typeof candidate.plan_digest === 'string' && Array.isArray(candidate.steps)
    && Array.isArray(candidate.data_edges) && !!candidate.budget && typeof candidate.budget === 'object'
    ? candidate : null
})
const PHASE_LABEL: Record<string, string> = { exploration: '探索', execution: '执行' }
/** 阶段与载荷类别：阶段是渲染侧展示词（两项，无契约枚举），载荷走契约 `payloadKindLabel`。未知取值原样显示，不留白。 */
const phaseLabel = (value: string): string => PHASE_LABEL[value] ?? value
/** 传输相位文案只走契约表（`local_transfer_state`）；未知相位显示原文，不断言。 */
const transferLabel = (value: string): string => metaOf(LOCAL_TRANSFER_STATE, value).label
// 结果可能已发出：保留幂等键，用回执对账，绝不自动重发。
const UNCERTAIN = new Set(['outcome_unknown', 'resume_unknown', 'host_operation_failed',
  'connection_failed', 'disposed', 'receipt_required', 'transfer_unknown'])

/**
 * 交付 Wiki 的打开（HostProxy 绑定）：propose 时的 centerConnectionId 不进计划，
 * 中心身份只在 `transport_bindings`（transport_ref='center'）里。同款六字段规则
 * 对 `sourceList()` 的登记源找交付中心 —— 找到就切到那个中心源（整页重载），
 * 重载后落在它的 `/wiki?wiki_id=…&revision_id=…` 阅读页；找到但切不过去、
 * 或根本找不到时，只展示引用与中心地址，由用户重新连接/切换后打开。
 * 交付的回答文本仍按本地取回的交付结果渲染（`delivery.result`），不经远端读。
 */
const deliveryCenter = shallowRef<{ sourceId: string; label: string } | null>(null)
const wikiError = ref('')

async function loadDeliveryWiki() {
  wikiError.value = ''
  deliveryCenter.value = null
  if (!bridge || verified.value !== 'passed' || !textOf(deliveryWiki.value.wiki_id)) return
  try {
    const bound = rowsOf(scope.value.transport_bindings).find((item) => item.transport_ref === 'center')
    if (!bound) {
      wikiError.value = '计划里没有已审阅的中心传输记录，无法定位交付 Wiki 的保存位置。'
      return
    }
    const host = bridge as unknown as {
      sourceList?: () => Promise<{ ok: boolean; value?: Record<string, unknown>[] }>
    }
    const listed = await host.sourceList?.()
    if (!listed?.ok) {
      wikiError.value = '数据源列表读取失败，无法定位交付 Wiki 的保存位置。'
      return
    }
    const envOf = (item: Record<string, unknown>) => rowOf(item.environment)
    const profileOf = (item: Record<string, unknown>) => rowOf(item.profile)
    const match = (listed.value ?? []).find((item) =>
      textOf(envOf(item).authorityNodeId) === textOf(bound.recipient_node_id)
      && textOf(envOf(item).environmentId) === textOf(bound.environment_id)
      && textOf(envOf(item).workspaceId) === textOf(bound.workspace_id)
      && textOf(profileOf(item).profileId) === textOf(bound.profile_id)
      && textOf(profileOf(item).issuer) === textOf(bound.issuer)
      && textOf(profileOf(item).subject) === textOf(bound.subject))
    if (!match || typeof match.sourceId !== 'string') {
      wikiError.value = '未找到与审阅记录完全一致的中心数据源；重新连接该中心后才能打开。'
      return
    }
    deliveryCenter.value = { sourceId: match.sourceId as string, label: textOf(match.label) }
  } catch (cause) {
    wikiError.value = workspaceError(cause)
  }
}

async function openDeliveryWiki() {
  if (!bridge || !deliveryCenter.value || verified.value !== 'passed') return
  wikiError.value = ''
  try {
    const host = bridge as unknown as {
      sourceActivate?: (input: { sourceId: string }) => Promise<{ ok: boolean; error?: { code?: string } }>
    }
    // 交付 Wiki 在交付中心源的 Wiki 页读：切源即整页重载，散列先指到目标修订。
    const target = `#/wiki?wiki_id=${encodeURIComponent(textOf(deliveryWiki.value.wiki_id))}`
      + (textOf(deliveryWiki.value.revision_id)
        ? `&revision_id=${encodeURIComponent(textOf(deliveryWiki.value.revision_id))}` : '')
    const result = await host.sourceActivate?.({ sourceId: deliveryCenter.value.sourceId })
    if (!result) {
      wikiError.value = '宿主暂未提供数据源接口'
      return
    }
    if (!result.ok) {
      wikiError.value = `切换到交付中心失败：${workspaceError(new Error(result.error?.code ?? ''))}，请在数据源页重试`
      return
    }
    location.hash = target
    location.reload()
  } catch (cause) {
    wikiError.value = workspaceError(cause)
  }
}

async function open(id: string) {
  if (!bridge || !sourceId.value || !id) return
  const mine = ++generation
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const value = unwrap(await bridge.clientPlanGet({ connectionId: sourceId.value, planId: id }))
    if (!alive || mine !== generation) return
    detail.value = value
    await loadDeliveryWiki()
  } catch (cause) {
    if (alive && mine === generation) error.value = workspaceError(cause)
  } finally {
    if (alive && mine === generation) busy.value = false
  }
}

/** 一次只做一笔带键写；键先落本地，不确定码保留键以便对账。 */
async function keyed(action: string, run: (key: string) => Promise<unknown>) {
  if (locked.value || !planId.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    planKey.value = crypto.randomUUID()
    planAction.value = action
    await run(planKey.value)
    planKey.value = ''
    planAction.value = ''
  } catch (cause) {
    const code = cause instanceof Error ? cause.message : ''
    if (!UNCERTAIN.has(code)) {
      planKey.value = ''
      planAction.value = ''
    }
    if (alive) error.value = workspaceError(cause)
  } finally {
    if (alive) busy.value = false
    // 失败文案优先：重读只在成功后刷新镜像，失败时不再用一次成功的 open
    // 把刚写进去的错误说明清空（旧面板的 keyed 在 finally 里无条件 list+open，
    // 失败提示只在测试里被下一次成功覆盖；这里失败不清错）。
    if (!error.value) await open(planId.value)
  }
}

const approve = (phase: 'exploration' | 'execution') => keyed(`approve-${phase}`, async (key) => {
  if (!bridge) throw new Error('connection_not_current')
  // 渲染器这一点击不是批准：宿主拿当前 scopeDigest 重读计划并弹原生对话框，
  // 只有在系统确认框里批准才生效（`userConfirmed: true` + 摘要对上）。
  unwrap(await bridge.clientPlanApprove({ connectionId: sourceId.value, planId: planId.value, phase,
    scopeDigest: textOf(plan.value.scope_digest), userConfirmed: true, idempotencyKey: key }))
})

const reviewCenter = () => keyed('review-center', async (key) => {
  if (!bridge) throw new Error('connection_not_current')
  const created = rowOf(unwrap(await bridge.clientPlanReviewCenter(
    { connectionId: sourceId.value, planId: planId.value, idempotencyKey: key })))
  const next = textOf(created.plan_id)
  if (next && next !== planId.value) {
    await router.push({ name: 'federation-task-local', params: { planId: next } })
  }
  notice.value = '已从中心保存的计划建立新的待批准修订；旧批准不会沿用。请重新审阅步骤、数据边与总预算。'
})

async function interrupt(action: 'revoke' | 'cancel') {
  if (!bridge || stopping.value || stopKey.value || !planId.value) return
  stopping.value = true
  error.value = ''
  try {
    stopKey.value = crypto.randomUUID()
    stopAction.value = action
    const input = { connectionId: sourceId.value, planId: planId.value, idempotencyKey: stopKey.value }
    unwrap(action === 'revoke' ? await bridge.clientPlanRevoke(input) : await bridge.clientPlanCancel(input))
    stopKey.value = ''
    stopAction.value = ''
  } catch (cause) {
    if (!UNCERTAIN.has(cause instanceof Error ? cause.message : '')) {
      stopKey.value = ''
      stopAction.value = ''
    }
    if (alive) error.value = workspaceError(cause)
  } finally {
    stopping.value = false
    await open(planId.value)
  }
}
const stopping = ref(false)

const dispatch = (phase: 'exploration' | 'execution') => keyed(`dispatch-${phase}`, async (key) => {
  if (!bridge) throw new Error('connection_not_current')
  const state = rowOf(unwrap(await bridge.clientPlanDispatch(
    { connectionId: sourceId.value, planId: planId.value, phase, idempotencyKey: key })))
  // 中心拒绝或结果未知会持久化并原样返回，不抛错：在这里说明，不自动重发。
  const failure = textOf(rowOf(state.error).code)
  if (failure) notice.value = `中心未确认此次派发：${workspaceError(new Error(failure))}（${failure}）`
})

const resume = () => keyed('resume', async (key) => {
  if (!bridge) throw new Error('connection_not_current')
  const state = rowOf(unwrap(await bridge.clientPlanResume(
    { connectionId: sourceId.value, planId: planId.value, idempotencyKey: key })))
  const failure = textOf(rowOf(state.error).code)
  notice.value = failure
    ? `中心未确认此次继续请求：${workspaceError(new Error(failure))}（${failure}）`
    : '中心已给出新的待审阅修订；旧批准不会沿用。请重新审阅步骤、数据边与总预算后再批准。'
})

const confirm = () => keyed('confirm', async (key) => {
  if (!bridge) throw new Error('connection_not_current')
  const state = rowOf(unwrap(await bridge.clientPlanConfirmDelivery({ connectionId: sourceId.value,
    planId: planId.value, deliveryId: textOf(delivery.value.id),
    resultManifestDigest: textOf(delivery.value.result_manifest_digest), idempotencyKey: key })))
  const failure = textOf(rowOf(state.error).code)
    || (rowOf(state.delivery).state !== 'confirmed' ? textOf(rowOf(state.delivery).reason) : '')
  if (failure) notice.value = `交付尚未确认：${workspaceError(new Error(failure))}（${failure}）。可以再次确认，不会重复发布。`
})

async function read(kind: 'reconcile' | 'fetch') {
  if (!bridge || locked.value || !planId.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  const mine = ++generation
  try {
    const input = { connectionId: sourceId.value, planId: planId.value }
    const value = unwrap(kind === 'reconcile'
      ? await bridge.clientPlanReconcile(input) : await bridge.clientPlanFetchDelivery(input))
    if (!alive || mine !== generation) return
    detail.value = value
    const failure = textOf(rowOf(rowOf(value.federation).error).code)
    if (failure) notice.value = `中心暂未给出结果：${workspaceError(new Error(failure))}（${failure}）`
  } catch (cause) {
    if (alive && mine === generation) error.value = workspaceError(cause)
  } finally {
    if (alive) busy.value = false
  }
}

async function receipt(kind: 'plan' | 'stop' = 'plan') {
  if (!bridge) return
  const key = kind === 'stop' ? stopKey.value : planKey.value
  if (!key) return
  try {
    const value = unwrap(await bridge.clientReceipt({ connectionId: sourceId.value, idempotencyKey: key }))
    if (!alive) return
    if (value === null) {
      error.value = '尚未找到这次操作的回执；保留操作编号，不会自动重复发送。'
      return
    }
    if (kind === 'plan') {
      const next = textOf(rowOf(value).plan_id)
      if (['propose', 'review-center'].includes(planAction.value) && next && next !== planId.value) {
        await router.push({ name: 'federation-task-local', params: { planId: next } })
        return
      }
      planKey.value = ''
      planAction.value = ''
    } else {
      stopKey.value = ''
      stopAction.value = ''
    }
    error.value = ''
    await open(planId.value)
  } catch (cause) {
    if (alive) error.value = workspaceError(cause)
  }
}

function back() {
  void router.push({ name: 'federation-tasks' })
}

onMounted(() => {
  void open(planId.value)
})
onBeforeUnmount(() => {
  alive = false
  generation++
})
</script>

<template>
  <section class="task-detail" aria-label="联邦任务详情">
    <header>
      <el-button link @click="back">← 联邦任务</el-button>
      <h1>联邦任务审阅</h1>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" class="ddp-degraded" aria-live="polite">{{ notice }}</p>
    <p v-if="planKey" class="ddp-degraded" role="status">有一项任务操作结果未知（{{ planAction }}）。
      <el-button text @click="receipt('plan')">查询回执</el-button></p>
    <p v-if="stopKey" class="ddp-degraded" role="status">停止操作待对账（{{ stopAction }}）。
      <el-button text @click="receipt('stop')">查询停止回执</el-button></p>
    <p v-if="!detail && !error" role="status" class="muted">正在读取本机任务…</p>

    <template v-if="detail">
      <div class="axes" aria-label="状态轴">
        <span class="axis"><span class="axis-label">规划</span>
          <StatusTag :meta="metaOf(PLANNING_STATE, textOf(plan.planning_state))" /></span>
        <span class="axis"><span class="axis-label">派发进展</span>
          <StatusTag v-if="federation" :meta="metaOf(LOCAL_DISPATCH_STATE, fedState)" />
          <span v-else class="muted">尚未派发</span></span>
        <span class="axis"><span class="axis-label">交付</span>
          <StatusTag v-if="textOf(delivery.state)" :meta="metaOf(DELIVERY_STATE, textOf(delivery.state))" />
          <span v-else class="muted">尚无交付</span></span>
        <span v-if="federation?.root_task_id" class="muted">中心任务已受理</span>
        <span v-if="federation" class="muted">最近对账
          <span class="ddp-num">{{ clockOf(rowOf(federation.reconcile).at) }}</span></span>
      </div>
      <p v-if="federation?.last_error" role="alert" class="error">
        {{ workspaceError(new Error(textOf(rowOf(federation.last_error).code))) }}
      </p>
      <p v-if="['explore_unknown', 'submit_unknown', 'resume_unknown'].includes(fedState)" class="ddp-degraded">
        中心可能已受理但回执丢失。请先对账；不会自动重发。</p>
      <p v-if="planDiverged" class="ddp-degraded is-danger" role="alert">
        中心规划的计划与已批准计划不同；执行已被拒绝，需要基于中心计划重新审阅。</p>

      <dl class="facts">
        <dt>保留策略</dt><dd>{{ retentionLabel(textOf(scope.retention)) }}</dd>
        <dt>输出位置</dt><dd class="ddp-mono">{{ (scope.output_locations as string[] | undefined)?.join('、') }}</dd>
        <dt>用途</dt><dd>{{ filePlan ? '解析固定 PDF（corpus.parse）'
          : planPurpose === 'wiki' ? '构建 Wiki 草稿（wiki.pages）' : '生成回答（rag.answer.cited）' }}</dd>
        <template v-if="planPurpose === 'wiki'">
          <dt>Wiki 标题</dt><dd>{{ textOf(planWiki.title) || '—' }}</dd>
          <dt>页数上限</dt><dd class="ddp-num">{{ planWiki.max_pages ?? '—' }}</dd>
        </template>
        <template v-if="centerExecution.parent_plan_id">
          <dt>父计划</dt><dd>见「技术信息」</dd>
        </template>
      </dl>

      <h2>实际外发内容</h2>
      <div class="scroll"><table>
        <thead><tr><th>阶段</th><th>内容</th><th>接收方</th><th class="num">字节</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="item in rowsOf(scope.payload_bindings)" :key="textOf(item.payload_id)">
          <td>{{ phaseLabel(textOf(item.phase)) }}</td>
          <td>{{ payloadKindLabel(textOf(item.payload_kind)) }}</td>
          <td class="ddp-mono">{{ item.recipient_node_id }}</td>
          <td class="ddp-num num">{{ countOf(item.size_bytes) }}</td>
          <td class="ddp-mono">{{ item.digest }}</td>
        </tr></tbody>
      </table></div>
      <p class="quoted">{{ filePlan ? '文件任务描述' : '问题原文' }}：{{ rowOf(scope.task_spec).query }}</p>

      <h2>接收方与传输</h2>
      <dl v-for="item in rowsOf(scope.transport_bindings)" :key="textOf(item.transport_ref)" class="facts">
        <dt>节点身份</dt><dd class="ddp-mono">{{ item.recipient_node_id }}</dd>
        <dt>地址</dt><dd class="ddp-mono">{{ item.endpoint }}</dd>
        <dt>中心工作区</dt><dd class="ddp-mono">{{ item.workspace_id }}</dd>
        <dt>用户</dt><dd class="ddp-mono">{{ item.subject }}</dd>
      </dl>

      <h2>计划修订 · 谁执行、数据流向哪里</h2>
      <PlanSummary v-if="planRevision" :plan="planRevision" />
      <p v-else class="ddp-degraded is-danger" role="alert">本机镜像里的计划结构不完整，步骤与数据边无法展示。不要据此批准。</p>

      <h2>{{ filePlan ? '锁定的本地原件 · 执行时外发' : '锁定的本地输入 · 不外发' }}</h2>
      <div class="scroll"><table>
        <thead><tr><th>版本</th><th class="num">字节</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="item in rowsOf(scope.input_manifest)" :key="textOf(item.ref)">
          <td class="ddp-mono">{{ item.ref }}</td>
          <td class="ddp-num num">{{ countOf(item.size_bytes) }}</td>
          <td class="ddp-mono">{{ item.digest }}</td>
        </tr></tbody>
      </table></div>
      <p v-if="!rowsOf(scope.input_manifest).length" class="muted">未锁定本地输入。</p>

      <h2>探索阶段预算</h2>
      <dl class="facts">
        <dt>允许接收方</dt><dd class="ddp-mono">
          {{ (rowOf(scope.exploration).allowed_recipients as string[] | undefined)?.join('、') || '未声明' }}</dd>
        <dt>允许载荷</dt><dd>
          {{ (rowOf(scope.exploration).allowed_payload as string[] | undefined)?.map(payloadKindLabel).join('、') || '无' }}</dd>
        <dt>探索请求上限</dt><dd class="ddp-num">{{ countOf(rowOf(rowOf(scope.exploration).budget).max_probe_requests) }}</dd>
        <dt>探索字节上限</dt><dd class="ddp-num">{{ countOf(rowOf(rowOf(scope.exploration).budget).max_egress_bytes) }}</dd>
      </dl>

      <div class="actions">
        <el-button v-if="!centerExecution.plan" type="primary"
          :disabled="locked || revoked || !planRevision || !!consents.exploration"
          @click="approve('exploration')">批准探索…</el-button>
        <el-button type="primary"
          :disabled="locked || revoked || !planRevision || !!consents.execution
            || (filePlan ? !consents.exploration : (!centerExecution.plan || planDiverged))"
          @click="approve('execution')">批准执行…</el-button>
      </div>
      <p class="muted">批准会弹出系统确认框，再次列出上面的接收方与外发内容；只有在系统确认框里批准才生效。</p>
      <el-button v-if="needsCenterReview" :disabled="locked || revoked || !hasRemoteRecord" @click="reviewCenter">
        载入中心计划重新审阅</el-button>

      <h2>派发与对账</h2>
      <p v-if="detail.transfer" aria-live="polite" class="muted">
        对象存储已确认 {{ countOf(detail.transfer.uploadedBytes) }} / {{ countOf(detail.transfer.totalBytes) }} 字节 ·
        {{ transferLabel(detail.transfer.state) }}。上传不是解析完成。</p>
      <div class="actions">
        <el-button v-if="!centerExecution.plan"
          :disabled="locked || revoked || !consents.exploration || (!!federation && fedState !== 'explore_unknown')"
          @click="dispatch('exploration')">派发探索</el-button>
        <el-button :disabled="locked || revoked || !consents.execution || !executable"
          @click="dispatch('execution')">{{ filePlan ? '派发 / 对账并继续上传' : '派发执行' }}</el-button>
        <el-button :disabled="locked || !hasRemoteRecord" @click="read('reconcile')">结果未确认 · 重新核对</el-button>
        <el-button v-if="!filePlan"
          :disabled="locked || revoked || !hasRemoteRecord || !['succeeded', 'failed', 'delivered'].includes(fedState)"
          @click="resume">请求继续</el-button>
        <el-button v-if="filePlan"
          :disabled="stopping || !!stopKey || !hasRemoteRecord
            || ['cancelled', 'expired', 'acked', 'delivered'].includes(fedState)"
          @click="interrupt('cancel')">取消中心计算</el-button>
      </div>
      <p class="muted">继续请求会在剩余根预算内推进；若需要扩大执行图，必须重新审阅并批准新修订。对账始终只读，不会重发执行请求。</p>

      <h2>交付</h2>
      <dl v-if="detail.verification.expected || verified !== 'unavailable'" class="facts">
        <dt>本地校验</dt><dd><StatusTag :meta="metaOf(LOCAL_VERIFY_STATE, verified)" /></dd>
        <dt>中心声明摘要</dt><dd class="ddp-mono">{{ detail.verification.expected }}</dd>
        <dt>本地重算摘要</dt><dd class="ddp-mono">{{ detail.verification.actual || '—' }}</dd>
      </dl>
      <p v-if="delivery.reason" role="alert" class="error">
        {{ workspaceError(new Error(textOf(delivery.reason))) }}</p>
      <article v-if="verified === 'passed' && textOf(rowOf(delivery.result).answer)" class="generated">
        <h3>中心生成内容 · 待复核</h3>
        <p>{{ rowOf(delivery.result).answer }}</p>
      </article>
      <template v-if="planPurpose === 'wiki'">
        <article v-if="verified === 'passed' && textOf(deliveryWiki.wiki_id)" class="generated">
          <h3>中心 Wiki 交付 · 待复核</h3>
          <dl class="facts">
            <dt>覆盖账本</dt><dd class="ddp-mono">
              {{ textOf(rowOf(delivery.result).coverage_ref) || textOf(federation?.root_task_id) || '—' }}</dd>
            <dt>语义复核</dt><dd>needs_review（中心自报的 passed 不是人工审阅）</dd>
            <dt>Wiki 引用</dt><dd class="ddp-mono">{{ textOf(deliveryWiki.wiki_id) }} · {{ textOf(deliveryWiki.revision_id) }}</dd>
          </dl>
          <p v-if="wikiError" role="alert" class="error">{{ wikiError }}</p>
          <div class="actions">
            <el-button :disabled="!deliveryCenter" @click="openDeliveryWiki">
              在交付中心打开固定修订与原始出处</el-button>
          </div>
          <p v-if="deliveryCenter" class="muted">切换到{{ deliveryCenter.label }}并打开它的 Wiki 阅读页（当前页会重载）。</p>
        </article>
        <p v-else-if="verified === 'passed'" class="ddp-degraded">
          中心尚未给出可用的 Wiki 引用（wiki_id/revision_id）：产物尚未落库或不可见，不记为成功。请对账后重试取回。</p>
      </template>
      <div class="actions">
        <el-button :disabled="locked || !hasRemoteRecord || textOf(delivery.state) === 'confirmed'
          || textOf(delivery.state) === 'expired'" @click="read('fetch')">取回交付并校验</el-button>
        <el-button type="primary" :disabled="locked || verified !== 'passed' || textOf(delivery.state) !== 'pending'"
          @click="confirm">确认交付</el-button>
      </div>
      <p class="muted">确认前本机保留已校验的结果；确认可以重复，不会重复发布。</p>

      <section class="danger" aria-label="危险操作">
        <el-divider />
        <h2>撤销批准</h2>
        <p class="muted">撤销批准会停止后续派发；已发出的内容不会被收回。撤销后需要准备并批准新计划才能继续。</p>
        <div class="actions">
          <el-button type="danger" plain :disabled="locked || revoked || stopping || !!stopKey"
            :loading="stopping" @click="interrupt('revoke')">撤销批准并停止传输</el-button>
        </div>
      </section>

      <details class="tech" aria-label="技术信息">
        <summary>技术信息</summary>
        <dl class="facts">
          <dt>任务</dt><dd class="ddp-mono">{{ textOf(plan.plan_id) }}</dd>
          <dt>范围摘要</dt><dd class="ddp-mono">{{ textOf(plan.scope_digest) }}</dd>
          <template v-if="centerExecution.parent_plan_id">
            <dt>父计划</dt><dd class="ddp-mono">{{ centerExecution.parent_plan_id }}</dd>
            <dt>中心计划摘要</dt><dd class="ddp-mono">{{ centerPlanDigest }}</dd>
          </template>
          <dt>中心任务</dt><dd class="ddp-mono">{{ textOf(federation?.root_task_id) || '—' }}</dd>
          <dt>交付</dt><dd class="ddp-mono">{{ textOf(delivery.id) || '—' }}</dd>
          <dt>交付声明摘要</dt><dd class="ddp-mono">{{ textOf(delivery.result_manifest_digest) || '—' }}</dd>
          <dt>待对账操作</dt><dd class="ddp-mono">{{ planKey || stopKey || '—' }}</dd>
        </dl>
      </details>
    </template>
  </section>
</template>

<style scoped>
.task-detail { max-width: 1120px; margin: auto; display: grid; gap: 12px; }
header { display: grid; gap: 6px; justify-items: start; }
h1 { font-size: 27px; font-weight: 600; margin: 0; }
h2 { font-size: 18px; font-weight: 600; margin: 20px 0 0; }
h3 { font-size: 15px; font-weight: 600; margin: 0 0 6px; }
.axes { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; }
.axis { display: inline-flex; align-items: center; gap: 8px; }
.axis-label { color: var(--ddp-ink-3); font-size: 12.5px; }
.meta, .muted { color: var(--ddp-ink-3); font-size: 13px; margin: 0; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 8px 0; font-size: 13px; }
.facts dt { color: var(--ddp-ink-3); }
.facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--ddp-ink-3); padding: 8px 14px 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); white-space: nowrap; }
td { padding: 8px 14px 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); vertical-align: top; }
td.ddp-mono { word-break: break-all; }
th.num, td.num { text-align: right; }
.quoted { font-size: 13px; white-space: pre-wrap; }
.actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.generated { margin-top: 12px; padding: 12px 0; border-top: 1px solid var(--el-border-color-lighter); }
.generated p { white-space: pre-wrap; }
.danger { margin-top: 20px; padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter); }
.tech { margin-top: 20px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.tech summary { cursor: pointer; font-size: 13px; color: var(--ddp-ink-3); }
</style>
