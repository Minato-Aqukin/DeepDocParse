<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api, type KeyInfo } from '@/api/client'

const keys = ref<KeyInfo[]>([])
const loading = ref(false)
const dialog = ref(false)
const created = ref<string>('')
const form = ref({ name: 'default', quota_pages: 1000, unlimited: false, rate_limit_per_min: 60 })

async function refresh() {
  loading.value = true
  try {
    keys.value = (await api.listKeys()).data
  } finally {
    loading.value = false
  }
}

async function create() {
  const { data } = await api.createKey(form.value)
  created.value = data.key // 明文只有这一次
  dialog.value = false
  await refresh()
}

async function revoke(key: KeyInfo) {
  await ElMessageBox.confirm(`吊销「${key.name}」？使用该 key 的调用会立刻 401。`, '确认', {
    type: 'warning',
  })
  await api.revokeKey(key.id)
  await refresh()
}

async function copy(text: string) {
  await navigator.clipboard.writeText(text)
  ElMessage.success('已复制')
}

onMounted(refresh)
</script>

<template>
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
    <el-table-column prop="name" label="名称" width="140" />
    <el-table-column prop="key_prefix" label="前缀" width="150" />
    <el-table-column label="用量" width="160">
      <template #default="{ row }">
        {{ row.used_pages }} / {{ row.quota_pages ?? '∞' }} 页
        <el-progress
          v-if="row.quota_pages"
          :percentage="Math.min(100, Math.round((row.used_pages / row.quota_pages) * 100))"
          :show-text="false"
        />
      </template>
    </el-table-column>
    <el-table-column prop="rate_limit_per_min" label="限速(次/分)" width="120" />
    <el-table-column label="最近使用" width="180">
      <template #default="{ row }">
        {{ row.last_used_at ? new Date(row.last_used_at).toLocaleString() : '—' }}
      </template>
    </el-table-column>
    <el-table-column label="状态" width="100">
      <template #default="{ row }">
        <el-tag size="small" :type="row.revoked_at ? 'danger' : 'success'">
          {{ row.revoked_at ? '已吊销' : '可用' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="100">
      <template #default="{ row }">
        <el-button link type="danger" :disabled="!!row.revoked_at" @click="revoke(row)">
          吊销
        </el-button>
      </template>
    </el-table-column>
  </el-table>

  <el-dialog v-model="dialog" title="新建 API Key" width="420px">
    <el-form label-width="90px">
      <el-form-item label="名称">
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item label="不限额度">
        <el-switch v-model="form.unlimited" />
      </el-form-item>
      <el-form-item label="页数额度" v-if="!form.unlimited">
        <el-input-number v-model="form.quota_pages" :min="1" />
      </el-form-item>
      <el-form-item label="限速">
        <el-input-number v-model="form.rate_limit_per_min" :min="1" /> 次/分钟
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialog = false">取消</el-button>
      <el-button type="primary" @click="create">创建</el-button>
    </template>
  </el-dialog>
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
</style>
