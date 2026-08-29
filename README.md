# DeepDocParse

[English](README.en.md) · Apache-2.0 · **文档解析由 [MinerU](https://github.com/opendatalab/MinerU) 提供支持**

面向技术手册与论文的多模态语料服务器。gateway 无状态转发模型，核心语料以
PostgreSQL + MinIO 持久化，对外提供解析、检索、抽取、知识与 MCP 能力：

| 平面 | 接口 | 引擎 |
|------|------|------|
| 识别 | `POST /v1/parse`（异步任务） | MinerU / born-digital / **vlm-ocr**（注册表驱动，见 models.yaml） |
| VQA | `POST /v1/chat/completions`（OpenAI 协议） | DeepSeek-OCR-2 |
| 向量 | `POST /v1/embeddings`、`POST /v1/rerank` | bge-m3 / bge-reranker-v2-m3（TEI） |
| **抽取** | **`POST /v1/extract`（异步任务）** | 编排层（检索定位 → 抽值 → bbox 裁剪 → 视觉核对） |
| MCP | `search` / `ask` / `get_evidence` / `read_wiki` / `graph_neighbors` | 语料级接口，返回 evidence + bbox + 裁图；`ask_document` 仅兼容保留 |

架构决策见根目录 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

## 这个项目想成为什么

**「可验证出处」的参考实现，而不是一个更小的 RAGFlow。**

这个类别里所有项目都宣称支持 grounded citation，但没有一个公开基准去度量 bbox 级出处的正确性——
于是"支持"这件事从来没被证明过。本项目把赌注全押在这一件事上：

- **出处三件套**：每个回答的每条依据都带页码 + bbox + 从原件裁出来的区域截图，点得开、对得上
- **字段级出处**（M9）：结构化抽取的**每一个字段**都点得开它的出处。
  市面上的抽取产品（Azure DI / Textract / LlamaExtract）返回字段最多带一个置信度，
  指不回原文的哪一块 —— 这是出处能力的更强形态，不是新品类
- **视觉验证**：裁出来的图会再喂给视觉模型核对一遍，而不是只信文本相似度
- **降级必须可见**（架构级铁律）：检索零命中、向量化挂了、视觉模型不可用、裁不出图、
  解析本身可疑、模型输出不合 schema——每一种都在结果上打标，绝不静默退化。
  这个项目吃过静默降级的大亏（M4a 的向量检索悄悄退回 BM25，很久没人发现）

> **定位表述从「可验证出处的问答」扩为「可验证出处的文档信息提取」。**
> 定位本身没变——不这么框的话，下面那份「明确不做」会失去否决力。

功能数量上追 RAGFlow / Docling / WeKnora 是必输的，追上了也不会有人因此选择本项目。
所以有一份同样重要的[**明确不做**](#明确不做)清单。

## 目录结构

```
DeepDocParse/
├── openapi.yaml            # 与 DeepDocParse-Web/backend 的接口契约（唯一依赖面）
├── models.yaml             # 模型注册表：加模型 = 加容器 + 一行配置
├── models.autodl.yaml      # 无 docker 的 GPU 机器用（endpoint 全是回环地址）
├── gateway/                # 唯一自研服务：薄适配层
│   ├── ddp_core/           # 共享语料模型、检索、编译、Agent 与知识纯逻辑
│   └── app/
│       ├── main.py         # FastAPI 入口
│       ├── config.py       # 配置 + 注册表加载
│       ├── auth.py         # service token 校验
│       ├── routers/        # parse / chat / health
│       ├── services/       # mineru_client / vqa_client / task_store
│       └── worker/         # ARQ 任务：结果归档链（v2 追加向量化步骤）
├── mcp_server/             # FastMCP：直读 PG/MinIO 的五个语料工具 + deprecated ask_document
├── docker/
│   ├── compose.dev.yml     # RTX 4060 8GB：MinerU pipeline（VQA 走宿主机原生二进制，见 models.dev-host.yaml）
│   └── compose.prod-nvidia.yml  # RTX 6000 级：vLLM + mineru-router 多卡
├── deploy/autodl/          # **跑不了 docker 的 GPU 机器**（AutoDL 实例本身是非特权容器）：
│                           #   裸进程部署 DeepSeek-OCR-2 + 抽取用指令模型，含 chat template 与验证脚本
├── scripts/                # make_fixtures（e2e 素材）/ prepare_bge_m3（权重转 safetensors）/ e2e_mcp（真机 e2e）
└── tests/                  # 契约测试（mineru 升级前必须通过）
```

## 快速开始（无 GPU 也能跑）

没有 GPU 就完全跑不起来，曾经是这个项目最大的采用阻塞。现在默认路径不需要 GPU：

```bash
cp .env.example .env        # 至少填一个真实的 SERVICE_TOKEN

# 一次性准备：只有向量检索需要权重（TEI 只认 safetensors，官方 bge-m3 只发 .bin）
python scripts/prepare_bge_m3.py

cd docker
docker compose -f compose.cpu.yml --env-file ../.env up -d --build
# gateway:  http://localhost:9000   契约文档: http://localhost:9000/docs
```

五个语料级 MCP 工具会直接读取 PostgreSQL/MinIO。默认连接同机
DeepDocParse-Web 的 `15432/19000` 端口；请先启动 Web 数据面，或在 `.env` 里设置
`CORPUS_DATABASE_URL` / `CORPUS_MINIO_*`。数据库不可达时工具会返回明确错误，
不会退回旧 Redis 索引假装成功。工具签名见 [docs/mcp-tools.md](docs/mcp-tools.md)。

> **从 2026-08 之前的版本升上来的注意：compose 项目名变了**（缺省的 `docker` → 固定的
> `ddp-service`）。两个仓库的 compose 目录都叫 `docker`，缺省项目名会撞车，
> 起 service 的 `redis` 会把 DeepDocParse-Web 那个 redis 容器直接顶掉。
> 本仓库的卷只有 `redis-data`（解析结果 24h 暂存 + 可重建的向量索引缓存），
> 丢了重新解析即可，不需要搬；要保留就用 `-p docker` 保持旧项目名。
> 三份 compose（dev / cpu / prod-nvidia）共用 `ddp-service`：它们绑同一批端口，
> 本来就是互斥的部署形态，切换时加 `--remove-orphans` 清掉上一套的残留容器。

> 不想下权重（约 2GB）：把 `compose.cpu.yml` 的 `embed` 服务和 `models.cpu.yaml` 的
> `embedding_models` 一起注释掉即可 —— 注册表里没有 embedding 段时，
> service 的检索链自动退回 BM25（注册表驱动的开关，不用改代码）。
> **但 DeepDocParse-Web 的索引会因此失败**：产品层的问答依赖向量索引，
> 那条路走不通就不能问答。只用 service 的解析/MCP 平面时才建议这么省。

这套配置用 **born-digital 引擎**（`pypdfium2` 直接抽 PDF 文字层 + 坐标，进程内跑、
不下模型、不要显卡），出处三件套（页码 / bbox / 区域截图）一样齐全。

**它覆盖什么**：有文字层的 PDF —— 论文、报告、合同这一类。
**不覆盖什么**（刻意不扩张）：扫描件、表格结构、公式。这些要 OCR，请启用 mineru
（`docker/compose.cpu.yml` 与 `models.cpu.yaml` 里各有一段注释掉的配置，CPU 上很慢）。

**没有 VQA**：视觉验证做不了。这不是残缺 —— 回答会照常给出，并带
`degraded="vision_unavailable"` 标记，用户看得见"这条没做视觉验证"。
七种降级本来就是按"视觉模型不可用时纯文本作答并打标"设计的。

有 GPU 时换成完整配置（MinerU pipeline + DeepSeek-OCR-2 + 指令模型 + TEI）：

```bash
docker compose -f compose.dev.yml --env-file ../.env up -d --build
```

## 配置

全部 gateway 配置见 [docs/CONFIG.md](docs/CONFIG.md)（由 `scripts/gen_config_docs.py`
从 `gateway/app/config.py` 生成，CI 会检查它有没有过期）。
改了配置项的注释后重跑一次即可刷新：

```bash
python scripts/gen_config_docs.py
```

## 开发里程碑

- [x] M1 解析平面：gateway + MinerU pipeline + ARQ 归档链（mineru 3.4.4 实测契约见 docs/mineru-api-contract.md）
- [x] M2 VQA 平面：deepseek-ocr.rs 接入（dev 用 Windows 原生二进制 v0.6.0 + ModelScope 自动下权重，见 models.dev-host.yaml；prod 用 vLLM 容器）
- [x] M3 MCP（历史）：`ask_document` v1；阶段 7 后仅作兼容入口
- [x] M4a embedding v2 + metrics + prod compose 锁版本：`/v1/embeddings` 透传、结构感知分块、
      bge-m3(TEI) 向量化、Redis Stack 向量检索（BM25 自动兜底）、Prometheus `/metrics`
      —— dev 全链路真机验证（`scripts/e2e_mcp.py`）
- [ ] M4b 压测 + 多卡 mineru-router 验证（需服务器，dev 机 8GB 做不到）
- [x] M5 契约冻结 v1.0：`/v1/parse` 增加可选 `doc_id`（稳定文档标识，ADR #11）后冻结 openapi.yaml；
      与 DeepDocParse-Web 全链路联调通过（见 ../DeepDocParse-Web/scripts/e2e_web.py）
- [x] M9 结构化抽取平面（plan-v2.md 全组）：
  - [x] **DDP-Extract v1 契约**（[docs/extract-format.md](docs/extract-format.md)）：字段三态
        found / not_found / error 必须分开，`schema_violation` 是第八种降级
  - [x] `/v1/extract` 抽取平面 + `/v1/rerank` 精排（openapi.yaml v1.1，纯新增端点）
  - [x] **块类型进版面契约**（DDP-Layout v1.1）：`para_blocks[].type` + 可选 `table_html`。
        顺带堵掉一个静默丢数据的洞 —— `block_text` 以前只读 `lines`，
        **mineru 表格块的内容全在嵌套 `blocks` 里，整张表的文字在分块阶段被丢弃**，
        表格解析出来了、索引里却没有，全程无报错
  - [x] **vlm-ocr 引擎**：视觉语言模型整页识别，「基于大语言模型的识别」这条线的落点。
        没有改动 engines.py 的任何既有代码——第三个引擎复验了"加引擎 = 加容器 + 一行配置"
  - [x] **识别质量评测**（[docs/EVAL-ocr.md](docs/EVAL-ocr.md) / `scripts/eval_ocr.py`）：
        文本编辑距离 + 表格单元格 F1，可接 OmniDocBench。
        **真值来自生成 PDF 的源文本**，不是解析器输出（否则纯属自我印证）
  - [x] 就绪探针按 `runtime` 而不是段名推断路径 —— vlm-ocr 挂在 parse_engines 段却说
        OpenAI 协议，按段名推断会去打不存在的 `/health`，把健康容器报成 down
  - [ ] **真机 e2e 待 GPU 服务器**：本机无 GPU，mineru / vlm-ocr / VQA 一次都没跑过
  - ⚠️ **已知限制（升级到 M9 后）**：分块规则变了（表格/公式/图片独立成块、标题作前缀），
        对**老文档**重建索引会切出不同的 `seq`。历史出处不会指错地方——
        `attach_resolution` 会比对内容，对不上就标"出处已失效"——
        但那些出处确实**接不回去了**。要保住历史问答/抽取的可追溯性，
        升级后不要对老文档点重建索引；确需重建的，先导出一份结果
- [x] M7 可发布度（plan.md 的 B/C/E 组）：
  - [x] B1 版面中间表示升格为契约（[docs/layout-format.md](docs/layout-format.md)）+ 显式 normalizer 层
  - [x] B2 born-digital 兜底引擎 + `compose.cpu.yml`：**无 GPU 全链路可跑**，
        顺带把"加引擎 = 加容器 + 一行配置"用第二个引擎验证了一遍。
        **这条当初打勾打早了**：引擎能跑，但缺省引擎名在路由层写死成 mineru，
        `default: true` 被架空，缺省请求在这套注册表上必然 404 —— 直到第一次
        真跑才发现。现已改成由注册表决定（见 git log）
  - [x] B3 配置参考文档自动生成（[docs/CONFIG.md](docs/CONFIG.md)），CI 检查是否过期
  - [x] C1/C2 Apache-2.0 + MinerU 归属（README / NOTICE / 产品界面页脚三处）
  - [x] C3/C4 GitHub Actions + **契约守卫**：`openapi.yaml` 与 gateway 端点不一致即红
        （上线第一天就抓到 openapi.yaml 不是合法 YAML —— 从没有人机械校验过它）
  - [x] C5 英文 README（[README.en.md](README.en.md)）+「明确不做」写进 README
  - [x] E 注册表能力显式化：`runtime` / `capabilities` / `adapter`（纯增量，老配置照跑）
- [x] 重构阶段 5/6：DDP-Layout v1.2 编译层、视觉原子与 Deep Agent 断言/核对链；
      本机契约与离线评测已通过，live 质量数字待 GPU 批次二
- [x] 重构阶段 7（代码完成）：DDP-Graph v1、STORM 两阶段 Wiki、复核队列，以及
      `search` / `ask` / `get_evidence` / `read_wiki` / `graph_neighbors` 五个语料级 MCP 工具；
      MCP 直接读持久化语料，返回 evidence、页码、bbox 与原件裁图。live 数字待 GPU 批次三

## 明确不做

比功能列表更能说明这个项目是什么。每条都附了触发条件——条件不满足就不做，也不重新论证。

| 不做 | 谁有 | 什么情况下会重新考虑 |
|---|---|---|
| 连接器（网盘/知识库/邮箱） | Onyx、WeKnora | 文档来源不在本系统内。上传够用 |
| 多渠道 IM 投放 | WeKnora | 永不进主仓（可做外围项目） |
| 工作流编排 | Dify | 永不——那是另一个品类 |
| Agentic 多跳检索 | RAGFlow、WeKnora | 出现跨文档比较/综合用例。单文档问答天然单轮 |
| 权限 / RBAC | Onyx、WeKnora | 目标形态改为企业内部多用户交付 |
| ColPali 视觉检索 | Morphik | 评测证明视觉检索确有差距（另注：pgvector 不支持多向量 MaxSim）|
| 分块的**人工编辑** | RAGFlow | 永不——单人维护 + 有评测集时，该调的是分块器，不是让用户手工修。只保留只读的边界可视化 |
| LoRA 训练 / 参数化记忆 | — | 违反下面那条贯穿性准则 |
| 重写 MinerU | — | 永不。保留成本 = 一行署名；替代成本 = 训 5 个模型。不成比例 |
| 泛格式支持（音视频/邮件） | Docling | 泛格式 ≠ 版面理解，是两件事 |
| 端到端「文档 → JSON」黑箱抽取 | 多数抽取产品 | **永不**——出处链断裂，撞下面那条贯穿性准则（ADR #19） |
| 抽取工作流编排（多步/条件分支） | Dify | 永不——那是另一个品类。`/v1/extract` 只做单一操作的批量化 |
| 抽取结果的人工编辑 | 多数抽取产品 | 永不——与"分块的人工编辑"同理由：该调的是抽取器 |

> **贯穿性准则：凡是把信息压进权重或潜空间的方案，都与"可验证出处"冲突。**
> 它们只能用在出处已经确定之后的终端环节（例如"读这张区域图并回答"），不得进入定位链路——
> 信息一旦进了潜空间就指不回原文的哪个 bbox 了。

## 原则备忘

1. **不重写 mineru-api/router** —— 官方成品，配置启动；gateway 只做协议转换/鉴权/归档/metrics
2. **不双重排队** —— 推理排队归 mineru 任务管理，ARQ 只管编排与后处理
3. **注册表驱动** —— gateway 不 import 任何模型代码，只查 models.yaml 转发
4. **锁版本 + 契约测试** —— mineru/deepseek-ocr.rs 镜像 pin 版本，升级前跑 tests/
5. **状态边界明确** —— gateway 的任务结果暂存 24h；语料、证据与知识层持久化在
   PostgreSQL/MinIO；向量索引是可重建缓存

## 许可

[Apache-2.0](LICENSE)。第三方组件与归属声明见 [NOTICE](NOTICE)。

**MinerU 归属（不可省略）**：本项目的文档解析能力由
[MinerU](https://github.com/opendatalab/MinerU) 提供。MinerU 在 Apache-2.0 之上有附加条款，
其中 §2 要求基于它提供**在线服务**的产品必须在界面或公开文档中清晰显著地标明使用了 MinerU，
§3 规定违反即自动终止许可（无需通知）。本项目正是这样的在线服务，因此该声明同时出现在
README 顶部、NOTICE 与产品界面页脚，请勿删除。
