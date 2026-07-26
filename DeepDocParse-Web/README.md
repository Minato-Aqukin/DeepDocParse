# DeepDocParse-Web

产品层：前端 + 后端 monorepo。总体架构见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

```
DeepDocParse-Web/
├── backend/    # FastAPI：用户、API key、额度限流、任务、永久归档、对外 API 与 MCP 代理
├── frontend/   # Vue 3 + TS + Element Plus：上传、任务列表、结果预览、key 管理、用量
├── docker/     # compose.web.yml：PostgreSQL + MinIO（有状态组件）
└── scripts/    # e2e_web.py 真环境全链路验证
```

对 DeepDocParse 的调用**只依赖** [../DeepDocParse/openapi.yaml](../DeepDocParse/openapi.yaml) 契约。

## 三类调用方，三套凭据

| 调用方 | 凭据 | 入口 |
|---|---|---|
| Web 用户 | JWT（登录换取） | `/api/*` |
| 第三方开发者 | API key `sk-xxx`（哈希入库，明文只返回一次） | `/v1/*`、`/mcp` |
| DeepDocParse（service） | 内网 `SERVICE_TOKEN` | `/internal/parse-callback` |
| 谁都行（token 即凭证） | 一次性随机 token | `/files/{token}` 原件下载 |

service 永远看不到用户凭据：本层验完 key/JWT 后统一换成 `SERVICE_TOKEN` 转发。

## 关键链路

**上传 → 归档**：算文件内容 sha256 作 `doc_id` → 存 MinIO → 生成稳定文件 URL →
`POST service /v1/parse{file_url, doc_id, callback_url}` → 回调或对账触发归档 →
结果里的 data URI 图片解码落盘、markdown 引用重写 → 按页计量。

**为什么要对账**：gateway 的完成回调是尽力而为的（失败只记日志），backend 恰好在重启时
就会永久丢结果（service 只暂存 24h）。`app/reconcile.py` 启动即跑一次、之后每 60s 扫一遍
未落终态的任务，这是本层可靠性的底线。

**为什么用稳定文件 URL 而不是预签名**：预签名会过期，签名还绑 host（给 service 的和给浏览器的
签名不同）；更要命的是 MCP 平面的 `ask_document` 只拿得到一个裸 URL，URL 每次变化会让
service 侧的向量索引永远命中不到。详见 ADR #12（文档身份本身见 ADR #11）。

## 快速开始（dev）

```bash
cp .env.example .env          # SERVICE_TOKEN 必须与 DeepDocParse/.env 一致

# 1. 有状态组件（PostgreSQL 15432 / MinIO 19000、控制台 19001）
cd docker && docker compose -f compose.web.yml --env-file ../.env up -d

# 2. backend（8080）
cd backend && ../.venv/Scripts/alembic upgrade head
../.venv/Scripts/python -m uvicorn app.main:app --port 8080 --reload

# 3. frontend（5173，已配好到 8080 的代理）
cd frontend && npm install && npm run dev
```

service 侧（gateway 9000 / mcp_server 9100）按 [../CLAUDE.md](../CLAUDE.md)「宿主机混合模式」启动。

## 验证

```bash
cd backend && ../.venv/Scripts/python -m pytest      # 单测：SQLite in-memory + respx，不需要 PG/MinIO
cd frontend && npm run build                          # 含 vue-tsc 类型检查

# 真环境全链路（需要 service 与本层都在跑）
REDIS_URL=redis://localhost:6379/0 .venv/Scripts/python scripts/e2e_web.py
```

## 里程碑

- [x] M5 产品层全栈：用户/key/额度计量、上传归档链路、对账兜底、对外 `/v1/*` 与 `/mcp` 代理、五个前端页面
- [ ] 多副本部署：限速计数换 Redis（现为进程内滑窗，见 `app/metering.py` 的 TODO(prod)）
- [ ] 对象存储生命周期：删除任务后回收 MinIO 中的文件
