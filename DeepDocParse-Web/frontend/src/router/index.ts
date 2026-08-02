import { createRouter, createWebHashHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

import { routes } from './routes'

const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes,
})

router.beforeEach((to) => {
  // 走 store 而不是直读 localStorage：登出/过期时状态只有一个来源
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isAuthenticated) return { name: 'documents' }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · DeepDocParse` : 'DeepDocParse'
})

export default router
