<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'

import { isDark, toggleTheme } from '@/composables/useTheme'
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
        <el-tooltip :content="isDark ? '切到浅色' : '切到深色'" placement="bottom">
          <el-button link :aria-label="isDark ? '切到浅色' : '切到深色'" @click="toggleTheme()">
            <el-icon><component :is="isDark ? 'Sunny' : 'Moon'" /></el-icon>
          </el-button>
        </el-tooltip>
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

      <!--
        MinerU 归属声明。**不要删。**
        MinerU 在 Apache-2.0 之上有附加条款：§2 要求基于它提供在线服务的产品，
        必须在产品界面或公开文档里清晰显著地标明使用了 MinerU；§3 规定违反即
        自动终止许可，无需通知。本项目正是 §2 说的那种在线服务。
        另一处在 README 顶部，法律文本在 NOTICE。
      -->
      <el-footer class="attribution" height="auto">
        文档解析由
        <a href="https://github.com/opendatalab/MinerU" target="_blank" rel="noopener">MinerU</a>
        提供支持
      </el-footer>
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
/* 品牌位是墨色方块，不是主题色圆点 —— 准则一：彩色只留给出处与出错 */
.dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  background: var(--ddp-ink);
  flex: none;
}
.menu {
  border-right: none;
  /* el-menu 自带 --el-bg-color（= panel），而侧栏本身继承页底（ground），
     不抹掉的话导航区中间会出现一道色差缝 */
  background: transparent;
}
/* 选中态：浅底 + 左侧 2px 墨色竖条。Element Plus 默认是蓝字蓝底，
   那是第二种"重音色"，会和出处的红抢注意力（准则一、准则二）。 */
.menu :deep(.el-menu-item.is-active) {
  background: color-mix(in srgb, var(--ddp-ink) 7%, transparent);
  color: var(--ddp-ink);
  font-weight: 500;
  border-left: 2px solid var(--ddp-ink);
  padding-left: calc(var(--el-menu-base-level-padding, 20px) - 2px);
}
.menu :deep(.el-menu-item:not(.is-active):hover) {
  background: color-mix(in srgb, var(--ddp-ink) 5%, transparent);
}
.group-label {
  padding: 12px 16px 4px;
  font-size: 12px;
  color: var(--ddp-ink-3);
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
.attribution {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px 16px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-bg-color-page);
  border-top: 1px solid var(--el-border-color-lighter);
}
.attribution a {
  color: var(--el-color-primary);
  text-decoration: none;
}
</style>
