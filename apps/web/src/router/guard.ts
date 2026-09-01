import type { RouteLocationNormalized } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

/**
 * 全局前置守卫。**抽出来是为了能被测到。**
 *
 * 它原来直接写在 `router/index.ts` 的 `beforeEach` 回调里，
 * 而 `index.ts` 一 import 就会 `createWebHashHistory` 并建出真实 router，
 * 单测里不好用。于是测试曾经**复制了一份守卫逻辑**去测 ——
 * 那等于没测：验收实测把真守卫的 `redirect` 去掉，单测**照样 18 passed**，
 * 而「跳登录要带 redirect」正是 plan.md 首批必须覆盖的六条之一。
 * 抽成一个纯函数之后，`index.ts` 与用例引用的是**同一份**代码。
 */
export function authGuard(to: RouteLocationNormalized) {
  // 走 store 而不是直读 localStorage：登出/过期时状态只有一个来源
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isAuthenticated) return { name: 'documents' }
  return true
}
