<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const showChrome = computed(() => route.name !== 'login')
const activeMenu = computed(() => `/${String(route.path).split('/')[1] || 'dashboard'}`)

function logout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="app">
    <el-header v-if="showChrome" class="topbar">
      <div class="brand">DeepDocParse</div>
      <el-menu :default-active="activeMenu" mode="horizontal" router :ellipsis="false" class="nav">
        <el-menu-item index="/documents">文档</el-menu-item>
        <el-menu-item index="/search">检索</el-menu-item>
        <el-menu-item index="/keys">API Key</el-menu-item>
        <el-menu-item index="/usage">用量</el-menu-item>
      </el-menu>
      <div class="spacer" />
      <span class="user">{{ auth.username }}</span>
      <el-button link @click="logout">退出</el-button>
    </el-header>
    <el-main :class="{ plain: !showChrome }">
      <RouterView />
    </el-main>
  </el-container>
</template>

<style scoped>
.app {
  min-height: 100vh;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.brand {
  font-weight: 600;
  font-size: 18px;
}
.nav {
  border-bottom: none;
}
.spacer {
  flex: 1;
}
.user {
  color: var(--el-text-color-secondary);
}
.plain {
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
