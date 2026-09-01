import { defineConfig, devices } from '@playwright/test'

/**
 * E2E：驱动真实浏览器走真实路径。
 *
 * **为什么单测不够**：vue-tsc 抓不到「按钮点了没反应」「路由白屏」，
 * Vitest 挂的是 jsdom（没有真实布局、没有真实导航）。本项目已知的前端 bug
 * 全是这一类。这一层的核心断言是 **零 console error / 零未处理 promise rejection** ——
 * 它比任何具体断言都便宜，也最容易在下一个 commit 里退化。
 *
 * **它自己会构建并起一个静态服务**（见文件末尾的 webServer），不需要先开 dev server。
 * `npm run test` 里仍然**不含** e2e：那条一条龙要在没有浏览器的机器上也能跑绿。
 * e2e 单独用 `npm run test:e2e`。
 *
 * ⚠️ `reuseExistingServer` 会复用本地已经开着的 5173 —— 那多半是 dev server，
 * 于是这一轮又变回"对 dev server 跑"。**验稳定性时先把它关掉**。
 */
const PORT = Number(process.env.E2E_PORT || 5173)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  // dev server 冷启动时 vite 还在做依赖预构建，并行跑时首个用例常常
  // 撞上 30s 默认超时。放宽到 60s —— 这一层本来就慢，不是判据
  timeout: 60_000,
  forbidOnly: !!process.env.CI,
  // **必须给 worker 数封顶。** 不设的话 Playwright 按 CPU 核数开
  // （本机 16 线程 → 8 个 Chrome）。历史上这些 worker 打的是**单线程的
  // vite dev server**，on-demand transform 排不过来，报出来的是
  // ERR_CONNECTION_CLOSED / ERR_TIMED_OUT / goto 超时，看起来像前端的 bug。
  // 阶段 8 把用例加到 65 条之后，即使 workers=4 也会把 dev server 打死
  // （实测 ERR_CONNECTION_REFUSED：进程直接没了），所以**改成对构建产物跑**，
  // 见下面的 webServer。静态服务扛得住，也顺带验了真正要发布的那份产物。
  // 注意加宽 console 允许列表挡不住这个 —— goto 超时根本不是 console 事件。
  workers: 4,
  // 本机也留一次重试：retry 成功会被报成 **flaky** 而不是 passed，
  // 不会把问题藏起来，但能让偶发的连接拆除竞态不至于打断整轮
  retries: process.env.CI ? 2 : 1,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    // vite 只监听 [::1]，用 localhost 而不是 127.0.0.1（工作区 CLAUDE.md 陷阱 #11）
    baseURL: process.env.E2E_BASE_URL || `http://localhost:${PORT}`,
    trace: 'on-first-retry',
  },
  projects: [{
    name: 'chromium',
    use: {
      ...devices['Desktop Chrome'],
      // **国内网络下 `npx playwright install chromium` 经常拉不下来**
      // （cdn.playwright.dev 会中途断连，工作区 CLAUDE.md 陷阱 #5 是同一回事）。
      // 两条出路，按顺序试：
      //   1. 走镜像：PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
      //   2. 直接用机器上已装的 Chrome：E2E_CHANNEL=chrome npm run test:e2e
      // 留成 env 而不是写死 channel：CI 上应该用 Playwright 自带的那份
      // （版本固定，才谈得上可复现），本机开发怎么方便怎么来。
      channel: process.env.E2E_CHANNEL || undefined,
    },
  }],
  // **对构建产物跑，不对 dev server 跑。**
  // 理由有两条：① dev server 是单线程的 on-demand transform，65 条用例并行
  // 会把它压死（阶段 8 实测直接 ERR_CONNECTION_REFUSED）；
  // ② CI 与生产发的都是 `dist/`，对它跑才验到真正要发布的东西。
  //
  // **构建放在 `test:e2e` 脚本里，不放这条命令里。** 写成
  // `npm run build && npm run preview` 的话，Playwright 收尾时杀掉的是那层 shell，
  // 孙进程 `vite preview` 活下来继续占着 5173 —— 下一轮 `reuseExistingServer`
  // 直接复用它，于是**测的是上一次的 dist**（实测发生过，进程留了两分多钟）。
  // 这里只起一个能被干净杀掉的进程。
  webServer: {
    command: `npx vite preview --host 127.0.0.1 --port ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // **不能留 pipe。** vite 8 会把浏览器里的 console.warn 转发到终端，
    // 而 Vue 的告警带整棵组件树 —— 一条告警就是几百 KB JSON，
    // 真正的失败信息会被冲得无影无踪（实测单次输出 3.4MB）。
    // 要看前端日志就单独开 `npm run dev`。
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
