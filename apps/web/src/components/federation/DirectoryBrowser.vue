<script setup lang="ts">
import { computed, ref } from 'vue'

import { directoryApi, type DirectoryMember, type MemberSnapshot } from '@/api/directory'
import StatusTag from '@/components/common/StatusTag.vue'
import {
  CAPABILITY_READINESS,
  MEMBER_EXPANSION_STATE,
  NODE_MEMBERSHIP_STATE,
  metaOf,
} from '@/constants/federation'
import { approvedPlanLabel } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

/**
 * 互联公开目录浏览：来源 / 目录修订 / 快照水位 / 离线与未知分支。
 *
 * 只读已封存的公开快照（`POST /api/v1/federation/member-snapshots` + 逐页读）。
 * **不编造全网总数**：总数只显示当前快照窗口的成员数与快照修订，
 * 离线（health != ready）、未知分支（expansion_state=unexpanded_subtree）、
 * 撤销（state=revoked）逐项照后端原样显示。管理员目录（nodes）403 时只说无权，
 * 不把“无权”显示成“没有成员”。
 *
 * 桌面中心源只读：封存是 POST（宿主会 403 拒绝），按钮禁用并写明原因，
 * 不让 403 被读成"当前账号无权查看"。
 */
const auth = useAuthStore()
const snapshot = ref<MemberSnapshot | null>(null)
const members = ref<DirectoryMember[]>([])
const nextCursor = ref<string | null>(null)
const complete = ref(false)
const loading = ref(false)
const error = ref('')

const online = computed(() => members.value.filter((m) => m.health === 'ready'))
const offline = computed(() => members.value.filter((m) => m.health !== 'ready'))
const revoked = computed(() => members.value.filter((m) => m.state === 'revoked'))
const unexpanded = computed(() => members.value.filter((m) => m.expansion_state === 'unexpanded_subtree'))

function problem(cause: unknown, fallback: string): string {
  const response = (cause as { response?: { status?: number; data?: { error?: { code?: string; message?: string } } } })?.response
  if (response?.status === 403) return `${fallback}：当前账号无权查看（403），不是“没有成员”。`
  if (response?.status === 410) return `${fallback}：快照已过期（410），请重新封存一份。`
  if (response?.status === 404) return `${fallback}：快照或游标不可用（404）。`
  const detail = response?.data?.error
  if (detail?.code) return `${fallback}：${detail.code}${detail.message ? `（${detail.message}）` : ''}`
  return `${fallback}：${cause instanceof Error ? cause.message : String(cause)}`
}

async function seal() {
  if (auth.readOnly) return
  loading.value = true
  error.value = ''
  members.value = []
  nextCursor.value = null
  complete.value = false
  try {
    const { data } = await directoryApi.createSnapshot({ page_size: 50, ttl_seconds: 900 })
    snapshot.value = data
    await readPage()
  } catch (cause) {
    error.value = problem(cause, '目录快照封存失败')
  } finally {
    loading.value = false
  }
}

async function readPage() {
  if (!snapshot.value) return
  loading.value = true
  try {
    const { data } = await directoryApi.snapshotPage(
      snapshot.value.snapshot_id, nextCursor.value ?? undefined)
    members.value = [...members.value, ...data.members]
    nextCursor.value = data.next_cursor
    complete.value = data.complete
    error.value = ''
  } catch (cause) {
    error.value = problem(cause, '目录页读取失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="directory" aria-label="互联公开目录">
    <p class="hint">
      只读已封存的公开快照：总数是当前快照窗口的成员数，不是全网普查。
      离线、未知分支与撤销逐项列出，不合并、不省略。
    </p>
    <div class="actions">
      <el-button :loading="loading" :disabled="auth.readOnly" @click="seal">封存当前可见目录</el-button>
      <el-button v-if="snapshot && !complete" :loading="loading" text @click="readPage">继续读下一页</el-button>
    </div>
    <p v-if="auth.readOnly" class="meta">{{ approvedPlanLabel() }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="snapshot" class="meta">
      来源 <span class="ddp-mono">{{ snapshot.authority_node_id }}</span> ·
      快照 <span class="ddp-mono">{{ snapshot.snapshot_id }}</span> ·
      目录修订 <span class="ddp-num">{{ snapshot.registry_revision }}</span> ·
      有效至 <span class="ddp-mono">{{ snapshot.expires_at }}</span> ·
      已读 <span class="ddp-num">{{ members.length }}</span> 项
      <span v-if="complete">（已读完本快照）</span><span v-else>（快照水位未读完）</span>
    </p>
    <p v-if="snapshot" class="meta">
      在线 <span class="ddp-num">{{ online.length }}</span> ·
      离线 <span class="ddp-num">{{ offline.length }}</span> ·
      未知分支 <span class="ddp-num">{{ unexpanded.length }}</span> ·
      已撤销 <span class="ddp-num">{{ revoked.length }}</span>
    </p>
    <div v-if="members.length" class="scroll">
      <table>
        <thead><tr><th>节点</th><th>成员状态</th><th>健康</th><th>接单</th><th>下级展开</th></tr></thead>
        <tbody>
          <tr v-for="member in members" :key="member.node_id">
            <td class="ddp-mono">{{ member.node_id }}<span class="muted"> · 修订 {{ member.revision }}</span></td>
            <td><StatusTag :meta="metaOf(NODE_MEMBERSHIP_STATE, member.state)" /></td>
            <td><StatusTag :meta="metaOf(CAPABILITY_READINESS, member.health)" /></td>
            <td>{{ member.accepting_admissions ? '接单' : '不接单' }}</td>
            <td><StatusTag :meta="metaOf(MEMBER_EXPANSION_STATE, member.expansion_state)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="snapshot && !loading" class="muted">本快照窗口内暂无可见成员。</p>
  </section>
</template>

<style scoped>
.directory { display: grid; gap: 12px; }
.hint, .meta, .muted { margin: 0; color: var(--ddp-ink-2); font-size: 13px; line-height: 1.7; }
.muted { color: var(--ddp-ink-3); }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 560px; }
th, td { padding: 6px 12px 6px 0; text-align: left; font-size: 13.5px; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
th { color: var(--ddp-ink-3); font-weight: 500; }
</style>
