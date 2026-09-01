// 顺序不能动：Element Plus 的深色变量定义在 html.dark（特异性比 :root 高），
// ddp-element-plus.css 只有排在它之后才压得住。
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'katex/dist/katex.min.css'
import '@/assets/ddp/ddp.css'
import './assets/main.css'

import * as ElementPlusIcons from '@element-plus/icons-vue'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 全局注册图标：路由 meta 里写图标名（如 'Files'）就能直接 <component :is="name" /> 用上，
// 加页面时不用再手动 import 图标组件
for (const [name, component] of Object.entries(ElementPlusIcons)) {
  app.component(name, component)
}

app.mount('#app')
