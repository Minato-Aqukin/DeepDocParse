import type { RouteRecordRaw } from 'vue-router'

import type { NavGroup } from '@/constants/nav'

declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题，同时用作菜单文字与浏览器标题 */
    title?: string
    /** Element Plus 图标组件名 */
    icon?: string
    /** 侧边栏分组；不填则不进侧边栏 */
    group?: NavGroup
    /** 是否出现在菜单里（详情页、版本页这类下钻页填 false） */
    nav?: boolean
    /** 免登录 */
    public?: boolean
    /** 下钻页回到哪个菜单项高亮 */
    activeMenu?: string
  }
}

/**
 * 路由表。**新增页面只要在这里加一条**：带上 meta 就会自动出现在侧边栏对应分组里，
 * 不需要改 AppShell，也不需要改导航配置。
 */
export const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/documents' },

  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true, nav: false },
  },

  {
    path: '/documents',
    name: 'documents',
    component: () => import('@/views/DocumentsView.vue'),
    meta: { title: '文档库', icon: 'Files', group: 'workspace', nav: true },
  },
  {
    path: '/documents/:id',
    name: 'workbench',
    component: () => import('@/views/WorkbenchView.vue'),
    meta: { title: '工作台', group: 'workspace', nav: false, activeMenu: '/documents' },
  },
  {
    path: '/documents/:id/versions',
    name: 'versions',
    component: () => import('@/views/VersionsView.vue'),
    meta: { title: '解析版本', group: 'workspace', nav: false, activeMenu: '/documents' },
  },
  {
    path: '/extractions',
    name: 'extractions',
    component: () => import('@/views/ExtractionsView.vue'),
    meta: { title: '结构化抽取', icon: 'Grid', group: 'workspace', nav: true },
  },
  {
    path: '/search',
    name: 'search',
    component: () => import('@/views/SearchView.vue'),
    meta: { title: '全文检索', icon: 'Search', group: 'workspace', nav: true },
  },
  {
    path: '/wiki',
    name: 'wiki',
    component: () => import('@/views/WikiView.vue'),
    meta: { title: '知识 Wiki', icon: 'Notebook', group: 'workspace', nav: true },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('@/views/GraphView.vue'),
    meta: { title: '实体图谱', icon: 'Share', group: 'workspace', nav: true },
  },

  {
    path: '/members',
    name: 'members',
    component: () => import('@/views/MembersView.vue'),
    meta: { title: '成员与角色', icon: 'User', group: 'account', nav: true },
  },
  {
    path: '/keys',
    name: 'keys',
    component: () => import('@/views/KeysView.vue'),
    meta: { title: 'API Key', icon: 'Key', group: 'developer', nav: true },
  },
  {
    path: '/usage',
    name: 'usage',
    component: () => import('@/views/UsageView.vue'),
    meta: { title: '用量', icon: 'DataLine', group: 'developer', nav: true },
  },

  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置', icon: 'Setting', group: 'account', nav: true },
  },

  // M5/M6 的旧路径：收藏夹里的链接还能用
  { path: '/dashboard', redirect: '/documents' },
  { path: '/task/:id', redirect: (to) => `/documents/${to.params.id}` },
  { path: '/:pathMatch(.*)*', redirect: '/documents' },
]
