<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import EngineOptionsForm from '@/components/engine/EngineOptionsForm.vue'
import { useAuthStore } from '@/stores/auth'
import { loadEnginePreference, saveEnginePreference } from '@/utils/preferences'
import type { EngineChoice } from '@/types/api'

/** 账号信息（来自 /api/auth/me，顺带校验会话）+ 本机默认解析参数。 */
const auth = useAuthStore()
const choice = ref<EngineChoice>(loadEnginePreference())
const loading = ref(false)

async function loadProfile() {
  loading.value = true
  try {
    await auth.fetchProfile()
  } finally {
    loading.value = false
  }
}

function savePreference() {
  saveEnginePreference(choice.value)
  ElMessage.success('已保存为本机默认，下次上传会带上这组参数')
}

onMounted(loadProfile)
</script>

<template>
  <div class="page">
    <el-card shadow="never" class="block" v-loading="loading">
      <template #header>账号</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="用户名">
          {{ auth.profile?.username ?? auth.username ?? '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="邮箱">{{ auth.profile?.email || '未绑定' }}</el-descriptions-item>
        <el-descriptions-item label="注册时间">
          {{ auth.profile ? new Date(auth.profile.created_at).toLocaleString() : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="用户 ID">
          <code>{{ auth.profile?.user_id ?? '—' }}</code>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        默认解析参数
        <span class="hint">上传对话框会用这组参数做默认值（只存在本机）</span>
      </template>
      <EngineOptionsForm v-model="choice" />
      <el-button type="primary" @click="savePreference">保存</el-button>
    </el-card>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
  max-width: 720px;
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 8px;
  font-weight: normal;
}
</style>
