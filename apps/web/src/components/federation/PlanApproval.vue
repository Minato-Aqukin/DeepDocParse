<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { tasksApi } from '@/api/tasks'
import { payloadKindLabel, retentionLabel } from '@/constants/federation'
import { buildExecutionConsent, recipientsOf, retentionOf, type TaskPlan } from '@/federation/task-model'

/**
 * 计划审阅与批准 —— 两层许可里的第二层。
 *
 * 界面上把**这一份修订实际会发出去的东西**摆全：接收方（含中继）、外发内容、
 * 保留策略与有效期。许可对象完全由计划推出（`recipientsOf` / `retentionOf`），
 * 用户批的就是看到的这一份；计划一变摘要就对不上，这份许可自动失效。
 *
 * 批准与受理是两个请求：中间失败时用户能看到卡在哪一步，而不是笼统的"提交失败"。
 * 已批准的计划只受理、不重签 —— 重签会把 `granted_at` 悄悄改成现在。
 */
const props = defineProps<{ rootTaskId: string; plan: TaskPlan }>()
const emit = defineEmits<{ (e: 'changed'): void }>()

const busy = ref<'' | 'approve' | 'submit'>('')
const error = ref('')
/**
 * 许可上写的"谁批准的"取自已认证的握手，不取 auth store —— 那份 profile 只在设置页
 * 加载过，任务页上多半是空的；而且这件事应该以服务端认定的身份为准。
 * 拿不到就不给按钮：**不知道自己是谁的时候不签许可。**
 */
const grantedBy = ref('')

const approved = computed(() => props.plan.planning_state === 'approved')
const recipients = computed(() => recipientsOf(props.plan))
const remote = computed(() => recipients.value.filter((node) => node !== props.plan.root_coordinator_node_id))
/** 各边保留策略不一致时一份许可批不了 —— 把原因显示出来，不替用户挑一个。 */
const retention = computed(() => {
  try { return { label: retentionLabel(retentionOf(props.plan)), ok: true } }
  catch (cause) { return { label: cause instanceof Error ? cause.message : String(cause), ok: false } }
})
const payloads = computed(() => [...new Set(props.plan.data_edges.map((edge) => edge.payload_kind))])

function problem(cause: unknown, fallback: string): string {
  const detail = (cause as { response?: { data?: { error?: { code?: string; message?: string } } } })
    ?.response?.data?.error
  return detail?.code ? `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
    : `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

async function run() {
  error.value = ''
  try {
    if (!approved.value) {
      // busy 先置位再组装：`buildExecutionConsent` 也会抛（计划过期 / 各边保留策略不一致），
      // 那是批准这一步的失败，不该被下面的 catch 报成"受理执行失败"。
      busy.value = 'approve'
      const consent = buildExecutionConsent(props.plan, {
        rootTaskId: props.rootTaskId,
        grantedBy: grantedBy.value,
        now: Date.now(),
      })
      await tasksApi.approve(props.rootTaskId, props.plan.plan_digest, consent)
    }
    busy.value = 'submit'
    // 受理的幂等键绑到这一份修订：批准后重试不会变成第二次受理，
    // 而重新规划出的新修订本来就该是一次新的受理。
    await tasksApi.submit(props.rootTaskId, props.plan.plan_digest,
      `execute-${props.rootTaskId}-r${props.plan.revision}`)
    emit('changed')
  } catch (cause) {
    error.value = busy.value === 'approve' ? problem(cause, '批准失败') : problem(cause, '受理执行失败')
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  try {
    grantedBy.value = (await tasksApi.identity()).data.profile.subject
  } catch (cause) {
    error.value = problem(cause, '读取身份失败，现在无法批准')
  }
})
</script>

<template>
  <section class="approval" aria-label="计划审阅">
    <p class="lead">
      批准之前先看清这一份修订会把什么发给谁。你批准的是<strong>这个摘要</strong>；
      计划一变，这份许可自动失效。
    </p>
    <dl class="what">
      <div>
        <dt>接收方</dt>
        <dd>{{ remote.length ? remote.join('、') : '只有本节点，不外发' }}</dd>
      </div>
      <div>
        <dt>外发内容</dt>
        <dd>{{ payloads.length ? payloads.map(payloadKindLabel).join('、') : '没有跨节点数据边' }}</dd>
      </div>
      <div>
        <dt>保留策略</dt>
        <dd :class="{ bad: !retention.ok }">{{ retention.label }}</dd>
      </div>
      <div>
        <dt>有效至</dt>
        <dd class="ddp-mono">{{ plan.valid_until }}</dd>
      </div>
    </dl>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="actions">
      <el-button type="primary" :loading="!!busy" :disabled="!retention.ok || !grantedBy" @click="run">
        {{ busy === 'submit' ? '正在受理执行…' : approved ? '执行' : '批准并执行' }}
      </el-button>
    </div>
  </section>
</template>

<style scoped>
.approval { display: grid; gap: 14px; }
.lead { margin: 0; color: var(--ddp-ink-2); font-size: 13.5px; line-height: 1.7; }
.lead strong { color: var(--ddp-ink); font-weight: 600; }
.what { display: grid; gap: 6px; margin: 0; }
.what > div { display: flex; flex-wrap: wrap; gap: 10px; }
dt { color: var(--ddp-ink-3); font-size: 12.5px; min-width: 5em; }
dd { margin: 0; font-size: 13.5px; overflow-wrap: anywhere; }
dd.bad { color: var(--ddp-danger); }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.actions { display: flex; justify-content: flex-end; }
</style>
