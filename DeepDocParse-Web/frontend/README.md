# DeepDocParse-Web / frontend

Vue 3 + TypeScript + Router + Pinia + Element Plus。用 `npm create vue@latest` 生成，未手写工程配置。

```bash
npm install
npm run dev          # 5173，/api /files /v1 /mcp 已代理到 http://127.0.0.1:8080
npm run build        # 含 vue-tsc 类型检查
```

## 页面

| 路由 | 内容 |
|---|---|
| `/login` | 登录 / 注册（开放注册，无邮箱验证） |
| `/documents` | 概览卡片 + 筛选（文件名/解析状态/问答状态）+ 分页 + 批量选择；行内可下载/重解析/重建索引/删除 |
| `/documents/:id` | 三栏工作台：pdf.js 原文 + bbox 高亮 ／ 按页结果 ／ 问答面板 |
| `/documents/:id/versions` | 解析版本对比、换参数重解析、设为当前 |
| `/search` | 跨文档检索，命中带页码可直达 |
| `/keys` | API key 签发（明文只显示一次）、额度/限速/有效期、吊销 |
| `/usage` | 用量卡片网格：统计卡 + 每日趋势图 + 按平面汇总 + 按天明细 |
| `/settings` | 账号信息（`/api/auth/me`）+ 本机默认解析参数 |

## 架构约定（加功能前先看这里）

- **加页面**：只在 `router/routes.ts` 加一条并写 `meta`（`title/icon/group/nav`），
  侧边栏会自动出现该项 —— `layouts/AppShell.vue` 从路由派生菜单，不要去改导航代码
- **加接口**：在 `api/` 下按域加模块并挂到 `api/index.ts`；类型放 `types/api.ts`
- **加状态值**：解析/索引状态、问答降级说明统一在 `constants/status.ts`，
  改一处全站生效（标签、筛选下拉、气泡文案都由它派生）
- **加解析引擎或参数**：只改 `constants/engines.ts` 的 schema，
  上传对话框与重解析共用 `components/engine/EngineOptionsForm.vue`，两处同时生效
- **轮询**：用 `composables/usePolling.ts`，不要再手写 `setInterval` + 清理

## 两个必须知道的约束

1. **渲染结果必须过 DOMPurify**（`src/utils/markdown.ts`）。markdown 来自被解析的文档，
   是不可信输入；`markdown-it` 已关 `html`，再 sanitize 一遍是第二道闸。
2. **图片走 blob 而不是直接 `<img src>`**。归档后的 markdown 里图片指向
   `/api/tasks/{id}/images/{name}`，该端点受 JWT 保护，而 `<img>` 发不出 Authorization 头 ——
   `resolveAuthedImages()` 渲染后按需取回并换成 object URL。好处是归档产物里不埋任何凭证。

## 图表

`src/components/BarChart.vue` 是手写 SVG，不引图表库。页数与请求数量纲不同，
**刻意分成两张单序列图**（小倍数），不做双 Y 轴；单序列不需要图例，标题即身份。
