<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { uploadDirect, waitForVerification } from '@/api/uploads'
import EngineOptionsForm from '@/components/engine/EngineOptionsForm.vue'
import { DEFAULT_ENGINE, defaultOptions, pruneOptions } from '@/constants/engines'
import { loadEnginePreference } from '@/utils/preferences'
import type { EngineChoice } from '@/types/api'

/**
 * 上传对话框：先选解析参数再传。
 *
 * 之前上传是"拖进来直接走默认参数"，后端支持的 engine/options 前端根本够不着；
 * 现在参数表单由 schema 驱动，默认值取自设置页的偏好。
 */
const emit = defineEmits<{ (e: 'uploaded'): void }>()

const visible = defineModel<boolean>({ default: false })
const files = ref<File[]>([])
const choice = ref<EngineChoice>(loadEnginePreference())
const uploading = ref(false)
const progress = ref<Record<string, number>>({})
const failed = ref<Record<string, string>>({})
/** 已经传完、正在等服务端校验摘要的文件。**这一段必须让用户看得见** */
const verifying = ref<Record<string, boolean>>({})

const CONCURRENCY = 3

function pick(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files ?? []))
  input.value = ''
}

function onDrop(event: DragEvent) {
  addFiles(Array.from(event.dataTransfer?.files ?? []))
}

function addFiles(incoming: File[]) {
  const existing = new Set(files.value.map((f) => `${f.name}:${f.size}`))
  files.value.push(...incoming.filter((f) => !existing.has(`${f.name}:${f.size}`)))
}

function removeFile(index: number) {
  files.value.splice(index, 1)
}

/**
 * 并发 3 上传；单个失败不影响整批，失败项留在列表里可重试。
 *
 * **字节流直传对象存储**，不经过任何应用进程（不变式 6）：
 * 拿预签名 -> 分片 PUT 到对象存储 -> finalize。finalize 之后还有一段
 * 服务端摘要校验，那段显示"校验中" —— 它没通过之前文档不进解析，
 * 假装已经好了会让用户以为自己传成功了。
 */
async function submit() {
  if (!files.value.length) return ElMessage.warning('先选几个文件')
  uploading.value = true
  failed.value = {}
  const queue = [...files.value]
  const succeeded: File[] = []

  async function worker() {
    for (;;) {
      const file = queue.shift()
      if (!file) return
      try {
        const session = await uploadDirect(file, {
          engine: choice.value.engine,
          options: pruneOptions(choice.value.options),
          onProgress: (percent) => (progress.value[file.name] = percent),
        })
        // finalize 返回的是 verifying，不是 ready。等校验出结果再算这份成功 ——
        // 摘要对不上的话整个会话会作废，那时说"已提交解析"就是骗人
        verifying.value[file.name] = true
        const settled = await waitForVerification(session.id)
        verifying.value[file.name] = false
        if (settled.status !== 'ready') {
          failed.value[file.name] = settled.error || `上传未通过校验（${settled.status}）`
          continue
        }
        succeeded.push(file)
      } catch (error) {
        verifying.value[file.name] = false
        failed.value[file.name] =
          (error as { response?: { data?: { error?: { message?: string } } } }).response?.data
            ?.error?.message ||
          (error as Error).message ||
          '上传失败'
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker))
  uploading.value = false
  files.value = files.value.filter((f) => !succeeded.includes(f))
  if (succeeded.length) {
    ElMessage.success(`${succeeded.length} 个文件已提交解析`)
    emit('uploaded')
  }
  if (!files.value.length) visible.value = false
}

function resetOptions() {
  choice.value = { engine: DEFAULT_ENGINE, options: defaultOptions(DEFAULT_ENGINE) }
}
</script>

<template>
  <el-dialog v-model="visible" title="上传文档" width="560px"
             @open="(failed = {}), (verifying = {})">
    <div class="dropzone" @drop.prevent="onDrop" @dragover.prevent>
      <input id="upload-input" type="file" multiple hidden
             accept=".pdf,.png,.jpg,.jpeg,.webp,.docx,.pptx,.xlsx" @change="pick" />
      <label for="upload-input" class="pick">
        <el-icon class="big"><component is="UploadFilled" /></el-icon>
        <div>把文件拖到这里，或<em>点击选择</em></div>
        <div class="hint">支持 PDF / 图片 / Office；同一文件重复上传会复用已有结果</div>
      </label>
    </div>

    <div v-if="files.length" class="files">
      <div v-for="(file, i) in files" :key="`${file.name}-${i}`" class="file">
        <span class="name">{{ file.name }}</span>
        <el-progress v-if="uploading && !verifying[file.name]"
                     :percentage="progress[file.name] ?? 0" :show-text="false" class="bar" />
        <!-- **校验中要单独说出来**：字节已经传完了，但服务端还在重算摘要，
             摘要对不上整个会话会作废。显示成"上传中"会让人以为还在传 -->
        <span v-if="verifying[file.name]" class="verifying">已上传，校验中…</span>
        <span v-if="failed[file.name]" class="error">{{ failed[file.name] }}</span>
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
  color: var(--ddp-text-muted, #888);
  font-size: 12px;
  white-space: nowrap;
}
</style>
