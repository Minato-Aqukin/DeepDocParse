# DeepDocParse 合仓与企业化重构方案

> 状态：提案（Draft 2：一次性重构版）  
> 日期：2026-09-01  
> 适用范围：`DeepDocParse`、`DeepDocParse-Web` 及其部署、契约、CI  
> 说明：本文件不覆盖当前执行权威 `plan.md`。正式执行前，须由用户确认本方案，并将获批决定同步回 `plan.md`。

## 1. 结论

将两个仓库合并为一个 monorepo 是可行且有收益的，但合仓不等于合并成单体，也不等于把 Python 全量重写。

建议目标是：

1. 将两个现有仓库连同 Git 历史合并为一个 monorepo，在独立重构分支中一次性完成目标架构。
2. 允许复用经过审查、仍符合目标架构的现有实现；最终工作树不保留未使用的旧模块、legacy 服务和兼容壳。
3. 新系统只在全量实现、迁移演练和验收完成后做一次生产切换；不做生产环境的新旧路由并存或渐进切流。
4. 运行时仍拆为多个可独立部署、扩缩容和失败隔离的服务。
5. Go 实现企业控制面和高并发数据入口。
6. Python 保留并重构语料核心、PDF、证据链、检索编排和模型接入。
7. 大文件直接进入对象存储，PDF、原图和裁图直接从对象存储/CDN 读取，不让 Go 或 Python 成为数据中转站。

目标架构：

```text
浏览器 / SDK / MCP Client
          │
          ▼
Go Control API（无状态、可水平扩展）
  ├─ 组织 / 用户 / 角色 / SSO
  ├─ API Key / 配额 / 限速 / 计量
  ├─ 上传签名 / 下载授权 / 审计
  ├─ 对外 /api、/v1、/mcp 入口
  └─ SSE / Webhook / 任务提交
          │
          ├───────────────┐
          ▼               ▼
Python Corpus API      Object Storage / CDN
  ├─ 文档与版本            ├─ PDF 原件
  ├─ evidence/citation     ├─ layout/图片
  ├─ 检索/问答/抽取         └─ bbox 裁图
  ├─ Wiki/图谱/复核
  └─ corpus MCP
          │
          ▼
Python Workers / Model Gateway
  ├─ 编译 / 索引 / 抽取
  ├─ MinerU / OCR / VLM
  ├─ TEI / reranker
  └─ GPU 调度与结果归档
```

## 2. 为什么现在需要合仓

当前两个仓库的运行边界仍然有价值，但开发边界已经出现明显摩擦：

- Web 后端需要先安装 service 仓库的 `ddp_core`，存在跨仓安装顺序约定。
- 两个 Python 发行包都曾使用顶层包名 `app`，从错误工作目录启动可能静默导入错包。
- 契约、`ddp_core`、Web 消费方、数据库迁移、Docker 和 CI 经常需要跨仓原子修改。
- 一个功能提交必须在两个仓库分别提交、验收和 push，版本关系没有机器可验证的单一真相。
- `openapi.yaml` 是跨仓契约，但 DDP-* 文档、前端类型和 Python 模型仍有人工同步面。
- 当前大文件上传、PDF 查看和 bbox 裁图读取均经过应用进程，扩容应用会同时放大对象存储带宽中转和内存占用。

合仓解决的是开发一致性、原子提交、依赖表达和统一发布，不改变服务的故障域与伸缩边界。

## 3. 目标与非目标

### 3.1 目标

- 一次提交可以原子修改契约、Go、Python、前端、迁移和部署配置。
- 建立语言中立的契约源，生成 Go、TypeScript 和 Python 类型。
- Go 控制面支持大组织需要的 SSO、RBAC、审计、API key、配额、限速和高并发入口。
- Python 语料面保持现有 evidence/citation/bbox 不变式和模型生态。
- 上传、下载、PDF 预览和裁图访问不再线性消耗应用进程内存与带宽。
- API、worker、模型运行时分别扩缩容。
- 新系统在独立重构分支和独立环境中一次性完成；最终提交不包含未使用的旧代码和双轨实现。

### 3.2 非目标

- 不重写 MinerU、vLLM、TEI 或其他官方模型运行时。
- 不为了统一语言而全量重写 `ddp_core`。
- 最终工作树不保留或部署未使用的旧应用代码，不提供 legacy 路由和旧内部包兼容层。
- 不把“一次性交付”理解为一个巨大 commit；开发期仍正常使用小提交，但中间版本不进入生产。
- 不将大型模型权重、Python 环境或桌面二进制发布到 npm。
- 不在没有容量数字前引入 Kafka、独立向量数据库或复杂服务网格。
- 不改变现有 `/v1/*` 语义；新增行为必须向后兼容。

## 4. 必须保留的产品不变式

整个重构必须继承 `plan.md` 的四条真不变式：

1. 每条结论都能指回一个 bbox；指不回必须明确说明。
2. 任何降级都必须对 API、存储和 UI 可见。
3. 生成物与原文必须可区分，生成物引用最终仍指向原始原子 bbox。
4. 契约先于实现；先改 OpenAPI、DDP-* 或 MCP 文档，再改消费方。

额外增加四条企业化边界：

5. 一个数据对象只能有一个写入所有者；Go 与 Python 不得任意共同写同一业务表。
6. 大文件不得完整进入应用进程内存，也不得由应用进程长期中转下载流量。
7. 进程重启不得使已受理任务永久丢失或永远停在运行态。
8. 若进入多组织 SaaS 模式，任何数据库、检索、对象存储和缓存查询都必须有组织边界，不能只靠调用方自觉过滤。

## 5. 目标仓库布局

建议新仓库名继续使用 `DeepDocParse`，原 service 仓库和 Web 仓库作为历史来源合入：

```text
DeepDocParse/
├── apps/
│   └── web/                       # Vue 3 + Vite + TypeScript
├── services/
│   ├── control-api/               # Go：企业控制面和统一入口
│   ├── corpus-api/                # Python：语料 API
│   ├── model-gateway/             # Python：解析/模型协议适配
│   ├── corpus-worker/             # Python：编译/索引/抽取持久 worker
│   └── mcp/                       # Python：语料级 MCP
├── python/
│   └── ddp_core/                  # evidence/检索/编译纯逻辑共享包
├── packages/
│   ├── contracts/                 # OpenAPI、JSON Schema、DDP-* 契约
│   ├── sdk-ts/                    # npm SDK
│   └── cli/                       # npm CLI（可后做）
├── database/
│   ├── control/                   # Go 所有的迁移
│   └── corpus/                    # Python/Alembic 所有的迁移
├── eval/                          # OCR/出处/抽取/Agent/图谱评测
├── infra/
│   ├── compose/
│   ├── kubernetes/
│   └── autodl/
├── scripts/
├── docs/
├── package.json                   # npm workspaces
├── go.work
└── pyproject.toml                 # 可选：Python workspace/tooling 入口
```

新 monorepo 通过合并两个仓库的 Git 历史建立，然后直接在重构分支上整理为目标布局。
最终工作树不得存在 `legacy/`、`compat/`、`old-api/` 等过渡目录，也不得同时保留新旧两套实现。

现有代码逐模块作“保留并重构 / 重写 / 删除”判定：仍符合目标职责、契约和不变式的实现可以迁入目标目录；
已被新实现替代、职责错误、重复、仅为旧部署服务或没有调用方的代码必须删除。被删除代码仍可从 Git 历史追溯，
但不进入最终构建产物、容器或运行路径。

## 6. 语言与职责边界

### 6.1 Go Control API

Go 负责高并发、无状态、企业组织相关能力：

- 组织、用户、成员关系、角色、权限。
- OIDC；SAML/SCIM 作为企业增强项独立交付。
- JWT、API key、服务凭据。
- 配额、限速、计量入口与审计日志。
- 预签名 multipart 上传、上传 finalize、下载/裁图授权。
- `/api/*`、`/v1/*`、`/mcp` 的统一入口和反向代理。
- SSE、Webhook、回调入口、请求 ID 和 trace 传播。
- 向持久任务表或队列提交任务，不执行 OCR、编译或索引。

建议实现选择：

- HTTP：标准库 `net/http` 或轻量路由器，不引入重量级应用框架。
- PostgreSQL：`pgx`；查询可使用 `sqlc` 生成类型安全代码。
- Redis：只用于分布式限速、短期缓存和选主，不作为唯一业务真相。
- 可观测性：OpenTelemetry + Prometheus。
- 契约：从 `packages/contracts` 生成服务端接口和客户端类型。

### 6.2 Python Corpus API

Python 保留与证据、PDF、检索和模型语义紧密相关的代码：

- 文档、版本、编译状态、索引状态。
- Chunk、Evidence、Citation、Assertion、Verification。
- 混合检索、RRF、rerank、候选门控。
- 问答、抽取、Wiki、图谱和复核队列。
- PDF 渲染、bbox、裁图和 born-digital 解析。
- DDP-* 格式的参考实现与质量评测。
- 语料级 MCP 五工具。

Python 服务仍应是无会话状态的 API 进程；持久状态进入 PostgreSQL、对象存储和持久任务队列。

### 6.3 Python Model Gateway / Workers

- gateway 继续按模型注册表做协议适配，不 import 重型模型代码。
- MinerU、vLLM、TEI 和 OCR 继续作为独立运行时。
- 编译、索引、抽取从 FastAPI `BackgroundTasks` / `asyncio.create_task` 迁入持久 worker。
- CPU 密集纯函数只有在 profiling 证明为瓶颈后，才通过 Rust + PyO3 局部优化。

### 6.4 TypeScript 前端与 SDK

- Vue 前端只调用公开契约，不 import Go/Python 内部模型。
- API 类型由 OpenAPI/JSON Schema 生成。
- `@deepdocparse/sdk` 作为 npm 包发布，服务于浏览器、Node 和企业集成。
- degraded、status、source_type 等关键枚举只从契约生成，禁止三处手写。

## 7. 数据所有权与服务通信

### 7.1 数据库边界

初期可以继续使用同一个 PostgreSQL 集群，但按 schema 和数据库角色隔离：

```text
control schema（Go 写，Python 只按必要权限读）
├── organizations
├── users
├── memberships
├── roles / permissions
├── api_keys
├── quotas / usage_ledger
└── audit_events

corpus schema（Python 写，Go 只通过 API 访问）
├── documents / document_uploads
├── parse_jobs / extraction_runs
├── chunks / evidence / citations
├── conversations / assertions / verifications
└── entities / edges / wiki / reviews
```

禁止：

- Go 直接更新 evidence/citation。
- Python 直接修改组织成员和角色。
- 两个服务分别维护同一字段的副本而没有对账。
- 在一次请求里依赖跨服务分布式事务。

跨边界流程采用本地事务 + Outbox：业务数据与 outbox 事件在一个事务提交，再由投递器发送；消费者以事件 ID 幂等处理。

### 7.2 内部协议

第一阶段继续使用 HTTP + OpenAPI，避免同时引入 gRPC 和 Go 重构两个变量。只有满足以下条件才考虑 gRPC：

- profiling 证明内部 JSON 序列化或连接开销是显著瓶颈；
- 存在大量稳定的服务间强类型调用；
- 团队能同时维护 HTTP 外部契约和 gRPC 内部契约。

内部调用必须携带：

- request/trace ID；
- 服务身份；
- organization/workspace context（若启用）；
- user/API key actor ID；
- idempotency key。

## 8. 大组织部署模式

### 8.1 推荐首发：单组织独占部署

一次部署服务一个组织，延续当前“共享语料”语义：

- 组织内成员共享语料。
- RBAC 控制上传、删除、复核、管理和 API key。
- 数据库和对象存储属于该组织的部署。
- 最容易保持当前 evidence 与全局去重语义。

### 8.2 后续可选：多组织 SaaS

多组织不能只在 Go 入口加 `organization_id`；必须端到端隔离：

- 所有 corpus 行增加 `organization_id` 或 `workspace_id`。
- 唯一约束、检索、回填、GC、对象键和缓存键全部带组织维度。
- PostgreSQL RLS 作为数据库最后一道防线。
- API key、计量和审计均按组织作用域。
- 全局内容哈希不能直接代表组织可见 Document。

允许物理 blob 按内容去重，但逻辑 Document、Evidence、Citation 和归属必须按组织分开。对象是否已存在不得成为跨组织可观察信息。

多组织 SaaS 是独立阶段，不与第一次 Go 迁移混在一起。

## 9. 大文件与 bbox 数据通路

### 9.1 上传

目标流程：

```text
1. 客户端向 Go 申请上传会话
2. Go 校验权限、配额、MIME、预期大小
3. Go 返回对象存储 multipart 预签名信息
4. 客户端直接上传对象存储
5. 客户端调用 finalize
6. Go 校验对象大小与摘要状态，创建上传记录并发出 DocumentSubmitted
7. Python 领取任务，创建/复用 Document 和 ParseJob
```

必须避免：

- 浏览器 → Go/Python 内存 → MinIO；
- 将 200MB 文件拼成单个 Python `bytes`；
- 依赖客户端声明的哈希而不做服务端验证；
- finalize 重试创建两份任务。

哈希验证可采用：对象存储元数据 + 后台流式校验。校验完成前文档状态为 `verifying`，不能进入解析。

### 9.2 PDF、原图和裁图

- Go 做授权，返回短期签名 URL；大对象由对象存储或 CDN 服务。
- PDF 服务必须支持 HTTP Range，避免查看一页下载整份 PDF。
- bbox 坐标继续作为小 JSON 由 API 返回，前端在 PDF.js 上绘制。
- 裁图键包含不可变 bbox 摘要，可设置长期 `Cache-Control: immutable`。
- service 所需的稳定文件 URL 与浏览器短期 URL 分开，不能为前端性能破坏 `doc_hash` 幂等机制。

## 10. 任务与并发模型

当前 Web 进程内的 BackgroundTasks/`asyncio.create_task` 不作为企业部署的最终形态。

目标状态机：

```text
queued → claimed(generation, lease_until) → running → succeeded / failed
                    │
                    └─ lease 过期后可被新 worker 接管
```

要求：

- 任务状态首先落 PostgreSQL，再返回 202。
- 领取使用 claim + generation fencing + lease/heartbeat。
- worker 崩溃后任务可接管；旧 worker 迟到不能覆盖新结果。
- 失败原因和降级必须持久化并在 UI 可见。
- embedding、VLM、OCR 分别设置并发与队列，不能共用一个无量纲总并发。
- API worker 不执行长时间索引、裁图批处理或抽取任务。

第一版可使用 PostgreSQL job 表和 `FOR UPDATE SKIP LOCKED`，或复用 Redis/ARQ 但把 PostgreSQL 作为任务真相。没有容量证据前不引 Kafka。

## 11. 一次性重构与单次切换计划

本次不采用 A–J 渐进上线，也不让新旧实现长期并存。开发期间允许正常拆分小 commit、模块和工作流，
但它们共同组成一个重构版本；只有完整目标系统通过全部验收后，才一次性切换生产。

### 11.1 建立合并仓库与重构分支

- 在临时 clone 中用 `git filter-repo --to-subdirectory-filter` 给两个仓库历史加前缀。
- 在新 monorepo 中用 `git merge --allow-unrelated-histories` 合并，保留原 author、时间和 message。
- 创建唯一的重构主分支；旧仓库在重构期间冻结功能开发，只接收阻塞性安全修复。
- 记录旧系统 commit、契约摘要、数据库 migration head、对象存储清单和测试基线。
- 合并后立即按目标目录重组，不建立 `legacy/` 运行目录。

合仓是重构工程的准备动作，不作为一个需要上线的中间产品版本。

### 11.2 全量代码盘点与删除判定

对每个现有模块建立台账，并且只能落入以下三类之一：

| 判定 | 含义 | 最终动作 |
|---|---|---|
| 保留并重构 | 职责正确、行为已经验证，但需要换包名、依赖或服务边界 | 移入目标模块并重构 |
| 重写 | 契约需要保留，但实现与目标架构冲突 | 用 Go 或 Python 重新实现，旧文件删除 |
| 删除 | 重复、不可达、旧部署专用、被替代或没有调用方 | 从最终工作树和构建清单移除 |

删除不能只靠代码搜索。每项还要核对入口注册、配置生成器、迁移、脚本、Compose、CI、文档和动态 import。
最终提交必须附删除台账，说明删除理由、原调用方和替代实现；无法证明无用的代码不得凭感觉删除。

### 11.3 一次性实现目标架构

以下工作流可以在开发期并行，但必须一起进入同一个生产版本：

1. **契约主干**：集中 OpenAPI、DDP-*、MCP Schema，生成 Go、TypeScript、Python 类型。
2. **Go Control API**：一次性实现组织、用户、RBAC、API key、限速、计量、审计、上传签名、统一入口和 SSE 代理。
3. **Python Corpus API**：重构 documents、evidence、citation、search、QA、extraction、knowledge、review 和 MCP。
4. **Model Gateway / Workers**：保留注册表驱动，重构为持久任务、lease、heartbeat 和 generation fencing。
5. **大文件数据面**：multipart 直传、finalize、签名下载、Range、ETag、CDN/对象存储直读。
6. **数据库 v2**：建立 `control`/`corpus` schema、明确写入所有权，并编写一次性数据迁移器。
7. **前端**：切换到新契约和组织/RBAC 模型，删除旧 API adapter、旧状态字段和不可达页面。
8. **部署**：从零编写新 Compose/生产部署，镜像中只包含目标服务。

Go 不先做透明代理版本，Python 也不保留 legacy Web API。开发环境只运行目标架构；需要核对旧行为时使用冻结夹具、
API 录制结果和旧系统只读测试环境，而不是把旧实现嵌进新系统。

### 11.4 数据库与对象的一次性迁移

新系统使用新的 schema revision，不要求新代码兼容所有旧中间表结构。单独提供一次性迁移程序：

```text
旧 PostgreSQL + 旧 MinIO
  → 只读扫描与预检
  → 转换到新 control/corpus schema
  → 对象键迁移或建立明确映射
  → evidence/citation/文档版本对账
  → 生成迁移报告
```

迁移要求：

- 至少做三次全量演练：空库、生产快照、对抗数据集。
- 迁移器可重复运行且结果幂等；不得因重跑产生重复计量、文档、出处或任务。
- 对账覆盖行数、外键、对象存在性、content digest、引用反查和 bbox 抽样。
- 接不回的旧出处保留并标失效，不得静默删除或伪装为已接回。
- 迁移器是交付工具，不进入正常请求路径；迁移完成后从运行镜像删除。

### 11.5 全量验收门

生产切换前必须同时满足：

- 全部公开 API、DDP-*、MCP 契约测试通过。
- Go、Python、前端和部署的完整 CI 全绿。
- 注册、上传、解析、编译、索引、检索、问答、bbox、Wiki、图谱、API key、计量和审计走完真实用户路径。
- 固定夹具与冻结的旧系统输出逐字段比较；任何有意差异都有获批记录。
- 新数据库迁移在生产快照上完成演练并通过对账。
- 并发上传、PDF Range、SSE、队列满载和依赖故障压测达到获批 SLO。
- 安全测试覆盖组织边界、权限矩阵、对象 URL、API key、服务凭据和日志脱敏。
- 最终源码树扫描不存在 legacy 服务、重复实现、旧入口、死配置和未引用生产模块。

任一项未通过，整个重构版本不得上线；不允许把缺失能力留给上线后补齐。

### 11.6 单次生产切换

切换窗口执行：

1. 停止旧系统写入并进入维护模式。
2. 完成最终数据库和对象存储快照。
3. 执行一次性数据迁移与全量对账。
4. 部署完整的新 Go/Python/前端/worker 系统。
5. 运行 smoke、关键真实路径和证据抽样。
6. 一次性把入口流量切到新系统。
7. 解除维护模式并持续观察。

不做按路由、按用户或按百分比灰度；切换单位是整个产品。

### 11.7 失败处置

本方案不保留旧代码作为长期运行回退路径。生产切换后以向前修复为主；因此必须在切换前把风险压到最低。

- 切换前的数据库与对象快照只用于灾难恢复和数据取证，不等同于继续维护旧实现。
- 若迁移或 smoke 失败，保持维护模式，修复新系统或迁移器后重新执行，不开放一个半迁移状态的系统。
- 新系统开始接受写入后，不允许把数据库直接降回旧结构。
- 旧仓库代码仍存在于合并后的 Git 历史，便于审计和追责，但不在最终工作树、构建产物或部署清单中。
- 重构版本稳定并完成审计后，原两个独立仓库归档只读；monorepo 成为唯一开发与发布源。

## 12. 测试与验收体系

### 12.1 每次 commit 的固定门禁

延续现有流程：

```text
两套 Python pytest
→ 契约守卫
→ 配置文档 --check
→ Go test / vet
→ 前端类型检查 + Vitest
→ 构建
→ 相关 Playwright
→ 独立 agent 按本次 diff 验收
→ commit / push
```

测试数量在重构启动时重新记录为基线，只许因明确合并重复测试而下降；下降必须有等价覆盖证明，不能静默删除。

### 12.2 对拍测试

- 新 API 对冻结的旧系统黄金 fixture 返回兼容结构；对拍不依赖在新系统中运行旧代码。
- Python 与生成的 Go/TS 模型对 DDP-* fixture 往返一致。
- 新直传流程对固定文件产生预期的 `doc_id`、Document、ParseJob 和对象内容。
- 新检索路径对固定语料返回黄金 evidence 集；排序变化必须有显式质量评测。
- 所有 degraded 代码和错误体逐项覆盖。

### 12.3 压测场景

容量目标必须由用户确认。建议先以以下工作负载建立基线，而不是直接承诺生产数字：

- 登录/API key/列表/证据详情的混合短请求。
- 大量并发 PDF Range 查看和 bbox 元数据请求。
- 多个 50/100/200MiB multipart 上传。
- 长连接 SSE 问答及客户端中途断开。
- 同一文档并发重复上传和重复 finalize。
- 索引、抽取、OCR 队列满载时的 API 延迟。
- PostgreSQL/Redis/MinIO 人工延迟与短时故障。

记录：p50/p95/p99、错误率、RSS、事件循环延迟、Go goroutine 数、PG pool wait、Redis 延迟、对象存储吞吐、队列年龄、GPU 利用率和每任务成本。

## 13. 可观测性

统一字段：

- `request_id`
- `trace_id`
- `organization_id` / `workspace_id`
- `actor_id` / `api_key_id`
- `document_id` / `parse_job_id` / `task_id`
- `engine` / `model`
- `degraded`

核心指标：

- API QPS、p95/p99、状态码。
- 上传会话、上传字节、finalize 失败。
- PDF/裁图签名与对象存储命中率。
- PG 连接池等待和慢查询。
- 各任务队列深度、最老任务年龄、claim 接管次数。
- OCR/VLM/embedding/rerank 吞吐、排队和失败。
- retrieval vector/BM25/rerank 路径计数。
- evidence 写入失败、失效出处、verification 分布。

日志不得包含原文全文、JWT、API key、SERVICE_TOKEN、预签名 URL 查询串或上传内容。

## 14. 安全与合规

大组织版本至少需要：

- OIDC 企业登录；管理员强制 MFA 由 IdP 策略承担。
- 明确 RBAC：viewer、contributor、reviewer、admin。
- API key 作用域、过期、撤销和最后使用时间。
- 服务间凭据轮换；生产优先 mTLS 或短期服务 token。
- 对象 URL 短期有效、最小权限、独立下载域名。
- 原文件 MIME 白名单、`nosniff`、安全 Content-Disposition。
- 审计日志不可由普通管理员修改。
- 数据保留、软删除宽限、不可逆 GC 和恢复流程。
- 备份加密、恢复演练、密钥不进入 Git/镜像。
- 若为多组织 SaaS，RLS 与跨租户负样本是发布门禁。

## 15. CI/CD 与发布

### 15.1 CI 分层

根据变更路径选择任务，但合并队列前必须满足全局契约门禁：

- `services/control-api/**`：Go test、契约、代理/SSE 对拍。
- `python/**`、Python services：pytest、契约、配置、评测守卫。
- `apps/web/**`：typecheck、Vitest、Playwright。
- `packages/contracts/**`：触发全部语言生成与全部消费方测试。
- `database/**`：新库从零建立、旧生产快照一次性迁移、幂等重跑和带数据对账。
- `infra/**`：配置渲染、镜像构建、启动/就绪检查。

### 15.2 版本与发布

- monorepo 使用一个产品版本号，各镜像带相同 Git SHA。
- 镜像仍分别发布：control-api、corpus-api、worker、gateway、mcp、web。
- OpenAPI/DDP 契约独立记录 revision。
- npm 只发布 `@deepdocparse/contracts`、`@deepdocparse/sdk` 和可选 CLI。
- 生产发布使用维护窗口：冻结旧写入 → 快照 → 一次性迁移 → 部署全套新版本 → 对账 → 整体切流。

## 16. 风险台账

| 风险 | 后果 | 防护 |
|---|---|---|
| 合仓时丢 Git 历史 | 无法审计来源 | 临时 clone + filter-repo；随机抽查旧 commit |
| 一次性重构范围过大 | 集成问题集中到末期 | 开发期小提交、持续集成、每日完整构建；但只做一次生产切换 |
| 删除仍有动态调用的旧代码 | 运行时缺功能 | 模块删除台账、入口/配置/脚本/动态 import 全面扫描 |
| Go/Python 数据所有权不清 | 数据漂移 | schema和数据库角色强制单写所有权，不设计双写 |
| 代理破坏 SSE/取消 | 计量错、任务泄漏 | 字节级代理测试、真实 uvicorn/Go e2e |
| 上传直传绕过校验 | 恶意对象、配额绕过 | 上传会话、大小/MIME约束、finalize验证 |
| 预签名 URL 泄漏 | 未授权下载 | 短 TTL、独立域名、审计、禁止日志记录查询串 |
| 多组织只在入口过滤 | 跨租户泄漏 | corpus强制作用域 + RLS + 负样本 |
| Go重写证据规则 | 假出处 | evidence/citation 唯一实现继续在 Python |
| 多 worker 耗尽 PG 连接 | 全站雪崩 | 连接预算、PgBouncer、pool wait 监控 |
| 进程内任务在滚动发布丢失 | 永久运行态 | 持久队列、lease、fencing、对账 |
| 为性能过早引 Kafka/Rust | 复杂度上升无收益 | 以 profiling 和容量数字作为引入条件 |

## 17. 预估工作量

以下是单个熟悉现有代码库的工程师的粗略人周。它们是一次性交付内部的工作流，
不是可以单独上线的阶段；不含等待 GPU、企业 IdP 协调和安全审计：

| 工作流 | 粗略工作量 |
|---|---:|
| 基线、合仓与代码盘点 | 1–2 周 |
| 目录、包名与统一构建 | 1–2 周 |
| 契约生成 | 1–2 周 |
| Go完整控制面与统一入口 | 4–8 周 |
| 大文件数据面 | 2–3 周 |
| Python语料服务重构 | 3–5 周 |
| 持久任务与worker | 2–3 周 |
| 前端适配与旧界面清理 | 2–4 周 |
| 数据迁移器与三轮演练 | 2–4 周 |
| 容量、HA、安全与切换演练 | 3–5 周 |

合计约 21–38 人周。一次性交付把跨模块集成、迁移和切换风险集中在最后，
因此工作量高于渐进路线；多人并行也不能按人数线性缩短。

## 18. 开工前必须确认的决定

1. 首发是单组织独占部署，还是多组织 SaaS？建议前者。
2. Go 是否一次性接管完整控制面（组织、鉴权、计量、限速、审计和上传）？本方案默认是。
3. 新 monorepo 是否沿用 `DeepDocParse` 名称？建议沿用。
4. 原两个仓库何时归档只读？建议合仓开始即冻结功能开发，新版本稳定并审计后归档。
5. 目标容量：并发用户、并发上传、文件大小、SSE 数和每日文档量分别是多少？
6. 企业身份源是 OIDC、SAML，还是两者都要？建议先 OIDC。
7. 是否要求跨区域、RTO/RPO、私有化离线安装和审计留存年限？

## 19. 一次性交付范围

以下内容必须作为同一个重构版本同时完成，不能拆成生产批次：

1. 两个仓库及历史合并为唯一 monorepo。
2. 最终目录、包名、统一 CI、构建和版本号落地。
3. 删除重复、不可达、旧部署专用和已被替代的代码，不保留 legacy 运行路径。
4. contracts workspace 与 Go/TS/Python 类型生成落地。
5. Go 完整控制面、统一入口、组织、RBAC、API key、计量、限速和审计上线。
6. Python Corpus API、Model Gateway、MCP 和持久 worker 完成目标边界重构。
7. 上传改为对象存储 multipart 直传；PDF/裁图支持签名直读和 Range。
8. 新数据库 schema、一次性迁移器、对象迁移和三轮演练完成。
9. 前端只使用新契约，删除旧 adapter、状态和不可达页面。
10. 全量功能、安全、迁移、容量、HA 和真实用户路径验收通过。
11. 在维护窗口完成一次数据迁移和一次全产品切换。

其中任意一项未完成，重构版本不进入生产。旧代码可以存在于 Git 历史中用于追溯，
但不得存在于最终工作树、构建产物、容器、部署清单和运行时路由中。
