# DeepDocParse-Web

前端 + 后端 monorepo。总体架构见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

```
DeepDocParse-Web/
├── backend/    # FastAPI：用户、API key、额度限流、任务、永久归档、MCP 代理
└── frontend/   # Vue 3：上传、任务列表、结果预览、key 管理
```

## backend 职责边界

- **持有用户与 key**：注册/登录、API key 签发/吊销、额度与速率限制 —— service 不感知用户
- **对外 API 入口**：第三方开发者的请求在此验 key，再以 SERVICE_TOKEN 转发 DeepDocParse
- **永久存储**：MinIO 存原始文件与解析结果（service 只暂存 24h，本层负责取回归档），
  支持历史记录与重新下载
- **MCP 代理**（方案 A）：`/mcp` 路径验 key 后反代 DeepDocParse 的 mcp-server

## backend 模块规划（对应 app/ 目录）

| 模块 | 内容 | 依赖 |
|------|------|------|
| auth | 用户注册/登录（JWT session） | users 表 |
| apikeys | key 签发/吊销/额度（sk-xxx，哈希入库） | api_keys 表 |
| tasks | 上传 -> MinIO -> 调 service /v1/parse -> 状态同步 -> 归档 | DeepDocParse 契约 |
| proxy | 对外 API（验 key 版 /v1/*）与 /mcp 反代 | apikeys + DeepDocParse |

数据库：PostgreSQL（users / api_keys / tasks / usage_records）。
对 DeepDocParse 的调用**只依赖** `../DeepDocParse/openapi.yaml` 契约。

## frontend 初始化（建议用脚手架生成，不手写）

```bash
cd frontend
npm create vue@latest .   # 选 TypeScript + Router + Pinia
npm i axios
```

页面规划：
- `/login` 登录注册
- `/dashboard` 任务列表 + 上传（拖拽，进度）
- `/task/:id` 结果预览（Markdown 渲染 + 原文对照）、下载
- `/keys` API key 管理（创建/吊销/用量）

## 启动（M5 联调阶段完善）

```bash
cd backend && uvicorn app.main:app --reload   # http://localhost:8080
cd frontend && npm run dev                    # http://localhost:5173
```
