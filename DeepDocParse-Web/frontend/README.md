# frontend

Vue 3 前端。**请用官方脚手架初始化，不要手写工程配置：**

```bash
npm create vue@latest .   # 选: TypeScript / Router / Pinia
npm i axios
npm run dev
```

## 页面规划

| 路由 | 页面 | 要点 |
|------|------|------|
| /login | 登录/注册 | JWT 存储与刷新 |
| /dashboard | 任务列表 + 上传 | 拖拽上传、解析进度轮询 |
| /task/:id | 结果预览 | Markdown 渲染（表格/公式）、原文对照、下载 md/json/图片 |
| /keys | API key 管理 | 创建（明文只显示一次）、吊销、用量图表 |

API 基地址指向 backend（dev: http://localhost:8080）。
