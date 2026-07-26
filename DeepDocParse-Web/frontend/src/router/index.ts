import { createRouter, createWebHashHistory } from 'vue-router'

import { TOKEN_KEY } from '@/api/client'

const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    { path: '/dashboard', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
    { path: '/task/:id', name: 'task', component: () => import('@/views/TaskDetailView.vue') },
    { path: '/keys', name: 'keys', component: () => import('@/views/KeysView.vue') },
    { path: '/usage', name: 'usage', component: () => import('@/views/UsageView.vue') },
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
