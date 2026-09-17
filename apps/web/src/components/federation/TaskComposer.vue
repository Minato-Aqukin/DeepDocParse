<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'

import { resourcesApi, type Resource } from '@/api/resources'
import { tasksApi } from '@/api/tasks'
import { PROBE_PAYLOAD_LABEL } from '@/constants/federation'
import {
  buildIntent,
  exhaustiveAllowed,
  remoteNodesOf,
  type ProbePayload,
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

const query = ref('')
const operation = ref<TaskOperation>('rag.answer.cited')
const scopeKind = ref<ScopeKind>('site_public')
const mode = ref<'fast' | 'exhaustive_scope'>('fast')
const resourceRefs = ref<string[]>([])
const payload = ref<ProbePayload[]>(['query_text'])
const maxProbeRequests = ref(8)
const maxEgressBytes = ref(1 << 20)
const scope = shallowRef<ScopeEnvelope | null>(null)
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

async function buildScope() {
  scoping.value = true
  error.value = ''
  try {
    const { data } = await tasksApi.createScope('corpus.retrieve')
    scope.value = data
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
    maxProbeRequests: maxProbeRequests.value, maxEgressBytes: maxEgressBytes.value,
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

onMounted(loadContext)
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
      </el-radio-group>
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
      </div>
    </fieldset>

    <fieldset class="field">
      <legend>检索方式</legend>
      <el-radio-group v-model="mode">
        <el-radio value="fast">快速（只查选中的候选，永远不声明查全）</el-radio>
        <el-radio value="exhaustive_scope" :disabled="!canExhaustive">范围穷查（需要已封存的范围清单）</el-radio>
      </el-radio-group>
    </fieldset>

    <fieldset v-if="remoteNodes.length" class="field egress">
      <legend>外发许可</legend>
      <p class="hint">
        勾中的内容会发给：{{ remoteNodes.join('、') }}。<strong>问题本身也是一种外发</strong> ——
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
    <p v-else class="hint">没有远端目标：本次不会向任何其他节点发出一个字节。</p>

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
