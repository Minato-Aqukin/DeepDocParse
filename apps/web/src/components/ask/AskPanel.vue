<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { askStream, conversationsApi } from '@/api'
import CitationChip from '@/components/ask/CitationChip.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { confidenceOf, degradedLabelOf } from '@/constants/status'
import { approvedPlanLabel, federationTaskNewLocation, isDesktop } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'
import type { AnswerAssertion, CandidateDecision, ChatMessage, Citation, DocumentInfo } from '@/types/api'
import { fetchAuthedImage } from '@/utils/markdown'

/**
 * 会话式文档问答。回答带出处，点出处驱动左右两栏定位。
 *
 * 降级要显式展示：没做视觉验证 / 没检索到内容 / 上游挂了，都在气泡上打标。
 * 这个项目吃过静默降级的亏，UI 上不能装作一切正常。
 */
const props = defineProps<{ document: DocumentInfo }>()
const emit = defineEmits<{ (e: 'locate', citation: Citation): void }>()
// 中心只读入口（plan §1.5）：问答框旁预填当前问题跳到联邦任务新建页。
// router/pinia 按"没有就不显示入口"处理 —— 不在 setup 顶层硬拿，
// 否则只 mock @/api 的旧用例挂载即崩（与只读入口无关）。
let panelRouter: ReturnType<typeof useRouter> | undefined
try { panelRouter = useRouter() } catch { panelRouter = undefined }
let panelAuth: ReturnType<typeof useAuthStore> | undefined
try { panelAuth = useAuthStore() } catch { panelAuth = undefined }
const readonlyHint = computed(() => (isDesktop() && panelAuth?.readOnly ? approvedPlanLabel() : ''))
function proposeAsTask() {
  panelRouter?.push(federationTaskNewLocation(question.value.trim()))
}

const conversations = ref<{ id: string; title: string }[]>([])
const activeId = ref<string>('')
const messages = ref<ChatMessage[]>([])
const question = ref('')
const streaming = ref(false)
const streamText = ref('')
const scroller = ref<HTMLElement>()
const cropUrls = ref<Record<string, string>>({})   // crop_url -> blob URL
let abort: (() => void) | undefined
// 组件是否还活着。**光靠 onBeforeUnmount 里 revokeCrops 是不够的**：
// 取图请求还在飞的时候组件被卸载，revoke 已经跑过了，而请求返回后
// 又往 cropUrls 里塞一个新的 blob —— 那一个**再也没有人回收**。
// 用户点开一条出处又立刻切走就会触发，是日常操作。
// ExtractionsView 的 loadCrops 早就用同一个标志修过这个竞态（见那边的注释）。
let alive = true

const askable = computed(() => props.document.index_status === 'ready')
const indexHint = computed(() =>
  ({
    none: '文档还没有建立索引',
    pending: '索引排队中…',
    indexing: '正在建立索引…',
    failed: `索引失败：${props.document.index_error || '未知原因'}`,
    ready: '',
  })[props.document.index_status],
)
/**
 * 没有回答跟在后面的问题。服务端先落问题、生成结束（含出错、停止）才落回答；进程在
 * 生成中途被杀时收尾跑不到，库里只剩问题本身。不标出来的话它看起来像还在等（2026-09-24 E 实测）。
 */
const unanswered = computed(() => new Set(messages.value
  .filter((message, i) => message.role === 'user' && messages.value[i + 1]?.role !== 'assistant'
    && !(streaming.value && i === messages.value.length - 1))
  .map((message) => message.id)))

async function loadConversations() {
  conversations.value = (await conversationsApi.list(props.document.id)).data
  if (!conversations.value.length) return
  activeId.value = conversations.value[0]!.id
  await loadMessages()
}

async function loadMessages() {
  if (!activeId.value) return
  messages.value = (await conversationsApi.messages(activeId.value)).data
  await loadCrops()
  await scrollToEnd()
}

/** 出处缩略图受 JWT 保护，必须取回来换成 blob——直接绑到 src 上会 401。 */
async function loadCrops() {
  const pending = messages.value
    .flatMap((m) => [
      ...(m.citations || []),
      ...(m.assertions || []).flatMap((assertion) => assertion.citations),
    ])
    .filter((c) => c.crop_url && !cropUrls.value[c.crop_url])
  await Promise.all(
    pending.map(async (c) => {
      const objectUrl = await fetchAuthedImage(c.crop_url!)
      if (!objectUrl) return
      // 卸载后到手的 blob 当场回收：存进 cropUrls 的话 revokeCrops 已经跑过了
      if (!alive) URL.revokeObjectURL(objectUrl)
      else cropUrls.value[c.crop_url!] = objectUrl
    }),
  )
}

function verificationLabel(assertion: AnswerAssertion) {
  const { state, mode } = assertion.verification
  if (state === 'passed') return mode === 'human' ? '人工核对通过' : '自动核对通过'
  if (state === 'rejected') return mode === 'human' ? '人工核对驳回' : '自动核对不一致'
  if (state === 'questioned') return '人工标疑'
  return '尚未核对'
}

function verificationType(assertion: AnswerAssertion) {
  const { state } = assertion.verification
  if (state === 'passed') return 'success' as const
  if (state === 'rejected') return 'danger' as const
  if (state === 'questioned') return 'warning' as const
  return 'info' as const
}

function candidateSummary(candidates: CandidateDecision[]) {
  const accepted = candidates.filter((candidate) => candidate.accepted).length
  return `${accepted} 条通过 · ${candidates.length - accepted} 条拒绝`
}

function revokeCrops() {
  Object.values(cropUrls.value).forEach((u) => URL.revokeObjectURL(u))
  cropUrls.value = {}
}

/**
 * 刚新建的会话在服务端一定是空的，切过去时不用读。读回来的空列表晚于本地刚放上的问题气泡
 * 到达，会把它冲掉：新会话的第一问在回答中途断开时（服务端正好不可达、重读失败），
 * 问题从面板上直接消失（2026-09-24 E 浏览器实测）。
 */
let created = ''

async function newConversation() {
  // Creating/deleting a conversation is a center write: read-only desktop centers refuse it.
  if (readonlyHint.value) return
  const { data } = await conversationsApi.create(props.document.id)
  conversations.value.unshift({ id: data.id, title: data.title })
  created = data.id
  activeId.value = data.id
  messages.value = []
}

async function removeConversation(cid: string) {
  if (readonlyHint.value) return
  await conversationsApi.remove(cid)
  conversations.value = conversations.value.filter((c) => c.id !== cid)
  if (activeId.value === cid) {
    activeId.value = conversations.value[0]?.id ?? ''
    await loadMessages()
  }
}

async function send() {
  const text = question.value.trim()
  if (!text || streaming.value) return
  // Asking spends center compute (conversation + answer are POSTs): a read-only
  // desktop center only offers the federated-task entry; the host would 403 anyway.
  if (readonlyHint.value) {
    ElMessage.info(readonlyHint.value)
    return
  }
  if (!askable.value) {
    ElMessage.warning(indexHint.value)
    return
  }
  if (!activeId.value) await newConversation()

  messages.value.push({
    id: `local-${Date.now()}`, role: 'user', content: text, citations: [],
    verified: false, degraded: null, created_at: new Date().toISOString(),
  })
  question.value = ''
  streaming.value = true
  streamText.value = ''
  await scrollToEnd()

  let interrupted = false
  abort = askStream(activeId.value, text, {
    onDelta: (piece) => {
      streamText.value += piece
      void scrollToEnd()
    },
    onError: ({ message, code }) => {
      ElMessage.error(message)
      interrupted ||= code === 'stream_incomplete' || code === 'network_error'
    },
    onDone: async () => {
      await loadMessages()
      if (conversations.value.length) await loadConversations()
    },
    // 复位必须挂 onSettled 而不是 onDone：限速 429、索引未就绪 409、断网等
    // 请求都建立不起来的情况根本走不到 done 帧，只在 onDone 里复位会让面板
    // 永久卡在"回答中"，用户只能手动点停止
    onSettled: () => {
      streaming.value = false
      streamText.value = ''
      // 流在 done 之前断了：这一轮落没落库只有服务端知道，以会话记录为准重读。
      // 服务还没回来时重读会失败，留着本地的问题气泡（它下面会标"没有回答"）
      if (interrupted) loadMessages().catch(() => undefined)
    },
  })
}

function stop() {
  abort?.()
  // 服务端把这一轮落成 degraded=client_aborted（已产出的文字留着）。本地也立刻显示同一个
  // 状态：不这样做，停止后问题下面什么都没有，要刷新才看得到"回答被中断"（C 阶段浏览器实测）
  messages.value.push({
    id: `local-aborted-${Date.now()}`, role: 'assistant', content: streamText.value, citations: [],
    verified: false, degraded: 'client_aborted', created_at: new Date().toISOString(),
  })
  streaming.value = false
  streamText.value = ''
}

async function scrollToEnd() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(() => props.document.id, async () => {
  revokeCrops()
  await loadConversations()
}, { immediate: true })
watch(activeId, (id) => {
  if (id === created) {
    created = ''
    return
  }
  void loadMessages()
})
onBeforeUnmount(() => {
  alive = false
  abort?.()
  revokeCrops()
})
</script>

<template>
  <div class="ask-panel">
    <div class="head">
      <el-select v-model="activeId" size="small" placeholder="选择会话" class="picker">
        <el-option v-for="c in conversations" :key="c.id" :value="c.id" :label="c.title" />
      </el-select>
      <el-button size="small" :disabled="!!readonlyHint" @click="newConversation">新会话</el-button>
      <el-button v-if="activeId" size="small" link type="danger" :disabled="!!readonlyHint"
                 @click="removeConversation(activeId)">删除</el-button>
    </div>

    <el-alert v-if="!askable" :title="indexHint" type="info" :closable="false" class="notice" />

    <div ref="scroller" class="stream">
      <div v-for="message in messages" :key="message.id" class="bubble" :class="message.role">
        <div v-if="message.role !== 'assistant' || !message.assertions?.length" class="text">
          {{ message.content }}
        </div>
        <div v-else class="assertions">
          <section
            v-for="assertion in message.assertions"
            :key="assertion.id ?? assertion.position"
            class="assertion"
            :class="{ unsupported: assertion.unsupported }"
          >
            <div class="assertion-text">{{ assertion.text }}</div>
            <div class="assertion-state">
              <StatusTag
                v-if="assertion.unsupported || !assertion.evidence_ids.length"
                label="无证据支持"
                type="warning"
              />
              <StatusTag
                v-else
                :label="verificationLabel(assertion)"
                :type="verificationType(assertion)"
              />
            </div>
            <div v-if="assertion.citations.length" class="citations assertion-citations">
              <CitationChip
                v-for="(citation, i) in assertion.citations"
                :key="citation.evidence_id ?? i"
                :citation="citation"
                :index="i + 1"
                :crop-url="citation.crop_url ? cropUrls[citation.crop_url] : undefined"
                :warn-below="message.confidence?.warn_below"
                @locate="emit('locate', citation)"
              />
            </div>
          </section>
        </div>
        <div v-if="unanswered.has(message.id)" class="meta">
          <StatusTag label="没有回答" type="warning" />
          <span class="unanswered-hint">回答生成时连接或服务中断，这一轮没有保存下回答，可以重新提问</span>
        </div>

        <div v-if="message.role === 'assistant' && message.query_decision" class="agent-trace">
          <StatusTag
            :label="message.query_decision.need_retrieval
              ? '本轮执行检索'
              : `继承上一轮 ${message.query_decision.inherited_evidence_ids.length} 条证据`"
            type="info"
          />
          <!-- 这是"要不要检索"判定模型的理由，不是对回答的评价。不加前缀时
               "当前证据不足"会紧挨着一个有出处的答案，读起来像答案没依据（C 阶段浏览器实测）。
               判定模型不可用时理由就是降级码本身，旁边的降级标签已经说了，不再原样重复 -->
          <span v-if="message.query_decision.reason !== message.query_decision.degraded">
            检索判定理由：{{ message.query_decision.reason }}
          </span>
          <StatusTag
            v-if="message.query_decision.degraded"
            :label="degradedLabelOf(message.query_decision.degraded) ?? message.query_decision.degraded"
            type="warning"
          />
        </div>
        <details
          v-if="message.role === 'assistant' && message.retrieval?.candidates.length"
          class="candidate-trace"
        >
          <summary>候选门控：{{ candidateSummary(message.retrieval.candidates) }}</summary>
          <ol>
            <li
              v-for="candidate in message.retrieval.candidates"
              :key="`${candidate.document_id}:${candidate.chunk_id}:${candidate.rank}`"
            >
              <StatusTag
                :label="candidate.accepted ? '通过' : '拒绝'"
                :type="candidate.accepted ? 'success' : 'warning'"
              />
              <span>#{{ candidate.rank }} · {{ candidate.reason }}</span>
            </li>
          </ol>
        </details>
        <div v-if="message.role === 'assistant'" class="meta">
          <StatusTag v-if="message.verified" label="已做视觉验证" type="success" />
          <StatusTag
            v-else-if="message.degraded"
            :label="degradedLabelOf(message.degraded)!"
            type="warning"
          />
          <StatusTag
            v-if="confidenceOf(message.confidence?.level)"
            :meta="confidenceOf(message.confidence?.level)!"
          />
        </div>

        <!--
          低相关时主动提醒（借自 kotaemon）。**不拦着不给答案** ——
          把"我有多确信"交给用户判断，而不是替用户决定。
        -->
        <el-alert
          v-if="message.citations?.length && confidenceOf(message.confidence?.level)?.hint"
          class="confidence-hint"
          :title="confidenceOf(message.confidence?.level)!.hint"
          :type="message.confidence?.level === 'low' ? 'warning' : 'info'"
          :closable="false"
          show-icon
        />

        <div v-if="!message.assertions?.length && message.citations?.length" class="citations">
          <CitationChip
            v-for="(citation, i) in message.citations"
            :key="i"
            :citation="citation"
            :index="i + 1"
            :crop-url="citation.crop_url ? cropUrls[citation.crop_url] : undefined"
            :warn-below="message.confidence?.warn_below"
            @locate="emit('locate', citation)"
          />
        </div>
      </div>

      <div v-if="streaming" class="bubble assistant">
        <div class="text">{{ streamText }}<span class="caret">▍</span></div>
      </div>
      <el-empty v-if="!messages.length && !streaming" description="就这份文档问点什么" />
    </div>

    <div class="composer">
      <el-input v-model="question" type="textarea" :rows="2" :disabled="!askable"
                placeholder="例如：第 3 页的表格说明了什么？（Enter 发送）"
                @keydown.enter.exact.prevent="send" />
      <el-button v-if="!streaming" type="primary" :disabled="!askable || !!readonlyHint" @click="send">发送</el-button>
      <el-button v-else @click="stop">停止</el-button>
      <el-button v-if="readonlyHint" link @click="proposeAsTask">作为联邦任务发起</el-button>
    </div>
    <p v-if="readonlyHint" class="readonly-hint" role="note">{{ readonlyHint }}</p>
  </div>
</template>

<style scoped>
.ask-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 8px;
}
.head {
  display: flex;
  gap: 8px;
  align-items: center;
}
.picker {
  flex: 1;
}
.notice {
  padding: 6px 10px;
}
/* 索引失败的原因是一长串不带空格的 JSON，不强制折行会被横向截掉。
   降级/失败原因必须完整可见（铁律 3），截断等于没显示。 */
.notice :deep(.el-alert__title) {
  word-break: break-word;
  line-height: 1.6;
}
.stream {
  flex: 1;
  overflow: auto;
  padding-right: 4px;
}
/* 两种气泡必须一眼分得出谁在说话。
   原来一个用 --el-fill-color-light、一个用 --el-color-primary-light-9，
   而这两个 EP 变量在本规范里被映射到了同一个令牌 —— 实测撞成同色，
   分不出谁在说话。直接用 ddp 令牌钉死，不再依赖 EP 的调色板层级。 */
.bubble {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--ddp-panel-2);
}
/* 两种气泡不做左右分栏，所以只靠底色区分太弱（两档都只差 10 个色阶）。
   再加一道 2px 左边线：形状比色差可靠，和准则三是同一个道理。 */
.bubble.user {
  background: var(--ddp-panel-3);
  border-left: 2px solid var(--ddp-line-2);
}
.text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.assertions {
  display: grid;
  gap: 10px;
}
.assertion {
  display: grid;
  gap: 7px;
  padding-left: 10px;
  border-left: 2px solid var(--ddp-line-2);
}
.assertion.unsupported {
  border-left-style: dashed;
}
.assertion-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.assertion-state,
.agent-trace {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.agent-trace {
  margin-top: 8px;
  color: var(--ddp-ink-2);
  font-size: 12px;
}
.candidate-trace {
  margin-top: 8px;
  color: var(--ddp-ink-2);
  font-size: 12px;
}
.candidate-trace summary {
  cursor: pointer;
}
.candidate-trace ol {
  display: grid;
  gap: 6px;
  margin: 8px 0 0;
  padding-left: 22px;
}
.candidate-trace li {
  padding-left: 2px;
}
.candidate-trace li > span + span {
  margin-left: 6px;
}
.meta {
  margin-top: 6px;
}
.citations {
  margin-top: 8px;
  display: grid;
  gap: 6px;
}
.unanswered-hint {
  margin-left: 8px;
  color: var(--ddp-ink-2);
  font-size: 12px;
}
.confidence-hint {
  margin-top: 8px;
  padding: 6px 10px;
}
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.readonly-hint {
  margin: 0;
  font-size: 12px;
  color: var(--ddp-ink-3);
}
.caret {
  opacity: 0.5;
}
</style>
