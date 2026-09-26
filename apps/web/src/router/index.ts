import { createRouter, createWebHashHistory } from 'vue-router'

import { isDesktop } from '@/platform/desktop'
import { authGuard } from './guard'
import { routes } from './routes'

// 桌面不注册浏览器专有页（login / members / keys / usage / web 设置 / 抽取 / 图谱），
// 浏览器不注册桌面专有页（sources / models / desktop-settings / updates）。
const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: routes.filter((r) => {
    const platform = r.meta?.platform
    if (!platform) return true
    return isDesktop() ? platform === 'desktop' : platform === 'web'
  }),
})

// 守卫本身在 ./guard.ts —— 抽出来是为了让单测引用**同一份**代码
// 而不是复制一份去测（那样改了真守卫测试也不会红，详见那个文件的注释）
router.beforeEach(authGuard)

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · DeepDocParse` : 'DeepDocParse'
})

export default router
