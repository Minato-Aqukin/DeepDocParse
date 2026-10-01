<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { resourcesApi, type Resource } from '@/api/resources'
import { tasksApi } from '@/api/tasks'
import ScopeTargets from '@/components/federation/ScopeTargets.vue'
import { usePolling } from '@/composables/usePolling'
import { CAPABILITY_READINESS, PROBE_PAYLOAD_LABEL, metaOf } from '@/constants/federation'
import {
  buildIntent,
  exhaustiveAllowed,
  generationRecipients,
  generationObservationCurrent,
  GENERATION_CLOCK_SKEW_MS,
  generationRecipientReady,
  remoteNodesOf,
  type ProbePayload,
  type GenerationCandidate,
  type ScopeEnvelope,
  type ScopeKind,
  type TaskDraft,
  type TaskOperation,
} from '@/federation/task-model'

/**
 * 发起一个联邦任务。**两层许可里的第一层在这里签**：远端 Probe 之前必须先有外发许可，
 * 而"问题本身也是一种外发" —— 所以问题原文是一个要用户自己勾的选项，界面不替他补上。
 * 没有远端接收方时一律 `local_only`，载荷、接收方、远端预算全空（契约 allOf 钉着）。
 *
 * 这一步只创建需求并请求计划；**要不要执行是下一步的事**（详情页里批准计划）。
 */
const emit = defineEmits<{ (e: 'created', rootTaskId: string): void }>()
const props = withDefaults(defineProps<{ initialQuery?: string }>(), { initialQuery: '' })

const query = ref(props.initialQuery)
watch(() => props.initialQuery, (q) => {
  if (q) query.value = q
})
const operation = ref<TaskOperation>('rag.answer.cited')
const scopeKind = ref<ScopeKind>('site_public')
const wikiTitle = ref('')
const wikiMaxPages = ref(4)
const wikiId = ref('')
const wikiBaseRevision = ref('')
const mode = ref<'fast' | 'exhaustive_scope'>('fast')
const resourceRefs = ref<string[]>([])
const payload = ref<ProbePayload[]>(['query_text'])
const maxProbeRequests = ref(8)
const maxEgressBytes = ref(1 << 20)
const scope = shallowRef<ScopeEnvelope | null>(null)
const recipients = ref<string[]>([])
const candidates = shallowRef<GenerationCandidate[]>([])
const discovering = ref(false)
const discovered = ref(false)
const discoveryError = ref('')
const clock = ref(Date.now())
const clockPolling = usePolling(() => { clock.value = Date.now() }, () => true, 1000)
let discoveryRequest = 0
const resources = shallowRef<Resource[]>([])
/** 签许可要用的三样，全部来自已认证的握手 —— 拿不到就不让提交，不拿空串去凑。 */
const localNodeId = ref('')
const workspaceRef = ref('')
const grantedBy = ref('')
const busy = ref(false)
const scoping = ref(false)
const error = ref('')
/** 同一次提交重试要复用同一个幂等键：丢响应后重点一次不能变成第二个任务。 */
let attemptKey = ''

const remoteNodes = computed(() => (scopeKind.value === 'federation_public'
  ? remoteNodesOf(scope.value ?? undefined, localNodeId.value) : []))
const canExhaustive = computed(() => exhaustiveAllowed({
  scopeKind: scopeKind.value, scope: scope.value ?? undefined,
}))
const generationNodes = computed(() => generationRecipients(
  candidates.value, scopeKind.value === 'federation_public' ? scope.value ?? undefined : undefined,
  operation.value, localNodeId.value, clock.value,
))
const computeNodes = computed(() => generationNodes.value.filter((item) => !remoteNodes.value.includes(item.node_id)))
const possibleRecipients = computed(() => [...remoteNodes.value, ...computeNodes.value.map((item) => item.node_id)])

watch(operation, () => {
  // 切换用途不会把上一种能力观测当作本次许可；数据接收方的选择保留。
  recipients.value = recipients.value.filter((node) => remoteNodes.value.includes(node))
  candidates.value = []
  void loadGenerationCandidates()
})
watch(scopeKind, () => {
  recipients.value = []
  candidates.value = []
  scope.value = null
  mode.value = 'fast'
  discovered.value = false
  discovering.value = false
  discoveryError.value = ''
  discoveryRequest++
})

const PAYLOAD_CHOICES = Object.entries(PROBE_PAYLOAD_LABEL) as [ProbePayload, string][]

function problem(cause: unknown, fallback: string): string {
  const detail = (cause as { response?: { data?: { error?: { code?: string; message?: string } } } })
    ?.response?.data?.error
  return detail?.code ? `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
    : `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

async function loadContext() {
  try {
    const [who, mine] = await Promise.all([tasksApi.identity(), resourcesApi.list('mine', 0)])
    localNodeId.value = who.data.identity.authority_node_id
    workspaceRef.value = who.data.identity.workspace_id
    grantedBy.value = who.data.profile.subject
    resources.value = mine.data.items
  } catch (cause) {
    error.value = problem(cause, '读取本节点身份或资源失败')
  }
}

async function loadGenerationCandidates() {
  const current = ++discoveryRequest
  const purpose = operation.value
  candidates.value = []
  discovered.value = false
  discoveryError.value = ''
  discovering.value = false
  if (purpose === 'corpus.retrieve' || scopeKind.value !== 'federation_public' || !scope.value) return
  discovering.value = true
  try {
    const { data } = await tasksApi.generationCandidates(purpose)
    if (current !== discoveryRequest) return
    candidates.value = data.items
    discovered.value = true
    clock.value = Date.now()
    const ready = generationNodes.value.filter((item) => generationRecipientReady(item, clock.value)).map((item) => item.node_id)
    recipients.value = recipients.value.filter((node) => remoteNodes.value.includes(node) || ready.includes(node))
  } catch (cause) {
    if (current === discoveryRequest) {
      recipients.value = recipients.value.filter((node) => remoteNodes.value.includes(node))
      discoveryError.value = problem(cause, '生成能力目录读取失败（仅算力节点不可选，相关选择已清除）')
    }
  } finally {
    if (current === discoveryRequest) discovering.value = false
  }
}

function readiness(candidate: GenerationCandidate) {
  if (!generationObservationCurrent(candidate, clock.value)) {
    return candidate.observed_at && Date.parse(candidate.observed_at) > clock.value + GENERATION_CLOCK_SKEW_MS
      ? '未知（观测时间超前，无法确认就绪）' : '未知（观测已过期或缺失）'
  }
  const label = metaOf(CAPABILITY_READINESS, candidate.readiness).label
  return candidate.accepting_admissions ? label : `${label} · 不接单`
}

async function buildScope() {
  scoping.value = true
  error.value = ''
  try {
    const { data } = await tasksApi.createScope('corpus.retrieve')
    scope.value = data
    recipients.value = []
    await loadGenerationCandidates()
    // 枚举没封存就没有可信分母，穷查在协调者那边也会被拒 —— 当场降回快速检索。
    if (data.effective_enumeration_state !== 'sealed') mode.value = 'fast'
  } catch (cause) {
    error.value = problem(cause, '生成范围清单失败')
  } finally {
    scoping.value = false
  }
}

function draft(): TaskDraft {
  return {
    query: query.value, operation: operation.value, scopeKind: scopeKind.value,
    scope: scope.value ?? undefined, resourceRefs: resourceRefs.value, mode: mode.value,
    payload: payload.value,
    recipients: recipients.value, generationCandidates: candidates.value,
    maxProbeRequests: maxProbeRequests.value, maxEgressBytes: maxEgressBytes.value,
    wiki: operation.value === 'wiki.pages' ? {
      ...(wikiId.value.trim() ? { wiki_id: wikiId.value.trim() } : {}),
      ...(wikiBaseRevision.value.trim() ? { base_revision_id: wikiBaseRevision.value.trim() } : {}),
      ...(wikiTitle.value.trim() ? { title: wikiTitle.value.trim() } : {}),
      max_pages: wikiMaxPages.value,
    } : undefined,
  }
}

async function submit() {
  error.value = ''
  let body
  try {
    attemptKey ||= `intent-${crypto.randomUUID()}`
    body = buildIntent(draft(), {
      localNodeId: localNodeId.value,
      grantedBy: grantedBy.value,
      workspaceRef: workspaceRef.value,
      nonce: attemptKey.slice(7, 19), now: Date.now(),
    })
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    return
  }
  busy.value = true
  try {
    const intent = await tasksApi.createIntent(body, attemptKey)
    const rootTaskId = intent.data.root_task_id
    // 规划失败不回滚意图：任务已经建好，详情页里能看到规划状态与失败原因并重试。
    try { await tasksApi.plan(rootTaskId) } catch { /* 详情页会显示规划状态与原因 */ }
    attemptKey = ''
    emit('created', rootTaskId)
  } catch (cause) {
    error.value = problem(cause, '创建任务失败')
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void loadContext()
  clockPolling.start()
})
onBeforeUnmount(() => { discoveryRequest++ })
</script>

<template>
  <form class="composer" aria-label="新建联邦任务" @submit.prevent="submit">
    <label class="field">
      <span>问题</span>
      <el-input v-model="query" type="textarea" :rows="3" placeholder="要在哪些资料里查什么？" />
    </label>

    <fieldset class="field">
      <legend>要什么</legend>
      <el-radio-group v-model="operation">
        <el-radio value="rag.answer.cited">带出处的回答</el-radio>
        <el-radio value="corpus.retrieve">只取证据</el-radio>
        <el-radio value="wiki.pages">构建 Wiki 草稿</el-radio>
      </el-radio-group>
      <div v-if="operation === 'wiki.pages'" class="scope">
        <label class="field"><span>Wiki 标题（新建与更新都必填，requirements.wiki.title）</span>
          <el-input v-model="wikiTitle" placeholder="用固定证据解释什么？" maxlength="255" required />
        </label>
        <label class="field"><span>页数上限（1–12，requirements.wiki.max_pages）</span>
          <el-input-number v-model="wikiMaxPages" :min="1" :max="12" />
        </label>
        <label class="field"><span>更新已有 Wiki（可选，requirements.wiki.wiki_id）</span>
          <el-input v-model="wikiId" placeholder="留空=新建 Wiki" />
        </label>
        <label class="field"><span>基于修订（更新时必填，requirements.wiki.base_revision_id）</span>
          <el-input v-model="wikiBaseRevision" placeholder="留空=新建 Wiki" />
        </label>
      </div>
    </fieldset>

    <fieldset class="field">
      <legend>查哪些资料</legend>
      <el-radio-group v-model="scopeKind">
        <el-radio value="site_public">本站公开</el-radio>
        <el-radio value="fixed_resources">指定资源</el-radio>
        <el-radio value="federation_public">联邦公开范围</el-radio>
      </el-radio-group>
      <el-select v-if="scopeKind === 'fixed_resources'" v-model="resourceRefs" multiple
        placeholder="选择资源" class="resources">
        <el-option v-for="item in resources" :key="item.id" :value="item.id" :label="item.display_name" />
      </el-select>
      <div v-if="scopeKind === 'federation_public'" class="scope">
        <el-button :loading="scoping" @click="buildScope">生成范围清单</el-button>
        <p v-if="scope" class="hint">
          清单 <span class="ddp-mono">{{ scope.manifest.scope_id }}</span> ·
          目标 <span class="ddp-num">{{ scope.total_targets }}</span> ·
          枚举 {{ scope.effective_enumeration_state === 'sealed' ? '已封存' : '不完整（只能快速检索）' }} ·
          远端节点 {{ remoteNodes.length ? remoteNodes.join('、') : '无' }}
        </p>
        <p v-else class="hint">穷查要先把范围枚举并封存下来，否则“查全了”没有分母。</p>
        <ScopeTargets v-if="scope" :scope-id="scope.manifest.scope_id" />
      </div>
      <div v-if="scopeKind === 'federation_public' && scope" class="scope">
        <p class="hint">接收方要逐项勾选；范围清单不是外发许可。仅算力节点不提供资料，只能接收固定证据用于生成。实际生成位置与每条数据边会在执行前的计划里再次确认。</p>
        <div v-if="remoteNodes.length" class="scope">
          <span>资料节点</span>
          <el-checkbox-group v-model="recipients">
            <el-checkbox v-for="node in remoteNodes" :key="node" :value="node">{{ node }}</el-checkbox>
          </el-checkbox-group>
        </div>
        <div v-if="operation !== 'corpus.retrieve'" class="scope">
          <span>仅算力节点</span>
          <el-button :loading="discovering" @click="loadGenerationCandidates">刷新生成能力</el-button>
          <el-checkbox-group v-model="recipients">
            <el-checkbox v-for="item in computeNodes" :key="item.node_id" :value="item.node_id"
              :disabled="!generationRecipientReady(item, clock)">
              {{ item.node_id }} · {{ readiness(item) }}
            </el-checkbox>
          </el-checkbox-group>
          <p v-if="discovered && !computeNodes.length && !discovering" class="hint">本次范围内没有描述新鲜的已批准仅算力候选；不会自动加入其他节点。</p>
          <p v-if="discoveryError" class="error" role="alert">{{ discoveryError }}</p>
        </div>
      </div>
    </fieldset>

    <fieldset class="field">
      <legend>检索方式</legend>
      <el-radio-group v-model="mode">
        <el-radio value="fast">快速（只查选中的候选，永远不声明查全）</el-radio>
        <el-radio value="exhaustive_scope" :disabled="!canExhaustive">范围穷查（需要已封存的范围清单）</el-radio>
      </el-radio-group>
    </fieldset>

    <fieldset v-if="possibleRecipients.length" class="field egress">
      <legend>外发许可</legend>
      <p class="hint">
        勾中的内容只允许发给已选接收方：{{ recipients.length ? recipients.join('、') : '无（尚未选择）' }}。<strong>问题本身也是一种外发</strong> ——
        不勾就不发，对应的远端目标会如实记成“被拒绝”，而不是假装查过。
      </p>
      <el-checkbox-group v-model="payload">
        <el-checkbox v-for="[value, label] in PAYLOAD_CHOICES" :key="value" :value="value">{{ label }}</el-checkbox>
      </el-checkbox-group>
      <div class="budget">
        <label>探测次数上限 <el-input-number v-model="maxProbeRequests" :min="0" :max="64" /></label>
        <label>外发字节上限 <el-input-number v-model="maxEgressBytes" :min="0" :step="65536" /></label>
      </div>
    </fieldset>
    <p v-else class="hint">没有已选远端接收方：本次不会向其他节点发送任务内容。</p>

    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="actions">
      <el-button type="primary" native-type="submit" :loading="busy">创建任务并规划</el-button>
    </div>
  </form>
</template>

<style scoped>
.composer { display: grid; gap: 18px; max-width: 720px; }
.field { display: grid; gap: 8px; border: 0; padding: 0; margin: 0; }
legend, .field > span { color: var(--ddp-ink-2); font-size: 13px; padding: 0; }
.hint { margin: 0; color: var(--ddp-ink-3); font-size: 12.5px; line-height: 1.7; }
.hint strong { color: var(--ddp-ink-2); font-weight: 600; }
.resources { max-width: 480px; }
.scope { display: grid; gap: 6px; }
.budget { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; font-size: 13px; color: var(--ddp-ink-2); }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.actions { display: flex; justify-content: flex-end; }
</style>
