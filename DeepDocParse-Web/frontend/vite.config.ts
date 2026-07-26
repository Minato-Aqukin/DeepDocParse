import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// dev 下前端 5173、后端 8080，四个前缀都代理过去：
//   /api    前端自用接口（JWT）
//   /files  稳定文件 URL（原件预览，token 即凭证）
//   /v1 /mcp 对外 API（前端不用，留着方便在浏览器里试 key）
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
      ['/api', '/files', '/v1', '/mcp'].map((p) => [
        p,
        { target: 'http://127.0.0.1:8080', changeOrigin: true },
      ]),
    ),
  },
})
