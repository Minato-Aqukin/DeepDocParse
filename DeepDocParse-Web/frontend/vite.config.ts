import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// dev 下前端 5173，四个前缀都代理到后端：
//   /api    前端自用接口（JWT）
//   /files  稳定文件 URL（原件预览，token 即凭证）
//   /v1 /mcp 对外 API（前端不用，留着方便在浏览器里试 key）
//
// 后端地址用 VITE_API_TARGET 覆盖：Windows 的保留端口段会漂移，8080 有时会落进去
// （netsh interface ipv4 show excludedportrange protocol=tcp 可查），后端只能换端口起。
const target = process.env.VITE_API_TARGET || 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [vue(), vueDevTools()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/api', '/files', '/v1', '/mcp'].map((p) => [p, { target, changeOrigin: true }]),
    ),
  },
})
