<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'

import { NAV_GROUPS, type NavGroup } from '@/constants/nav'
import { useAuthStore } from '@/stores/auth'

/**
 * 应用外壳：侧边栏 + 顶部条 + 内容区。
 *
 * 菜单**完全由路由 meta 派生**（meta.nav / meta.group / meta.icon / meta.title），
 * 所以加一个页面只需要在 router/routes.ts 里加一条，不用碰这里。
 */
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const collapsed = defineModel<boolean>('collapsed', { default: false })

const menus = computed(() =>
  NAV_GROUPS.map((group) => ({
    ...group,
    items: router
      .getRoutes()
      .filter((r) => r.meta?.nav && r.meta?.group === (group.key as NavGroup))
      .map((r) => ({ path: r.path, title: r.meta.title as string, icon: r.meta.icon as string })),
  })).filter((group) => group.items.length),
)

// 下钻页（工作台/版本页）要让父级菜单保持高亮
const activeMenu = computed(() => (route.meta.activeMenu as string) || route.path)

function logout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="shell">
    <el-aside :width="collapsed ? '64px' : '208px'" class="aside">
      <div class="brand" :class="{ mini: collapsed }">
        <span class="dot" />
        <span v-show="!collapsed" class="name">DeepDocParse</span>
      </div>

      <el-menu :default-active="activeMenu" :collapse="collapsed" :collapse-transition="false"
               router class="menu">
        <template v-for="group in menus" :key="group.key">
          <div v-show="!collapsed" class="group-label">{{ group.label }}</div>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
            <template #title>{{ item.title }}</template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <el-button link @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </el-button>
        <span class="page-title">{{ route.meta.title }}</span>
        <div class="spacer" />
        <el-dropdown>
          <span class="user">
            <el-icon><component is="User" /></el-icon>
            {{ auth.username || '未登录' }}
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="router.push('/settings')">设置</el-dropdown-item>
              <el-dropdown-item divided @click="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="main">
        <RouterView />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.shell {
  height: 100vh;
}
.aside {
  border-right: 1px solid var(--el-border-color-light);
  transition: width 0.2s;
  overflow: hidden;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 56px;
  padding: 0 16px;
  font-weight: 600;
  font-size: 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.brand.mini {
  justify-content: center;
  padding: 0;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--el-color-primary);
  flex: none;
}
.menu {
  border-right: none;
}
.group-label {
  padding: 12px 16px 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.page-title {
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.user {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--el-text-color-regular);
}
.main {
  background: var(--el-bg-color-page);
  padding: 16px;
}
</style>
