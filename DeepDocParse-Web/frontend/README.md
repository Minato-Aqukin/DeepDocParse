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
| `/dashboard` | 拖拽上传（多文件、进度）+ 任务表格（未落终态时才轮询） |
| `/task/:id` | 左原文 iframe（`/files/{token}`）／右 Markdown 渲染，可切源码，下载 .md / 版面 JSON / 原件 |
| `/keys` | API key 签发（明文只显示一次）、额度与限速、吊销 |
| `/usage` | 用量：两张单序列小倍数柱状图 + 按平面汇总 + 按天表格 |

## 两个必须知道的约束

1. **渲染结果必须过 DOMPurify**（`src/utils/markdown.ts`）。markdown 来自被解析的文档，
   是不可信输入；`markdown-it` 已关 `html`，再 sanitize 一遍是第二道闸。
2. **图片走 blob 而不是直接 `<img src>`**。归档后的 markdown 里图片指向
   `/api/tasks/{id}/images/{name}`，该端点受 JWT 保护，而 `<img>` 发不出 Authorization 头 ——
   `resolveAuthedImages()` 渲染后按需取回并换成 object URL。好处是归档产物里不埋任何凭证。

## 图表

`src/components/BarChart.vue` 是手写 SVG，不引图表库。页数与请求数量纲不同，
**刻意分成两张单序列图**（小倍数），不做双 Y 轴；单序列不需要图例，标题即身份。
