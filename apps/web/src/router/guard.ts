import type { RouteLocationNormalized } from 'vue-router'

import { bootSource, getActiveSource, goDesktopSources, isDesktop } from '@/platform/desktop'
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
 *
 * 桌面补充（DESKTOP-APPSHELL-PLAN wave 1）：
 * - 平台过滤：`platform: 'web'` 的路由在桌面不可达（回退到根，让根重定向决定去向）；
 *   `platform: 'desktop'` 的路由在浏览器不可达（回退到资源库）。
 * - 能力过滤：`features` 与当前源能力不是子集关系 → 去数据源页并带上原因
 *   `not_supported_locally`（本机源能力由本地运行时声明，中心源固定只读子集）。
 * - 未认证（桌面 = 当前源不可用）：去数据源页（首运落点），绝不去 Web 登录页。
 * - 已认证访问 `/sources` 以外的桌面页；`/login` 在桌面不存在（platform 过滤已处理）。
 */
export async function authGuard(to: RouteLocationNormalized) {
  if (isDesktop()) return desktopGuard(to)
  // 走 store 而不是直读 localStorage：登出/过期时状态只有一个来源
  const auth = useAuthStore()
  if (to.meta.platform === 'desktop') return { name: 'resources' }
  if (!to.meta.public && auth.isAuthenticated && !auth.profile) {
    try {
      await auth.fetchProfile()
    } catch {
      // 401 清除会话；暂时不可用则保留会话，由外壳明确提示且不给写权限。
    }
  }
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isAuthenticated) return { name: 'documents' }
  return true
}

function desktopGuard(to: RouteLocationNormalized) {
  const auth = useAuthStore()
  if (to.meta.platform === 'web') return '/'
  const source = getActiveSource() ?? bootSource.value
  const ready = source?.state === 'ready'
  const authed = auth.isAuthenticated
  if (!to.meta.public && ready && authed && !auth.profile) {
    // profile 经代理从 /api/auth/me 拿：失败只标不可用（401 由拦截器送去数据源页）。
    void auth.fetchProfile().catch(() => undefined)
  }
  if (!ready || !authed) {
    // 无源/未就绪：首屏落在数据源页；已在数据源页则放行（避免重定向循环）。
    if (to.name === 'sources') return true
    return { name: 'sources', query: to.meta.public ? {} : { reason: signedOutOrEmpty(source?.state) } }
  }
  const missing = missingFeatures(to.meta.features, source?.features ?? [])
  if (missing.length) {
    return { name: 'sources', query: { reason: 'not_supported_locally' } }
  }
  return true
}

function signedOutOrEmpty(state: string | undefined): string {
  return state === 'signed_out' ? 'source_signed_out' : 'no_active_source'
}

/** 当前源缺哪些能力（子集检查）。导出供单测与 AppShell 导航过滤共用。 */
export function missingFeatures(
  required: readonly string[] | undefined,
  available: readonly string[],
): string[] {
  if (!required || !required.length) return []
  return required.filter((f) => !available.includes(f))
}
