import { ElButton, ElMessage } from 'element-plus'
import type { MessageHandler } from 'element-plus'
import { h } from 'vue'
import { createRouter, createWebHashHistory, isNavigationFailure } from 'vue-router'

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

// A running window can outlive its installed chunk files. Keep the failure
// visible without retrying imports or reloading until the user chooses to.
let loadNotice: MessageHandler | null = null
let navigationNotice: MessageHandler | null = null
router.onError((error) => {
  if (isNavigationFailure(error)) return
  if (/Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Loading (?:CSS )?chunk .+ failed|Unable to preload CSS|ChunkLoadError/i.test(error.message)) {
    if (loadNotice) return
    loadNotice = ElMessage({
      type: 'error',
      duration: 0,
      showClose: true,
      message: h('span', [
        '界面文件已变更或缺失，无法打开此页面。请重新加载窗口；若仍失败，请检查安装文件或网络连接。 ',
        h(ElButton, { size: 'small', onClick: () => location.reload() }, () => '重新加载窗口'),
      ]),
      onClose: () => { loadNotice = null },
    })
    return
  }
  console.error(error)
  if (navigationNotice) return
  navigationNotice = ElMessage.error({
    message: `页面打开失败：${error.message}`,
    duration: 0,
    showClose: true,
    onClose: () => { navigationNotice = null },
  })
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · DeepDocParse` : 'DeepDocParse'
})

export default router
