# DeepDocParse-Web

产品层：前端 + 后端 monorepo。总体架构见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

```
DeepDocParse-Web/
├── backend/    # FastAPI：用户、key、额度、归档、分块索引、文档问答、对外 API 与 MCP 代理
├── frontend/   # Vue 3 + TS + Element Plus：文档库、三栏工作台（原文/结果/问答）、检索、用量
├── docker/     # compose.web.yml：PostgreSQL(pgvector) + MinIO + Redis（多副本才需要）
├── docs/       # DESIGN.md：M6 设计（问答、Document/ParseJob 拆分、多副本）
└── scripts/    # e2e_web.py 真环境全链路验证
```

后端模块速览：`chunking` 分块 · `indexing` 索引管线 · `search` 混合检索（pgvector/内存两实现）
· `qa` 问答编排 · `crops` 出处裁剪 · `archive` 归档 · `reconcile` 对账 · `gc` 对象回收。

对 DeepDocParse 的调用**只依赖** [../DeepDocParse/openapi.yaml](../DeepDocParse/openapi.yaml) 契约。
下一轮的设计已定稿在 [docs/DESIGN.md](docs/DESIGN.md)——改动本层结构前先读它。

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

**文档问答（M6）**：归档完成后本层自己分块（读自家 `layout.json`，不碰 service）→ 向量化 →
存进 Postgres+pgvector。提问时混合检索（向量 + 关键词，RRF 融合）→ 按 bbox 裁出原文区域 →
多模态问答 → SSE 流式返回，答案带页码/bbox/截图三件套的出处。
**降级一律可见**：没检索到、视觉模型不可用、非 PDF 不能裁剪，都会在回答上打标。

**与 service 的耦合面**只有两处：解析契约（`openapi.yaml`）与 OpenAI 兼容的 embedding/chat
端点——后者可以配成任意兼容服务（`EMBEDDING_URL` / `CHAT_URL`），本层不绑定 DeepDocParse
的部署形态。检索索引、分块、问答编排全部在本层。

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
.venv/Scripts/python scripts/e2e_web.py

# dev 机内存装不下 mineru + TEI + VQA 同时开，分两段跑（两段用同一个账号）：
.venv/Scripts/python scripts/e2e_web.py --phase parse --user e2e_split   # 需 mineru + TEI
#   ...停掉 mineru、起 VQA（deepseek-ocr-server --device cpu --port 18001）...
.venv/Scripts/python scripts/e2e_web.py --phase qa --user e2e_split      # 需 VQA + TEI
```

## 里程碑

- [x] M5 产品层全栈：用户/key/额度计量、上传归档链路、对账兜底、对外 `/v1/*` 与 `/mcp` 代理、五个前端页面
- [x] M6 深化（设计见 [docs/DESIGN.md](docs/DESIGN.md)）：
  - [x] Document/ParseJob 拆分与迁移（`0002`）；换参数重解析、版本切换
  - [x] 本层分块 + pgvector 向量索引（service 零改动）
  - [x] 文档问答：SSE 流式、出处页码/bbox/截图、四种降级可见
  - [x] 三栏工作台（pdf.js 渲染 + bbox 联动）、跨文档检索、打包导出
  - [x] 多副本：Redis 令牌桶限速、对账选主、对象 GC、`/readyz`、`/metrics`
- [x] 真机 e2e 通过（分两段，见「验证」）：pgvector 真实检索、真实版面裁剪、
      带视觉验证的问答答案、迁移回填、对象回收都已在真环境验证
