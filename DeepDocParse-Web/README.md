# DeepDocParse-Web

[English](README.en.md) · Apache-2.0 · **文档解析由 [MinerU](https://github.com/opendatalab/MinerU) 提供支持**

产品层：前端 + 后端 monorepo。总体架构见 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

本层是「可验证出处」这个定位真正落到用户眼前的地方：回答的每条依据都带页码 + bbox +
原件裁出来的区域截图，并且**任何降级都必须打标**（检索零命中 / 向量化不可用 / 视觉模型不可用 /
不能裁剪 / 解析可疑）。定位与「明确不做」清单见 [../DeepDocParse/README.md](../DeepDocParse/README.md)。

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

## 快速开始（无 GPU 也能跑）

```bash
cp .env.example .env          # SERVICE_TOKEN 必须与 DeepDocParse/.env 一致

# 1. 有状态组件（PostgreSQL 15432 / MinIO 19000、控制台 19001）
cd docker && docker compose -f compose.web.yml --env-file ../.env up -d

# 2. backend（8080）
cd backend && ../.venv/bin/alembic upgrade head
../.venv/bin/python -m uvicorn app.main:app --port 8080 --reload

# 3. frontend（5173，已配好到 8080 的代理）
cd frontend && npm install && npm run dev
```

service 侧用 [../DeepDocParse/README.md](../DeepDocParse/README.md) 的
**CPU 快速开始**（`compose.cpu.yml`）即可，不需要显卡：解析走 born-digital 引擎，
问答会带 `degraded="vision_unavailable"` 标记 —— 那是设计好的降级路径，不是故障。

**唯一还需要你自己准备的是一个 OpenAI 兼容的 chat 端点**（本层只要求协议兼容，
不绑定 DeepDocParse 的部署形态，见 ADR #17）。本地 llama.cpp / Ollama / 任意托管 API 都行：

```bash
# .env
CHAT_URL=http://127.0.0.1:11434/v1/chat/completions
CHAT_MODEL=qwen3:8b
```

> Windows 上把 `.venv/bin/` 换成 `.venv/Scripts/`。

## 配置

全部 45 项配置见 [docs/CONFIG.md](docs/CONFIG.md)（由 `scripts/gen_config_docs.py`
从 `backend/app/config.py` 生成，CI 会检查它有没有过期）。

## 验证

```bash
cd backend && ../.venv/bin/python -m pytest          # 单测：SQLite in-memory + respx，不需要 PG/MinIO
cd frontend && npm run build                          # 含 vue-tsc 类型检查

# 真环境全链路（需要 service 与本层都在跑）
.venv/bin/python scripts/e2e_web.py

# 显存装不下 mineru + TEI + VQA 同时开时分两段跑（两段用同一个账号）：
.venv/bin/python scripts/e2e_web.py --phase parse --user e2e_split   # 需 mineru + TEI
#   ...停掉 mineru、起 VQA（deepseek-ocr-server --device cpu --port 18001）...
.venv/bin/python scripts/e2e_web.py --phase qa --user e2e_split      # 需 VQA + TEI
```

## 出处评测

「可验证出处」是这个项目的立身之本，所以它必须**被度量**，而不只是被声称。

```bash
python scripts/eval_citations.py --mode offline      # 不需要模型/服务，量定位链路本身
python scripts/eval_citations.py --mode live         # 全链路，四个指标全量
```

四个指标（页码命中率 / bbox 包含率 / 拒答正确率 / 降级标记准确率）按属性切片给分，
定义、数据集格式与当前结论见 [docs/EVAL.md](docs/EVAL.md)。
**不报综合分**：综合分只告诉你「变好了 3%」，切片才告诉你「双栏页的命中率只有 40%」。

## 许可

[Apache-2.0](LICENSE)。第三方组件与归属声明见 [NOTICE](NOTICE)。

**MinerU 归属（不可省略）**：文档解析由 [MinerU](https://github.com/opendatalab/MinerU) 提供。
MinerU 的附加条款 §2 要求基于它提供在线服务的产品在界面或公开文档中清晰显著地标明使用了
MinerU，§3 规定违反即自动终止许可。本层是面向用户的那一端，因此声明同时出现在
README 顶部与**每个页面的页脚**（`frontend/src/layouts/AppShell.vue`），请勿删除。

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
- [x] M7 可验证出处（plan.md 的 P0/A/C 组）：
  - [x] **P0 数据留存**：出处改用稳定定位键 `(parse_job_id, seq)`，重建索引后仍接得回原文；
        回答落 `model_meta`（模型 + 检索参数快照）；迁移 `0003` 双向可跑并回填老数据
  - [x] A1 出处评测集与评测脚本（[docs/EVAL.md](docs/EVAL.md) / `scripts/eval_citations.py`），
        真实版面 fixture 固化格式；**修掉 `e2e_web.py` 的恒真断言**（`check("已做视觉验证", True)`）
  - [x] A2 出处带相关度 + 低相关主动提醒。**显示的是余弦相似度而不是 RRF 融合分** ——
        后者只由名次决定，绝佳命中与勉强及格长得一模一样
  - [x] A3 分块边界只读叠加层（只读，不做人工编辑，理由见 `types/workbench.ts`）
  - [x] A4 `parse_mismatch` 降级：补上七种降级里唯一的洞「解析本身错了」。
        **真机验收待 GPU**，本机只到 mock 单测为止
  - [x] C1/C2 Apache-2.0 + MinerU 归属（README / NOTICE / 每个页面的页脚）
  - [x] C3 GitHub Actions：后端单测 + 迁移双向跑（真 PG）+ 前端类型检查
  - [x] C5 英文 README（[README.en.md](README.en.md)）
