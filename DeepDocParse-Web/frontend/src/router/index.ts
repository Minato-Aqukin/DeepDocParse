import { createRouter, createWebHashHistory } from 'vue-router'

import { TOKEN_KEY } from '@/api/client'

const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/documents' },
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    { path: '/documents', name: 'documents', component: () => import('@/views/DocumentsView.vue') },
    {
      path: '/documents/:id',
      name: 'workbench',
      component: () => import('@/views/WorkbenchView.vue'),
    },
    {
      path: '/documents/:id/versions',
      name: 'versions',
      component: () => import('@/views/VersionsView.vue'),
    },
    { path: '/search', name: 'search', component: () => import('@/views/SearchView.vue') },
    { path: '/keys', name: 'keys', component: () => import('@/views/KeysView.vue') },
    { path: '/usage', name: 'usage', component: () => import('@/views/UsageView.vue') },
    // M5 的旧路径：收藏夹里的链接还能用
    { path: '/dashboard', redirect: '/documents' },
    { path: '/task/:id', redirect: (to) => `/documents/${to.params.id}` },
  ],
})

// 未登录一律回登录页（token 只在 localStorage，刷新后仍然有效）
router.beforeEach((to) => {
  if (to.name !== 'login' && !localStorage.getItem(TOKEN_KEY)) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
