<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { api, askStream, type ChatMessage, type Citation, type DocumentInfo } from '@/api/client'
import { fetchAuthedImage } from '@/utils/markdown'

/**
 * 会话式文档问答。回答带出处，点出处驱动左右两栏定位。
 *
 * 降级要显式展示：没做视觉验证 / 没检索到内容 / 上游挂了，都在气泡上打标。
 * 这个项目吃过静默降级的亏，UI 上不能装作一切正常。
 */
const props = defineProps<{ document: DocumentInfo }>()
const emit = defineEmits<{ (e: 'locate', citation: Citation): void }>()

const conversations = ref<{ id: string; title: string }[]>([])
const activeId = ref<string>('')
const messages = ref<ChatMessage[]>([])
const question = ref('')
const streaming = ref(false)
const streamText = ref('')
const scroller = ref<HTMLElement>()
const cropUrls = ref<Record<string, string>>({})   // crop_url -> blob URL
let abort: (() => void) | undefined

const DEGRADED_LABEL: Record<string, string> = {
  vision_unavailable: '未做视觉验证（视觉模型不可用）',
  crop_unsupported: '未做视觉验证（该文件不支持区域截图）',
  crop_failed: '未做视觉验证（区域截图失败）',
  embedding_unavailable: '仅关键词检索（向量化服务不可用）',
  no_hits: '未在本文档中检索到相关内容',
  client_aborted: '回答被中断',
  upstream_error: '问答服务异常',
}

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

async function loadConversations() {
  conversations.value = (await api.listConversations(props.document.id)).data
  if (!conversations.value.length) return
  activeId.value = conversations.value[0]!.id
  await loadMessages()
}

async function loadMessages() {
  if (!activeId.value) return
  messages.value = (await api.listMessages(activeId.value)).data
  await loadCrops()
  await scrollToEnd()
}

/** 出处缩略图受 JWT 保护，必须取回来换成 blob——直接绑到 src 上会 401。 */
async function loadCrops() {
  const pending = messages.value
    .flatMap((m) => m.citations || [])
    .filter((c) => c.crop_url && !cropUrls.value[c.crop_url])
  await Promise.all(
    pending.map(async (c) => {
      const objectUrl = await fetchAuthedImage(c.crop_url!)
      if (objectUrl) cropUrls.value[c.crop_url!] = objectUrl
    }),
  )
}

function revokeCrops() {
  Object.values(cropUrls.value).forEach((u) => URL.revokeObjectURL(u))
  cropUrls.value = {}
}

async function newConversation() {
  const { data } = await api.createConversation(props.document.id)
  conversations.value.unshift({ id: data.id, title: data.title })
  activeId.value = data.id
  messages.value = []
}

async function removeConversation(cid: string) {
  await api.deleteConversation(cid)
  conversations.value = conversations.value.filter((c) => c.id !== cid)
  if (activeId.value === cid) {
    activeId.value = conversations.value[0]?.id ?? ''
    await loadMessages()
  }
}

async function send() {
  const text = question.value.trim()
  if (!text || streaming.value) return
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

  abort = askStream(activeId.value, text, {
    onDelta: (piece) => {
      streamText.value += piece
      void scrollToEnd()
    },
    onError: ({ message }) => ElMessage.error(message),
    onDone: async () => {
      streaming.value = false
      streamText.value = ''
      await loadMessages()
      if (conversations.value.length) await loadConversations()
    },
  })
}

function stop() {
  abort?.()
  streaming.value = false
}

async function scrollToEnd() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(() => props.document.id, async () => {
  revokeCrops()
  await loadConversations()
}, { immediate: true })
watch(activeId, loadMessages)
onBeforeUnmount(() => {
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
      <el-button size="small" @click="newConversation">新会话</el-button>
      <el-button v-if="activeId" size="small" link type="danger"
                 @click="removeConversation(activeId)">删除</el-button>
    </div>

    <el-alert v-if="!askable" :title="indexHint" type="info" :closable="false" class="notice" />

    <div ref="scroller" class="stream">
      <div v-for="message in messages" :key="message.id" class="bubble" :class="message.role">
        <div class="text">{{ message.content }}</div>
        <div v-if="message.role === 'assistant'" class="meta">
          <el-tag v-if="message.verified" size="small" type="success">已做视觉验证</el-tag>
          <el-tag v-else-if="message.degraded" size="small" type="warning">
            {{ DEGRADED_LABEL[message.degraded] || message.degraded }}
          </el-tag>
        </div>
        <div v-if="message.citations?.length" class="citations">
          <div v-for="(citation, i) in message.citations" :key="i" class="citation"
               @click="emit('locate', citation)">
            <img v-if="citation.crop_url && cropUrls[citation.crop_url]"
                 :src="cropUrls[citation.crop_url]" alt="出处截图" />
            <div class="cite-text">
              <b>[{{ i + 1 }}] 第 {{ citation.page_idx + 1 }} 页</b>
              <span>{{ citation.snippet }}</span>
            </div>
          </div>
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
      <el-button v-if="!streaming" type="primary" :disabled="!askable" @click="send">发送</el-button>
      <el-button v-else @click="stop">停止</el-button>
    </div>
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
.stream {
  flex: 1;
  overflow: auto;
  padding-right: 4px;
}
.bubble {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
}
.bubble.user {
  background: var(--el-color-primary-light-9);
}
.text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.meta {
  margin-top: 6px;
}
.citations {
  margin-top: 8px;
  display: grid;
  gap: 6px;
}
.citation {
  display: flex;
  gap: 8px;
  padding: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.citation:hover {
  border-color: var(--el-color-primary);
}
.citation img {
  width: 88px;
  max-height: 60px;
  object-fit: cover;
  border-radius: 2px;
}
.cite-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--el-text-color-regular);
}
.composer {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.caret {
  opacity: 0.5;
}
</style>
