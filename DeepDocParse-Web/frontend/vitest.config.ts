import { fileURLToPath } from 'node:url'

import { configDefaults, defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config'

// 组件级测试。**为什么需要它**：在这之前前端的质量保障只有 vue-tsc 类型检查，
// `src/` 下零测试文件 —— 而本项目前端已知的真 bug（轮询活过组件卸载、
// 卸载后新建的 blob URL 永不回收）**恰好全是类型检查抓不到的那一类**。
//
// 复用 vite.config 而不是另写一套：alias `@`、vue 插件、env 处理必须与真实构建一致，
// 分两套迟早会出现"测试里能过、构建时报错"。
export default mergeConfig(
  viteConfig({ mode: 'test', command: 'serve' }),
  defineConfig({
    test: {
      // 组件要挂到真实 DOM 上才能验"卸载之后还会不会动"
      environment: 'jsdom',
      environmentOptions: {
        // **url 不能省。** 不给的话 jsdom 用不透明源（opaque origin），
        // 而不透明源下 `window.localStorage` 是 undefined —— 于是
        // `stores/auth.ts` 在 setup 里读 token 就直接抛，**每个碰到 auth 的
        // 用例都会以一个跟 auth 毫无关系的 TypeError 挂掉**，看起来像 store 坏了。
        jsdom: { url: 'http://localhost:5173/' },
      },
      // 约定：测试与被测代码同目录的 __tests__ 下（tsconfig.app.json 已经把它排除在构建之外）
      include: ['src/**/__tests__/**/*.spec.ts'],
      exclude: [...configDefaults.exclude, 'e2e/**'],
      root: fileURLToPath(new URL('./', import.meta.url)),
      setupFiles: ['./src/__tests__/setup.ts'],
      // 每个用例之间清干净 mock 的调用记录与实现。
      // （"自动 unmount"那件事在 setup.ts 里用 enableAutoUnmount 做，不在这儿）
      restoreMocks: true,
      clearMocks: true,
    },
  }),
)
