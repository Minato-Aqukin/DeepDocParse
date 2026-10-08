<script setup lang="ts">
import { ingestRejectionLabelOf } from '@deepdocparse/contracts'
import { isAxiosError } from 'axios'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import { ingestStatusOf, uploadDirect, waitForIngest, waitForVerification } from '@/api/uploads'
import EngineOptionsForm from '@/components/engine/EngineOptionsForm.vue'
import { DEFAULT_ENGINE, defaultOptions, pruneOptions } from '@/constants/engines'
import { loadEnginePreference } from '@/utils/preferences'
import type { EngineChoice } from '@/types/api'

/**
 * 上传对话框：先选解析参数再传。
 *
 * 之前上传是"拖进来直接走默认参数"，后端支持的 engine/options 前端根本够不着；
 * 现在参数表单由 schema 驱动，默认值取自设置页的偏好。
 *
 * 这个对话框只做**永久上传**：字节送达（摘要校验）之后还有一段登记确认
 * （ingest）：服务端把 `DocumentSubmitted` 投递给语料域，corpus 确认之后
 * 会话的 `ingest_status` 才变 `ready`。只有两段都过了才算这份成功 ——
 * 登记 `ready` 表示**已登记**，不是解析/索引完成，那条链路另行展示。
 */
const emit = defineEmits<{ (e: 'uploaded'): void }>()

/**
 * 追加目标资源。ResourcesView 的"追加版本"把资源 id 传进来，
 * 缺省（独立上传入口）则不传，建独立资源。
 */
const props = withDefaults(defineProps<{ resourceId?: string }>(), { resourceId: undefined })

const visible = defineModel<boolean>({ default: false })

/**
 * 队列项的展示阶段。verifying = 服务端摘要校验（字节已传完）；
 * registering/retrying/rejected 只出现在登记段，与字节失败（failed）是两条路，
 * 绝不混在一起。rejected 是终态，不提供重试。
 */
type QueueStage = 'queued' | 'hashing' | 'uploading' | 'verifying' | 'registering' | 'retrying' | 'ready' | 'rejected' | 'failed'

interface UploadEntry {
  /** 队列项身份，同时是创建幂等键：重试同一项拿回的是同一个上传会话 */
  id: string
  file: File
  /** 选入队列那一刻的追加目标快照，之后 prop 再怎么变都与它无关 */
  targetResourceId: string | null
  /** finalize 之后拿到的会话 id；状态重试只用它查，不重传 */
  sessionId: string | null
  progress: number
  stage: QueueStage
  failed: string
}

const files = ref<UploadEntry[]>([])
const choice = ref<EngineChoice>(loadEnginePreference())
const uploading = ref(false)

/** 追加模式一次只传一份：新版本必须与目标资源一一对应 */
const appendMode = computed(() => props.resourceId !== undefined && props.resourceId !== '')

const CONCURRENCY = 3

function pick(event: Event) {
  const input = event.target
  if (!(input instanceof HTMLInputElement)) return
  addFiles(Array.from(input.files ?? []))
  input.value = ''
}

function onDrop(event: DragEvent) {
  addFiles(Array.from(event.dataTransfer?.files ?? []))
}

/**
 * 入队即冻结目标：每个文件记下**此刻** prop 的快照。
 * 关闭重开、切换资源后再回来，都不得悄悄改写已排队文件的目标 ——
 * 换目标只能移除重选，那会记下新的快照。
 */
function addFiles(incoming: File[]) {
  if (uploading.value) return
  const snapshot = appendMode.value ? (props.resourceId ?? null) : null
  let selected = incoming
  if (appendMode.value && files.value.length + incoming.length > 1) {
    selected = incoming.slice(0, Math.max(0, 1 - files.value.length))
    if (selected.length < incoming.length) ElMessage.warning('追加版本一次只能传一个文件')
  }
  files.value.push(...selected.map((file) => ({
    id: crypto.randomUUID(),
    file,
    targetResourceId: snapshot,
    sessionId: null,
    progress: 0,
    stage: 'queued' as QueueStage,
    failed: '',
  })))
}

function removeFile(index: number) {
  files.value.splice(index, 1)
}

function describeBackendError(error: unknown, fallback: string): string {
  const data = isAxiosError(error) ? error.response?.data : null
  const detail = data && typeof data === 'object' && 'error' in data ? data.error : null
  if (detail && typeof detail === 'object' && 'message' in detail
    && typeof detail.message === 'string') return detail.message
  return error instanceof Error ? error.message : fallback
}

function markFailed(entry: UploadEntry, message: string) {
  entry.stage = 'failed'
  entry.failed = message
}

/**
 * 已 finalize 会话的结算：只查状态，不传字节。
 *
 * 字节就绪（`ready`）只是第一段，永久上传还要等登记确认：
 * null/pending 是"还没确认"，不是完成；误把字节就绪当完成，
 * 会让用户以为已经登记入库了。`rejected` 是终态（目标非法/摘要冲突等
 * 确定性拒绝），`ingest_error` 是安全文本，直接展示，不再轮询。
 */
async function settleFinalized(entry: UploadEntry, sessionId: string): Promise<boolean> {
  entry.stage = 'verifying'
  const settled = await waitForVerification(sessionId)
  if (settled.status !== 'ready') {
    // finalize 返回的是 verifying，不是 ready。等校验出结果再往下走 ——
    // 摘要对不上的话整个会话会作废，那时说"已提交解析"就是骗人
    markFailed(entry, settled.error || `上传未通过校验（${settled.status}）`)
    return false
  }
  entry.stage = 'registering'
  const registered = await waitForIngest(sessionId, {
    onPoll: (polled) => {
      entry.stage = ingestStatusOf(polled) === 'retrying' ? 'retrying' : 'registering'
    },
  })
  if (ingestStatusOf(registered) === 'rejected') {
    entry.stage = 'rejected'
    entry.failed = ingestRejectionLabelOf(registered.ingest_error) ?? '登记被拒绝'
    return false
  }
  entry.stage = 'ready'
  return true
}

/**
 * 并发 3 上传；单个失败不影响整批，失败项留在列表里可重试。
 *
 * **字节流直传对象存储**，不经过任何应用进程（不变式 6）：
 * 拿预签名 -> 分片 PUT 到对象存储 -> finalize。finalize 之后走
 * settleFinalized 查两段状态。创建用队列项 id 作幂等键：创建响应丢了、
 * 分片断在半路，重试取回的是同一个会话，只补缺的分片，不会多造一份资产。
 * 已经 finalize 过的条目（sessionId 非空）直接复用原会话查状态，
 * 不重传字节、不建第二个会话 —— 目标更是早已冻结在会话里。
 */
async function submit() {
  if (!files.value.length) return ElMessage.warning('先选几个文件')
  uploading.value = true
  for (const entry of files.value) {
    entry.failed = ''
    entry.progress = 0
    entry.stage = 'queued'
  }
  const queue = [...files.value]
  const succeeded = new Set<string>()

  async function worker() {
    for (;;) {
      const entry = queue.shift()
      if (!entry) return
      try {
        if (!entry.sessionId) {
          const session = await uploadDirect(entry.file, {
            engine: choice.value.engine,
            options: pruneOptions(choice.value.options),
            // 用入队时的快照，不读当前 prop：重试也不得换目标
            targetResourceId: entry.targetResourceId,
            idempotencyKey: entry.id,
            onStage: (stage) => {
              entry.stage = stage
            },
            onProgress: (percent) => {
              entry.progress = percent
            },
          })
          entry.sessionId = session.id
        }
        if (await settleFinalized(entry, entry.sessionId)) succeeded.add(entry.id)
      } catch (error) {
        // 状态轮询超时（校验/登记）只表示"还没确认"：会话已保留，
        // 失败项留在列表里，状态重试只查不传
        markFailed(entry, describeBackendError(error, '上传失败'))
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker))
  uploading.value = false
  files.value = files.value.filter((entry) => !succeeded.has(entry.id))
  if (succeeded.size) {
    ElMessage.success(`${succeeded.size} 个文件已登记`)
    emit('uploaded')
  }
  if (!files.value.length) visible.value = false
}

/**
 * 状态重试：只查不传。复用 finalize 时拿到的会话 id 去轮询，
 * 不重建会话、不重传字节 —— 换目标更是不允许（目标已冻结在会话里）。
 * 终态 rejected 不提供重试：确定性拒绝查多少次都是同一个结果。
 */
async function retryStatus(entry: UploadEntry) {
  const sessionId = entry.sessionId
  if (!sessionId || uploading.value) return
  uploading.value = true
  entry.failed = ''
  try {
    if (await settleFinalized(entry, sessionId)) {
      files.value = files.value.filter((item) => item.id !== entry.id)
      ElMessage.success('已登记')
      emit('uploaded')
      if (!files.value.length) visible.value = false
    }
  } catch (error) {
    if (entry.stage !== 'rejected') markFailed(entry, describeBackendError(error, '查询状态失败，会话已保留'))
  } finally {
    uploading.value = false
  }
}

function stageLabel(entry: UploadEntry): string {
  switch (entry.stage) {
    case 'hashing': return '计算摘要…'
    case 'verifying': return '已上传，校验中…'
    case 'registering': return '字节已就绪，登记中…'
    case 'retrying': return '登记重试中…'
    default: return ''
  }
}

function resetOptions() {
  choice.value = { engine: DEFAULT_ENGINE, options: defaultOptions(DEFAULT_ENGINE) }
}
</script>

<template>
  <el-dialog v-model="visible" title="上传文档" width="560px">
    <div class="dropzone" @drop.prevent="onDrop" @dragover.prevent>
      <input id="upload-input" type="file" :multiple="!appendMode" hidden
             :disabled="uploading" accept=".pdf,.png,.jpg,.jpeg,.webp,.docx,.pptx,.xlsx" @change="pick" />
      <label for="upload-input" class="pick">
        <el-icon class="big"><component is="UploadFilled" /></el-icon>
        <div>把文件拖到这里，或<em>点击选择</em></div>
        <div class="hint">{{ appendMode
          ? '追加为该资源的一个新固定版本；已有版本与历史引用不会被改写'
          : '支持 PDF / 图片 / Office；每次上传创建独立资源，同名文件不会被合并' }}</div>
      </label>
    </div>

    <div v-if="files.length" class="files">
      <div v-for="(entry, i) in files" :key="entry.id" class="file">
        <span class="name">{{ entry.file.name }}</span>
        <el-progress v-if="uploading && entry.stage === 'uploading'"
                     :percentage="entry.progress" :show-text="false" class="bar" />
        <!-- **校验中/登记中要单独说出来**：字节已经传完了，但服务端还在重算摘要、
             之后还要把登记投递给语料域确认。显示成"上传中"会让人以为还在传，
             显示成"已完成"会让人以为已经解析好了 —— 两句都是骗人 -->
        <span v-if="stageLabel(entry)" class="verifying">{{ stageLabel(entry) }}</span>
        <span v-if="entry.failed" class="error">{{ entry.failed }}</span>
        <el-button v-if="!uploading && entry.sessionId && entry.stage === 'failed'" link @click="retryStatus(entry)">重试查询</el-button>
        <el-button v-if="!uploading" link @click="removeFile(i)">移除</el-button>
      </div>
    </div>

    <el-divider>解析参数</el-divider>
    <EngineOptionsForm v-model="choice" />
    <el-button link type="primary" @click="resetOptions">恢复默认</el-button>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="uploading" @click="submit">
        上传 {{ files.length || '' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dropzone {
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  padding: 20px;
  text-align: center;
}
.pick {
  cursor: pointer;
  display: block;
  color: var(--el-text-color-regular);
}
.big {
  font-size: 34px;
  color: var(--el-color-primary);
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
.files {
  margin-top: 12px;
  display: grid;
  gap: 6px;
  max-height: 180px;
  overflow: auto;
}
.file {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar {
  width: 120px;
}
.error {
  color: var(--el-color-danger);
  font-size: 12px;
}
.verifying {
  color: var(--ddp-ink-3);
  font-size: 12px;
  white-space: nowrap;
}
</style>
