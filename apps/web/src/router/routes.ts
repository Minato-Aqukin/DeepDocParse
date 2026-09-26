import type { RouteRecordRaw } from 'vue-router'

import type { NavGroup } from '@/constants/nav'
import { bootSource, getActiveSource } from '@/platform/desktop'
import type { SourceFeature } from '@/platform/desktop'

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
    /**
     * 内容能力：桌面下只显示当前源能力包含的路由（子集关系）。
     * 不填 = 不依赖内容能力（登录、数据源、本机页等）。
     */
    features?: SourceFeature[]
    /**
     * 只在某一端注册：'desktop' = 桌面专有页，'web' = 浏览器专有页。
     * 不填 = 两端都有。
     */
    platform?: 'desktop' | 'web'
  }
}

/**
 * 路由表。**新增页面只要在这里加一条**：带上 meta 就会自动出现在侧边栏对应分组里，
 * 不需要改 AppShell，也不需要改导航配置。
 */
export const routes: RouteRecordRaw[] = [
  // 桌面：有可用的当前源 → 内容首页；没有（首运/全部移除/登录过期）→ 数据源页。
  // 浏览器：资源库（原有行为，不变）。
  {
    path: '/',
    redirect: () => {
      if (!window.ddpDesktop) return '/resources'
      const active = getActiveSource() ?? bootSource.value
      return active?.state === 'ready' ? '/resources' : '/sources'
    },
  },
  {
    path: '/resources', name: 'resources',
    component: () => import('@/views/ResourcesView.vue'),
    meta: { title: '资源库', icon: 'Files', group: 'workspace', nav: true, features: ['resources'] },
  },

  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true, nav: false, platform: 'web' },
  },

  {
    path: '/documents',
    name: 'documents',
    component: () => import('@/views/DocumentsView.vue'),
    meta: { title: '文档库', icon: 'Files', group: 'workspace', nav: true, features: ['documents'] },
  },
  {
    path: '/documents/:id',
    name: 'workbench',
    component: () => import('@/views/WorkbenchView.vue'),
    meta: { title: '工作台', group: 'workspace', nav: false, activeMenu: '/documents', features: ['documents'] },
  },
  {
    path: '/documents/:id/versions',
    name: 'versions',
    component: () => import('@/views/VersionsView.vue'),
    meta: { title: '解析版本', group: 'workspace', nav: false, activeMenu: '/documents', features: ['documents'] },
  },
  {
    path: '/extractions',
    name: 'extractions',
    component: () => import('@/views/ExtractionsView.vue'),
    meta: { title: '结构化抽取', icon: 'Grid', group: 'workspace', nav: true, platform: 'web' },
  },
  {
    path: '/search',
    name: 'search',
    component: () => import('@/views/SearchView.vue'),
    meta: { title: '全文检索', icon: 'Search', group: 'workspace', nav: true, features: ['search'] },
  },
  {
    path: '/tasks',
    name: 'federation-tasks',
    component: () => import('@/views/TasksView.vue'),
    meta: { title: '联邦任务', icon: 'Connection', group: 'workspace', nav: true, features: ['federation_tasks'] },
  },
  {
    path: '/tasks/new',
    name: 'federation-task-new',
    component: () => import('@/views/TaskPrepareView.vue'),
    meta: { title: '发起联邦任务', group: 'workspace', nav: false, activeMenu: '/tasks', features: ['federation_tasks'] },
  },
  {
    path: '/tasks/:rootTaskId',
    name: 'federation-task',
    component: () => import('@/views/TaskDetailRouteView.vue'),
    meta: { title: '联邦任务', group: 'workspace', nav: false, activeMenu: '/tasks', features: ['federation_tasks'] },
  },
  {
    path: '/tasks/local/:planId',
    name: 'federation-task-local',
    component: () => import('@/views/TaskDetailRouteView.vue'),
    meta: { title: '联邦任务', group: 'workspace', nav: false, activeMenu: '/tasks', features: ['federation_tasks'] },
  },
  {
    path: '/wiki',
    name: 'wiki',
    component: () => import('@/views/WikiView.vue'),
    meta: { title: '知识 Wiki', icon: 'Notebook', group: 'workspace', nav: true, features: ['wiki'] },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('@/views/GraphView.vue'),
    meta: { title: '实体图谱', icon: 'Share', group: 'workspace', nav: true, platform: 'web' },
  },

  {
    path: '/members',
    name: 'members',
    component: () => import('@/views/MembersView.vue'),
    meta: { title: '成员与角色', icon: 'User', group: 'account', nav: true, platform: 'web' },
  },
  {
    path: '/keys',
    name: 'keys',
    component: () => import('@/views/KeysView.vue'),
    meta: { title: 'API Key', icon: 'Key', group: 'developer', nav: true, platform: 'web' },
  },
  {
    path: '/usage',
    name: 'usage',
    component: () => import('@/views/UsageView.vue'),
    meta: { title: '用量', icon: 'DataLine', group: 'developer', nav: true, platform: 'web' },
  },

  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置', icon: 'Setting', group: 'account', nav: true, platform: 'web' },
  },

  // ---- 桌面专有页（浏览器里不注册，见 router/index.ts） ----
  {
    path: '/sources',
    name: 'sources',
    component: () => import('@/views/SourcesView.vue'),
    meta: { title: '数据源', icon: 'Connection', group: 'local', nav: true, platform: 'desktop' },
  },
  {
    path: '/models',
    name: 'local-models',
    component: () => import('@/views/LocalModelsView.vue'),
    meta: { title: '本地模型', icon: 'Cpu', group: 'local', nav: true, platform: 'desktop' },
  },
  {
    path: '/desktop-settings',
    name: 'desktop-settings',
    component: () => import('@/views/DesktopSettingsView.vue'),
    meta: { title: '设置', icon: 'Setting', group: 'local', nav: true, platform: 'desktop' },
  },
  {
    path: '/updates',
    name: 'updates',
    component: () => import('@/views/UpdatesView.vue'),
    meta: { title: '更新', icon: 'Refresh', group: 'local', nav: true, platform: 'desktop' },
  },

  // M5/M6 的旧路径：收藏夹里的链接还能用
  { path: '/dashboard', redirect: '/documents' },
  { path: '/task/:id', redirect: (to) => `/documents/${to.params.id}` },
  { path: '/:pathMatch(.*)*', redirect: '/documents' },
]
