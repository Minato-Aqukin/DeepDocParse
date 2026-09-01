import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'
import vueDevTools from 'vite-plugin-vue-devtools'

// dev 下前端 5173，四个前缀都代理到后端：
//   /api    前端自用接口（JWT）
//   /files  稳定文件 URL（原件预览，token 即凭证）
//   /v1 /mcp 对外 API（前端不用，留着方便在浏览器里试 key）
//
// 后端地址用 VITE_API_TARGET 覆盖（环境变量或 .env.local 都行）：
// Windows 的保留端口段会漂移，8080 有时会落进去导致后端只能换端口起
// （netsh interface ipv4 show excludedportrange protocol=tcp 可查）。
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), 'VITE_'), ...process.env }
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8080'

  return {
    plugins: [vue(), vueDevTools()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
        // 契约生成物。走别名而不是 npm workspace 依赖：生成物本来就在仓库里，
        // 别名让 vite / vitest / vue-tsc 三处指向同一个文件，不必先跑 npm install。
        // 将来要发 npm 包时把别名换成真依赖即可，import 语句一个字不用改。
        '@deepdocparse/contracts': fileURLToPath(
          new URL('../../packages/contracts/generated/ts/enums.ts', import.meta.url),
        ),
      },
    },
    server: {
      port: 5173,
      proxy: Object.fromEntries(
        ['/api', '/files', '/v1', '/mcp'].map((p) => [p, { target, changeOrigin: true }]),
      ),
    },
  }
})
