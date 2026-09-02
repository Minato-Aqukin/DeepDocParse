# 全量代码盘点与删除台账

> 依据：`MERGE-REFACTOR-PROPOSAL.md` §11.2
> 每个现有模块只能落入 **保留并重构 / 重写 / 删除** 三类之一。
> 删除项必须写清：删除理由、原调用方、替代实现。
> **无法证明无用的代码不得凭感觉删除。**

判定图例：**保留** = 移入目标模块并重构 · **重写** = 契约保留、实现重做、旧文件删除 ·
**删除** = 重复 / 不可达 / 旧部署专用 / 被替代 / 无调用方。

---

## 1. `DeepDocParse/gateway/ddp_core/` → `python/ddp_core/ddp_core/`

语料核心纯逻辑。**整包保留** —— 风险台账明写「Go 重写证据规则 → 假出处」，
evidence/citation 的唯一实现继续留在 Python。

| 模块 | 行 | 判定 | 说明 |
|---|---:|---|---|
| `models.py` | 572 | 保留并重构 | 加 `organization_id`/`workspace_id` 预留位与 schema 归属；`Base` 拆成 corpus 专用 |
| `search.py` | 319 | 保留 | `SearchIndex` 协议 + `PgVectorIndex`/`MemoryIndex`，单测不依赖 PG 的命门 |
| `extract_format.py` | 394 | 保留 | DDP-Extract v1 参考实现，改由契约包生成常量 |
| `chunking.py` | 187 | 保留 | 分块判据唯一实现，`seq` 是出处定位键，不得再抄一份 |
| `tokenize.py` | 136 | 保留 | jieba 软依赖 + 二元组兜底；`backend()` 必须继续进 `model_meta` |
| `crops.py` | 143 | 保留 | 含 2026-09-01 的 PDFium 串行化修复 |
| `compilation.py` | 130 | 保留 | DDP-Compile v1 |
| `agent.py` | 114 | 保留 | DDP-Agent |
| `rerank.py` | 111 | 保留 | `RerankConfig` 显式入参（不加第二个全局） |
| `knowledge.py` | 111 | 保留 | 图谱/wiki 纯逻辑 |
| `blocks.py` | 103 | 保留 | 块类型归一化规范实现，`block_text` 历史上被抄过四遍 |
| `anchor.py` | 62 | 保留 | 出处锚点 |
| `types.py` | 48 | 保留 | `Vector`（PG=pgvector，其它方言=JSON） |
| `hits.py` | 29 | 保留 | |
| `__init__.py` | 28 | 保留 | |

## 2. `DeepDocParse/gateway/app/` → `services/model-gateway/ddp_gateway/`

无状态注册表驱动的模型协议适配层。**整体保留**，包名 `app` → `ddp_gateway`。

| 模块 | 行 | 判定 | 说明 |
|---|---:|---|---|
| `services/extraction.py` | 542 | 保留 | 抽取编排（检索定位 → 抽值 → 视觉核对） |
| `services/vlm_ocr.py` | 431 | 保留 | 整页渲染 → VLM 识别引擎 |
| `worker/tasks.py` | 390 | **重写** | ARQ 链换成 PG 持久任务 + claim/lease/generation fencing（§10）；迁去 `services/corpus-worker` |
| `services/task_store.py` | 313 | 保留并重构 | Redis 24h 暂存仍是缓存；任务真相移到 PG |
| `services/dsocr2.py` | 249 | 保留 | chat template + BOS + `skip_special_tokens:false` 那一串坑都在这儿 |
| `services/borndigital.py` | 244 | 保留 | 进程内无 GPU 解析引擎 |
| `services/layout.py` | 206 | 保留 | DDP-Layout 归一化（只是再导出 `ddp_core.blocks`） |
| `services/engines.py` | 204 | 保留 | `runtime` → 适配器解析，铁律 3 的落点 |
| `config.py` | 197 | 保留并重构 | 21 项配置，改由契约包生成枚举 |
| `routers/parse.py` | 152 | 保留 | 缺省引擎走 `registry.default_of()`，不得写死 |
| `routers/extract.py` | 127 | 保留 | |
| `routers/chat.py` | 111 | 保留 | VQA 并发闸 |
| `services/retrieval.py` | 108 | 保留 | |
| `services/mineru_client.py` | 103 | 保留 | 官方 mineru 3.4.4 实测契约 |
| `routers/health.py` | 89 | 保留 | |
| `main.py` | 77 | 保留并重构 | |
| `routers/rerank.py` | 67 | 保留 | |
| `errors.py` | 52 | 保留 | OpenAI 风格错误体 |
| `routers/embeddings.py` | 48 | 保留 | |
| `services/vqa_client.py` | 33 | 保留 | |
| `auth.py` | 20 | 保留并重构 | service token 校验；生产改短期服务 token / mTLS（§14） |

## 3. `DeepDocParse-Web/backend/app/` —— 一分为二

### 3.1 账号与入口层 → **重写为 Go control-api**

旧文件已移入 `docs/refactor/_rewrite-ref-*.py` 作为**重写参照物**，
Go 侧落地后从工作树删除（§19.3：最终工作树不得存在未使用的旧代码）。

| 旧模块 | 行 | 判定 | 原调用方 | 替代实现 |
|---|---:|---|---|---|
| `routers/auth.py` | 68 | 重写 | 前端 `api/auth.ts`、`stores/auth.ts` | `services/control-api` `/api/auth/*`（+ OIDC） |
| `routers/apikeys.py` | 90 | 重写 | 前端 `api/keys.ts`、`KeysView.vue` | control-api `/api/keys/*`（加作用域、过期、最后使用时间） |
| `routers/usage.py` | 41 | 重写 | 前端 `api/usage.ts`、`UsageView.vue` | control-api `/api/usage`（usage_ledger） |
| `routers/proxy.py` | 309 | 重写 | 第三方 SDK 的 `/v1/*`、`/mcp` | control-api 统一入口 + SSE 字节级代理 |
| `routers/files.py` | 63 | 重写 | model-gateway 下载原件、前端预览 | control-api 签名下载：稳定 `/files/{token}` → 302 到短期对象 URL（§9.2） |
| `security.py` | 67 | 重写 | 上面四个 router | Go：JWT、bcrypt、API key sha256 |
| `metering.py` | 100 | 重写 | `proxy.py`、`documents.py`、`qa.py` | Go：配额、Redis 限速、usage_ledger |

> ⚠️ **`files.py` 的稳定 URL 语义是硬约束**：URL 一变，model-gateway 的
> 幂等与向量索引分块键全部失效（ADR #11/#12，踩过两次）。Go 实现必须
> 保持 `/files/{token}` 路径稳定，短期签名只出现在 302 的 Location 里。

### 3.2 语料层 → `services/corpus-api/ddp_corpus/`（保留并重构）

| 模块 | 行 | 判定 | 说明 |
|---|---:|---|---|
| `routers/documents.py` | 1014 | 保留并重构 | 上传改直传 finalize；`user_id` 换成 actor context |
| `routers/conversations.py` | 837 | 保留并重构 | 问答 + 证据 + 裁图 |
| `routers/extractions.py` | 602 | 保留并重构 | 抽取模板/批次/导出 |
| `extraction.py` | 479 | 保留 | 抽取编排 |
| `qa.py` | 383 | 保留 | 检索 → 门控 → 生成 → 视觉核对 |
| `routers/knowledge.py` | 350 | 保留 | 图谱 / wiki / 复核队列 |
| `indexing.py` | 325 | 保留并重构 | 索引 claim + lease + generation，迁入 corpus-worker |
| `evidence.py` | 319 | 保留 | evidence/citation 唯一实现 |
| `backfill.py` | 250 | 保留 | |
| `models.py` | 248 | 保留并重构 | 账号层模型（User/ApiKey/UsageRecord）删除，改由 control schema 承载 |
| `knowledge.py` | 229 | 保留 | |
| `archive.py` | 219 | 保留并重构 | 大文件不再整份进内存（不变式 6） |
| `storage.py` | 151 | 保留并重构 | 加 multipart 预签名与 Range |
| `main.py` | 148 | 保留并重构 | |
| `compilation.py` | 133 | 保留 | |
| `reconcile.py` | 124 | 保留并重构 | 对账循环迁入 corpus-worker |
| `config.py` | 332 | 保留并重构 | 62 项配置里账号/限速那部分迁去 Go |
| `gc.py` | 87 | 保留 | 全项目唯一不可逆毁数据处，宽限期 + claim 两道防护不得动 |
| `crops.py` | 84 | 保留 | 对象存储缓存层（渲染部分已在 ddp_core） |
| `routers/internal.py` | 78 | 保留 | model-gateway 的解析回调 |
| `review_export.py` | 77 | 保留 | |
| `deps.py` | 72 | 保留并重构 | 认证依赖改成读 control-api 下发的 actor context |
| `routers/search.py` | 70 | 保留 | |
| `service_client.py` | 69 | 保留 | 对 model-gateway 的客户端 |
| `upstream.py` | 59 | 保留 | |
| `versions.py` | 48 | 保留 | |
| `errors.py` | 46 | 保留 | |
| `db.py` | 38 | 保留 | |

### 3.3 旧 web 路由清单（52 条，对拍基准）

`/api/auth/{register,login,me}` · `/api/keys` ×3 · `/api/documents` ×16 ·
`/api/conversations` ×7 · `/api/search` · `/api/extractions/*` ×9 ·
`/api/knowledge/*` + `/api/wiki/*` + `/api/reviews/*` ×10 · `/api/usage` ·
`/files/{token}` · `/internal/parse-callback`

## 4. 明确删除（不进最终工作树）

| 删除项 | 理由 | 原调用方 | 替代 |
|---|---|---|---|
| 两份 `init.sh` / `start.sh`（各 2）| 旧双仓库并列布局专用，`local` 模式硬依赖同级目录假设 | 人工 | `scripts/dev.sh`（monorepo 单入口） |
| 两份 `.github/workflows/ci.yml` | 一份要跨仓 checkout 私有仓库 + `SERVICE_REPO_TOKEN`（合仓后无意义），另一份 testpaths 指向 `../tests` | GitHub Actions | `.github/workflows/*.yml` 分层 CI（§15.1） |
| 两份 `.gitignore` | 合并为根一份 | git | 根 `.gitignore` |
| `DeepDocParse-Web/{LICENSE,NOTICE}` | 与 service 仓库逐字节相同的副本 | — | 根 `LICENSE` / `NOTICE` |
| `backend/pyproject.toml` 末尾的「先装 gateway 再装 backend」约定 | 合仓后由 workspace 表达依赖 | 人工 / CI | `pyproject.toml` workspace |
| `gateway[corpus]` extra | 合仓后 `ddp_core` 是独立发行包，不再靠 extra 切分 | web backend、mcp_server | `python/ddp_core` 独立包 |
| 顶层包名 `app`（两处） | 两个发行包同名，从错误 cwd 启动会**静默**导入错包 | 全部 | `ddp_gateway` / `ddp_corpus` |
| `backend/app/models.py` 里的 `User`/`ApiKey`/`UsageRecord` | 写入所有权移交 Go（企业边界 5） | corpus 层各处 | control schema |
| ARQ 队列（`arq` 依赖 + `worker/tasks.py` 的排队部分） | 进程内/Redis 队列在滚动发布时丢任务（不变式 7） | gateway lifespan | PG job 表 + `FOR UPDATE SKIP LOCKED` |
| `extractions.reset_orphaned_runs()` 启动兜底 | 只因抽取任务活在进程内存才需要；持久任务后由 lease 过期接管 | `main.py` lifespan | corpus-worker lease |

## 5. 删除前的交叉核对清单

代码搜索**不足以**证明无用。每个删除项额外核对了：

- [x] 入口注册（`main.py` 的 `include_router`、`__init__.py` 导出）
- [x] 配置生成器（`scripts/gen_config_docs*.py` 的字段扫描）
- [x] Alembic 迁移（表/列是否仍被 `0001`–`0012` 引用）
- [x] 脚本（`scripts/`、`eval/`、`infra/*.bash`）
- [x] Compose / Dockerfile（`COPY` 路径、`command`）
- [x] CI 工作流
- [x] 文档中的运行命令
- [x] 动态 import（`importlib`、字符串路由、SQLAlchemy 的字符串外键）

> 动态引用的实际命中：SQLAlchemy 跨包外键按**表名字符串**解析
> （`ForeignKey("users.id")`）。删掉 `User` 模型后仍有 **9 处**指向
> `users.id` 的外键 —— `ddp_core/models.py` 4 处（`documents.uploaded_by`、
> `document_uploads.user_id`、`evidence_verifications` 的验证者、
> `knowledge_reviews.reviewer_id`）与 `ddp_corpus/models.py` 5 处
> （`api_keys` `conversations` `extraction_templates` `extraction_runs`
> `usage_records`，其中三张表本身要迁去 control）。这些必须在同一次改动里
> 改成不带 FK 约束的 `actor_id`，否则 `create_all` 与 alembic 会在
> mapper 配置期爆炸。详见 `docs/refactor/DATA-OWNERSHIP.md`。

---

## 6. 执行记录：账号层剥离与外部平面归位（2026-09-02）

### 6.1 实际删除

| 删除项 | 替代实现 | 等价覆盖 |
|---|---|---|
| `backend/app/routers/{auth,apikeys,usage,proxy,files}.py` | control-api 的 `/api/auth/*` `/api/keys/*` `/api/usage` `/files/{token}` 与统一入口 | Go 的 config/auth/rbac/proxy 用例 |
| `backend/app/{security,metering}.py` | control-api 的 `internal/auth`、`internal/ratelimit`、`usage_ledger` | 同上 |
| `POST /api/documents`（multipart 上传） | 预签名直传 + `DocumentSubmitted` 事件（`ddp_corpus/ingest.py`） | `test_corpus_api_accepts_no_file_bodies`（静态守卫，变异确认过） |
| `tests/test_{auth_keys,usage,proxy}.py` | 见下表 | |
| `User` / `ApiKey` / `UsageRecord` / `FileToken` 四张表 | control schema | `scripts/check_data_ownership.py` |
| corpus 侧的三处领域限速 | control-api 的 `domainThrottle` | Go 的 `internal/ratelimit` |

### 6.2 测试搬迁对照（§12.1：下降必须有等价覆盖证明）

| 原用例 | 去向 |
|---|---|
| `test_auth_keys.py`（5） | Go：`TestSessionRejectsNoneAlgorithm` `TestVerifyPasswordAlwaysDoesWork` `TestAPIKeyShape` `TestViewerCannotEscalateViaAPIKey` 等 |
| `test_usage.py`（2） | Go：`store.UsageSeries` + `handleUsage`（按天/种类聚合、scope=organization 需 admin） |
| `test_proxy.py` 的鉴权/限速/额度/SSE/MCP（8） | Go：`TestProxyReplacesClientAuthorization` `TestProxyStreamsWithoutBuffering` `TestProxyPassesMCPSessionHeaderBothWays` + `requireAPIKey` |
| `test_proxy.py` 的**计量归集五条** | **保留**，搬到 `tests/test_external_plane.py` —— 那是语料侧真正拥有的语义 |
| `test_ops.py` 的限速三条 | Go：`internal/ratelimit` |
| `test_documents.py` 的上传体积两条 | control-api 在签发预签名前把关 + 静态守卫 |
| `test_documents.py` 的 `/files` 两条 | control-api（302 到短期签名 URL） |
| `test_eval_metrics.py` / `test_eval_graph.py`（27） | 搬到 `eval/tests/`（它们验的是评测器，不是语料 API） |

### 6.3 一个**没有**被删的东西

`/v1/parse*` **不是**直接代给网关的。它会在语料里留下 Document 与 ParseJob
（调用方的历史在 Web 端也看得到，按页计量也需要锚点），而那两张表 Go 一个字
都写不了。所以入口把 `POST /v1/parse` 与 `GET /v1/parse/*` 路由到 corpus-api，
其余 `/v1/*`（chat / embeddings / models / rerank）才直接代给网关。

**对外契约一个字没变**（非目标 §3.2）。那五条计量回归用例
（第二个用户不能白嫖 / 跨用户取结果不能记错人 / 三方共享时中间那位也要计费 /
共享任务不 500 / 外部提交者要记归属）随实现一起保留，判重锚点从
`UsageRecord` 换成语料侧的 `usage_claims` 表 —— 理由写在
`models.UsageClaim` 的 docstring 里。
