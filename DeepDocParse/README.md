# DeepDocParse

多模态文档理解服务层。无状态、GPU 部署，对外三平面：

| 平面 | 接口 | 引擎 |
|------|------|------|
| 解析 | `POST /v1/parse`（异步任务） | MinerU（官方 mineru-api / mineru-router，不重写） |
| VQA | `POST /v1/chat/completions`（OpenAI 协议） | DeepSeek-OCR |
| MCP | `ask_document` 单一复合工具 | 编排层（解析缓存 → 检索 → bbox 裁剪 + VQA） |

架构决策见根目录 [../ARCHITECTURE.md](../ARCHITECTURE.md)。

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
└── tests/                  # 契约测试（mineru 升级前必须通过）
```

## 快速开始（dev, M1）

```bash
cp .env.example .env        # 填 SERVICE_TOKEN 等
cd docker
docker compose -f compose.dev.yml up --build
# gateway:    http://localhost:9000
# 契约文档:   http://localhost:9000/docs
```

## 开发里程碑

- [x] M1 解析平面：gateway + MinerU pipeline + ARQ 归档链（mineru 3.4.4 实测契约见 docs/mineru-api-contract.md）
- [x] M2 VQA 平面：deepseek-ocr.rs 接入（dev 用 Windows 原生二进制 v0.6.0 + ModelScope 自动下权重，见 models.dev-host.yaml；prod 用 vLLM 容器）
- [x] M3 MCP：ask_document v1（BM25 检索 + bbox 裁剪 VQA 验证 + 带出处返回；解析中即返回重试模式）
- [ ] M4 prod profile + metrics + 压测；embedding v2（bge-m3 + Redis Stack 向量检索）
- [ ] M5 契约冻结 v1.0，与 DeepDocParse-Web 联调

## 原则备忘

1. **不重写 mineru-api/router** —— 官方成品，配置启动；gateway 只做协议转换/鉴权/归档/metrics
2. **不双重排队** —— 推理排队归 mineru 任务管理，ARQ 只管编排与后处理
3. **注册表驱动** —— gateway 不 import 任何模型代码，只查 models.yaml 转发
4. **锁版本 + 契约测试** —— mineru/deepseek-ocr.rs 镜像 pin 版本，升级前跑 tests/
5. **无状态** —— 结果暂存 24h（Redis/本地盘），永久归档在 backend；向量索引是可重建缓存
