<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import StatusTag from '@/components/common/StatusTag.vue'
import { keysApi, type CreateKeyPayload } from '@/api'
import type { KeyInfo } from '@/types/api'

const keys = ref<KeyInfo[]>([])
const loading = ref(false)
const dialog = ref(false)
const created = ref('')

const form = ref<CreateKeyPayload>({
  name: 'default',
  unlimited: false,
  quota_pages: 1000,
  rate_limit_per_min: 60,
  expires_in_days: null,
})

async function refresh() {
  loading.value = true
  try {
    keys.value = (await keysApi.list()).data
  } finally {
    loading.value = false
  }
}

async function create() {
  const { data } = await keysApi.create(form.value)
  created.value = data.key // 明文只有这一次
  dialog.value = false
  await refresh()
}

async function revoke(key: KeyInfo) {
  await ElMessageBox.confirm(`吊销「${key.name}」？使用该 key 的调用会立刻 401。`, '确认', {
    type: 'warning',
  })
  await keysApi.revoke(key.id)
  await refresh()
}

async function copy(text: string) {
  await navigator.clipboard.writeText(text)
  ElMessage.success('已复制')
}

function expiryText(key: KeyInfo) {
  if (!key.expires_at) return '永不过期'
  const at = new Date(key.expires_at)
  return `${at.toLocaleDateString('zh-CN')}${at < new Date() ? '（已过期）' : ''}`
}

onMounted(refresh)
</script>

<template>
  <div class="page">
    <div class="bar">
      <span class="hint">key 明文只在创建时显示一次，库里只存哈希 —— 丢了只能重建</span>
      <el-button type="primary" @click="dialog = true">新建 Key</el-button>
    </div>

    <el-alert v-if="created" type="success" :closable="false" class="created">
      <div class="created-body">
        <code>{{ created }}</code>
        <div>
          <el-button size="small" @click="copy(created)">复制</el-button>
          <el-button size="small" link @click="created = ''">我已保存</el-button>
        </div>
      </div>
    </el-alert>

    <el-table :data="keys" v-loading="loading">
      <el-table-column prop="name" label="名称" width="130" />
      <el-table-column prop="key_prefix" label="前缀" width="140" />
      <el-table-column label="用量" width="170">
        <template #default="{ row }">
          {{ row.used_pages }} / {{ row.quota_pages ?? '∞' }} 页
          <el-progress
            v-if="row.quota_pages"
            :percentage="Math.min(100, Math.round((row.used_pages / row.quota_pages) * 100))"
            :show-text="false"
          />
        </template>
      </el-table-column>
      <el-table-column prop="rate_limit_per_min" label="限速(次/分)" width="110" />
      <el-table-column label="过期" width="150">
        <template #default="{ row }">{{ expiryText(row) }}</template>
      </el-table-column>
      <el-table-column label="最近使用" width="196">
        <template #default="{ row }">
          <span class="ddp-num">{{ row.last_used_at ? new Date(row.last_used_at).toLocaleString('zh-CN') : '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="94">
        <template #default="{ row }">
          <StatusTag
            :label="row.revoked_at ? '已吊销' : '可用'"
            :type="row.revoked_at ? 'danger' : 'success'"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" :disabled="!!row.revoked_at" @click="revoke(row)">
            吊销
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="新建 API Key" width="440px">
      <el-form label-width="100px" label-position="left">
        <el-form-item label="名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="不限额度">
          <el-switch v-model="form.unlimited" />
        </el-form-item>
        <el-form-item v-if="!form.unlimited" label="页数额度">
          <el-input-number v-model="form.quota_pages" :min="1" />
        </el-form-item>
        <el-form-item label="限速">
          <el-input-number v-model="form.rate_limit_per_min" :min="1" />
          <span class="unit">次/分钟</span>
        </el-form-item>
        <el-form-item label="有效期">
          <el-input-number v-model="form.expires_in_days" :min="1" placeholder="留空永不过期" />
          <span class="unit">天</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.created {
  margin-bottom: 12px;
}
.created-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
code {
  word-break: break-all;
}
.unit {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
