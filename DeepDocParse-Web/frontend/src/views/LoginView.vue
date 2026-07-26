<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const mode = ref<'login' | 'register'>('login')
const form = ref({ username: '', password: '' })
const loading = ref(false)

async function submit() {
  if (form.value.username.length < 3 || form.value.password.length < 8) {
    ElMessage.warning('用户名至少 3 位，密码至少 8 位')
    return
  }
  loading.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(form.value.username, form.value.password)
    } else {
      await auth.register(form.value.username, form.value.password)
    }
    router.push((route.query.redirect as string) || '/dashboard')
  } catch {
    // 错误提示由 axios 拦截器统一弹出
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-card class="box">
    <template #header>
      <div class="head">
        <span class="title">DeepDocParse</span>
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="login">登录</el-radio-button>
          <el-radio-button value="register">注册</el-radio-button>
        </el-radio-group>
      </div>
    </template>
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名">
        <el-input v-model="form.username" autocomplete="username" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          autocomplete="current-password"
          @keyup.enter="submit"
        />
      </el-form-item>
      <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">
        {{ mode === 'login' ? '登录' : '注册并登录' }}
      </el-button>
    </el-form>
  </el-card>
</template>

<style scoped>
.box {
  width: 380px;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.title {
  font-size: 18px;
  font-weight: 600;
}
</style>
