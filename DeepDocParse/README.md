# DeepDocParse

[English](README.en.md) · Apache-2.0 · **文档解析由 [MinerU](https://github.com/opendatalab/MinerU) 提供支持**

多模态文档理解服务层。无状态、GPU 部署，对外三平面：

| 平面 | 接口 | 引擎 |
|------|------|------|
| 解析 | `POST /v1/parse`（异步任务） | MinerU（官方 mineru-api / mineru-router，不重写） |
| VQA | `POST /v1/chat/completions`（OpenAI 协议） | DeepSeek-OCR |
| MCP | `ask_document` 单一复合工具 | 编排层（解析缓存 → 检索 → bbox 裁剪 + VQA） |

架构决策见根目录 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

## 这个项目想成为什么

**「可验证出处」的参考实现，而不是一个更小的 RAGFlow。**

这个类别里所有项目都宣称支持 grounded citation，但没有一个公开基准去度量 bbox 级出处的正确性——
于是"支持"这件事从来没被证明过。本项目把赌注全押在这一件事上：

- **出处三件套**：每个回答的每条依据都带页码 + bbox + 从原件裁出来的区域截图，点得开、对得上
- **视觉验证**：裁出来的图会再喂给视觉模型核对一遍，而不是只信文本相似度
- **降级必须可见**（架构级铁律）：检索零命中、向量化挂了、视觉模型不可用、裁不出图、
  解析本身可疑——每一种都在回答上打标，绝不静默退化。这个项目吃过静默降级的大亏
  （M4a 的向量检索悄悄退回 BM25，很久没人发现）

功能数量上追 RAGFlow / Docling / WeKnora 是必输的，追上了也不会有人因此选择本项目。
所以有一份同样重要的[**明确不做**](#明确不做)清单。

## 目录结构

```
DeepDocParse/
├── openapi.yaml            # 与 DeepDocParse-Web/backend 的接口契约（唯一依赖面）
├── models.yaml             # 模型注册表：加模型 = 加容器 + 一行配置
├── gateway/                # 唯一自研服务：薄适配层
│   └── app/
│       ├── main.py         # FastAPI 入口
│       ├── config.py       # 配置 + 注册表加载
│       ├── auth.py         # service token 校验
│       ├── routers/        # parse / chat / health
│       ├── services/       # mineru_client / vqa_client / task_store
│       └── worker/         # ARQ 任务：结果归档链（v2 追加向量化步骤）
├── mcp_server/             # FastMCP：ask_document
├── docker/
│   ├── compose.dev.yml     # RTX 4060 8GB：MinerU pipeline（VQA 走宿主机原生二进制，见 models.dev-host.yaml）
│   └── compose.prod-nvidia.yml  # RTX 6000 级：vLLM + mineru-router 多卡
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

有 GPU 时换成完整配置（MinerU pipeline + DeepSeek-OCR + TEI）：

```bash
docker compose -f compose.dev.yml --env-file ../.env up -d --build
```

## 配置

全部 12 项配置见 [docs/CONFIG.md](docs/CONFIG.md)（由 `scripts/gen_config_docs.py`
从 `gateway/app/config.py` 生成，CI 会检查它有没有过期）。
改了配置项的注释后重跑一次即可刷新：

```bash
python scripts/gen_config_docs.py
```

## 开发里程碑

- [x] M1 解析平面：gateway + MinerU pipeline + ARQ 归档链（mineru 3.4.4 实测契约见 docs/mineru-api-contract.md）
- [x] M2 VQA 平面：deepseek-ocr.rs 接入（dev 用 Windows 原生二进制 v0.6.0 + ModelScope 自动下权重，见 models.dev-host.yaml；prod 用 vLLM 容器）
- [x] M3 MCP：ask_document v1（BM25 检索 + bbox 裁剪 VQA 验证 + 带出处返回；解析中即返回重试模式）
- [x] M4a embedding v2 + metrics + prod compose 锁版本：`/v1/embeddings` 透传、结构感知分块、
      bge-m3(TEI) 向量化、Redis Stack 向量检索（BM25 自动兜底）、Prometheus `/metrics`
      —— dev 全链路真机验证（`scripts/e2e_mcp.py`）
- [ ] M4b 压测 + 多卡 mineru-router 验证（需服务器，dev 机 8GB 做不到）
- [x] M5 契约冻结 v1.0：`/v1/parse` 增加可选 `doc_id`（稳定文档标识，ADR #11）后冻结 openapi.yaml；
      与 DeepDocParse-Web 全链路联调通过（见 ../DeepDocParse-Web/scripts/e2e_web.py）
- [x] M7 可发布度（plan.md 的 B/C/E 组）：
  - [x] B1 版面中间表示升格为契约（[docs/layout-format.md](docs/layout-format.md)）+ 显式 normalizer 层
  - [x] B2 born-digital 兜底引擎 + `compose.cpu.yml`：**无 GPU 全链路可跑**，
        顺带把"加引擎 = 加容器 + 一行配置"用第二个引擎验证了一遍
  - [x] B3 配置参考文档自动生成（[docs/CONFIG.md](docs/CONFIG.md)），CI 检查是否过期
  - [x] C1/C2 Apache-2.0 + MinerU 归属（README / NOTICE / 产品界面页脚三处）
  - [x] C3/C4 GitHub Actions + **契约守卫**：`openapi.yaml` 与 gateway 端点不一致即红
        （上线第一天就抓到 openapi.yaml 不是合法 YAML —— 从没有人机械校验过它）
  - [x] C5 英文 README（[README.en.md](README.en.md)）+「明确不做」写进 README
  - [x] E 注册表能力显式化：`runtime` / `capabilities` / `adapter`（纯增量，老配置照跑）

## 明确不做

比功能列表更能说明这个项目是什么。每条都附了触发条件——条件不满足就不做，也不重新论证。

| 不做 | 谁有 | 什么情况下会重新考虑 |
|---|---|---|
| GraphRAG / RAPTOR | RAGFlow、WeKnora | 评测集显示跨块综合类问题占比显著 |
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

> **贯穿性准则：凡是把信息压进权重或潜空间的方案，都与"可验证出处"冲突。**
> 它们只能用在出处已经确定之后的终端环节（例如"读这张区域图并回答"），不得进入定位链路——
> 信息一旦进了潜空间就指不回原文的哪个 bbox 了。

## 原则备忘

1. **不重写 mineru-api/router** —— 官方成品，配置启动；gateway 只做协议转换/鉴权/归档/metrics
2. **不双重排队** —— 推理排队归 mineru 任务管理，ARQ 只管编排与后处理
3. **注册表驱动** —— gateway 不 import 任何模型代码，只查 models.yaml 转发
4. **锁版本 + 契约测试** —— mineru/deepseek-ocr.rs 镜像 pin 版本，升级前跑 tests/
5. **无状态** —— 结果暂存 24h（Redis/本地盘），永久归档在 backend；向量索引是可重建缓存

## 许可

[Apache-2.0](LICENSE)。第三方组件与归属声明见 [NOTICE](NOTICE)。

**MinerU 归属（不可省略）**：本项目的文档解析能力由
[MinerU](https://github.com/opendatalab/MinerU) 提供。MinerU 在 Apache-2.0 之上有附加条款，
其中 §2 要求基于它提供**在线服务**的产品必须在界面或公开文档中清晰显著地标明使用了 MinerU，
§3 规定违反即自动终止许可（无需通知）。本项目正是这样的在线服务，因此该声明同时出现在
README 顶部、NOTICE 与产品界面页脚，请勿删除。
