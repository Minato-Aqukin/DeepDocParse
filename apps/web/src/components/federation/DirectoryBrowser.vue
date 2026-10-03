<script setup lang="ts">
import { CAPABILITY_READINESS_VALUES, type CapabilityReadiness } from '@deepdocparse/contracts'
import { computed, ref } from 'vue'

import { directoryApi, type DirectoryMember, type DirectoryScopeEnvelope, type MemberSnapshot } from '@/api/directory'
import { resourcesApi, type Resource } from '@/api/resources'
import DirectoryCoverage from '@/components/federation/DirectoryCoverage.vue'
import ScopeTargets from '@/components/federation/ScopeTargets.vue'
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
 * 成员快照与公开集合范围使用同一快照绑定；分页结束不代表全网普查。
 * 资源目录仅使用本站公开资源接口，临时计算没有公开固定版本，不进入目录。
 * 无权与读取失败内联显示，不把降级显示成“没有成员”。
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
const scope = ref<DirectoryScopeEnvelope | null>(null)
const resources = ref<Resource[]>([])
const resourceOffset = ref(0)
const resourceHasMore = ref(false)
const resourceLoading = ref(false)
const resourceError = ref('')

const healthCounts = computed(() => {
  const counts: Record<CapabilityReadiness, number> = { configured: 0, ready: 0, draining: 0, unhealthy: 0, unknown: 0 }
  for (const member of members.value) counts[member.health]++
  return counts
})
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
  snapshot.value = null
  scope.value = null
  resources.value = []
  resourceOffset.value = 0
  resourceHasMore.value = false
  resourceError.value = ''
  members.value = []
  nextCursor.value = null
  complete.value = false
  try {
    const { data } = await directoryApi.createSnapshot({ page_size: 50, ttl_seconds: 900 })
    snapshot.value = data
    await readPage()
    const response = await directoryApi.createScope(data.snapshot_id)
    scope.value = response.data
    await readResources()
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

async function readResources() {
  resourceLoading.value = true
  resourceError.value = ''
  try {
    const { data } = await resourcesApi.list('site_public', resourceOffset.value)
    if (!Array.isArray(data?.items) || typeof data.has_more !== 'boolean') throw new Error('本站公开资源目录格式不兼容')
    resources.value.push(...data.items.filter(resource => resource.publication === 'published' && resource.versions.length > 0))
    resourceOffset.value += data.items.length
    resourceHasMore.value = data.has_more
  } catch (cause) {
    resourceError.value = problem(cause, '本站公开资源读取失败')
  } finally {
    resourceLoading.value = false
  }
}
</script>

<template>
  <section class="directory" aria-label="互联公开目录">
    <p class="hint">
      公开资源与集合按本站 / 远端分开；临时计算不进入公共目录。
      目录分页只描述本次可见范围，离线、未知分支与撤销逐项列出，不代表全网普查。
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
      <span v-if="complete">（已读到成员快照终止页）</span><span v-else>（成员目录页尚未读完）</span>
    </p>
    <p v-if="snapshot" class="meta" aria-label="节点健康统计">
      <span v-for="health in CAPABILITY_READINESS_VALUES" :key="health">
        {{ metaOf(CAPABILITY_READINESS, health).label }} <span class="ddp-num">{{ healthCounts[health] }}</span> ·
      </span>
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
    <DirectoryCoverage v-if="scope" :scope="scope" />
    <ScopeTargets v-if="scope && snapshot" :key="scope.manifest.scope_id" :scope-id="scope.manifest.scope_id" :local-node-id="snapshot.authority_node_id" />
    <section v-if="scope && snapshot" aria-label="本站公开资源">
      <h3>本站公开资源</h3>
      <p class="hint">本站当前公开的固定版本资源；与上方封存的集合分母独立，资源分页读取期间可能变化。</p>
      <p v-if="resourceError" role="alert" class="error">{{ resourceError }}</p>
      <div v-if="resources.length" class="scroll">
        <table>
          <thead><tr><th>资源</th><th>来源节点</th><th>上传者引用</th><th>归属</th></tr></thead>
          <tbody>
            <tr v-for="resource in resources" :key="resource.id">
              <td>{{ resource.display_name }} <span class="ddp-mono">{{ resource.id }}</span></td>
              <td class="ddp-mono">{{ snapshot.authority_node_id }}</td>
              <td class="ddp-mono">{{ resource.uploader_ref.issuer }} / {{ resource.uploader_ref.subject }}</td>
              <td class="ddp-mono">{{ resource.owner_id }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!resourceLoading && !resourceError" class="muted">当前已读本站目录页内暂无公开资源。</p>
      <el-button v-if="resourceHasMore || resourceError" :loading="resourceLoading" text @click="readResources">{{ resourceError ? '重试本站公开资源页' : '继续读本站资源下一页' }}</el-button>
    </section>
  </section>
</template>

<style scoped>
.directory { display: grid; gap: 12px; }
.hint, .meta, .muted { margin: 0; color: var(--ddp-ink-2); font-size: 13px; line-height: 1.7; }
.muted { color: var(--ddp-ink-3); }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
h3 { font-size: 14px; font-weight: 600; margin: 12px 0 8px; }
.error { border-left: 2px solid var(--ddp-danger); padding-left: 12px; color: var(--ddp-danger); margin: 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 560px; }
th, td { padding: 6px 12px 6px 0; text-align: left; font-size: 13.5px; border-bottom: var(--ddp-bw) solid var(--ddp-line); }
th { color: var(--ddp-ink-3); font-weight: 500; }
</style>
