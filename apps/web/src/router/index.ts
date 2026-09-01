import { createRouter, createWebHashHistory } from 'vue-router'

import { authGuard } from './guard'
import { routes } from './routes'

const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes,
})

// 守卫本身在 ./guard.ts —— 抽出来是为了让单测引用**同一份**代码
// 而不是复制一份去测（那样改了真守卫测试也不会红，详见那个文件的注释）
router.beforeEach(authGuard)

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · DeepDocParse` : 'DeepDocParse'
})

export default router
