# DeepDocParse 开发指南

## 必读上下文
- 总体架构与决策记录：`../ARCHITECTURE.md`（10 条 ADR，勿违背）
- 本仓库说明：`README.md`；对外契约：`openapi.yaml`；模型注册表：`models.yaml`

## 铁律
1. **不重写 mineru-api / mineru-router**——官方成品，配置启动；gateway 只做协议转换/鉴权/归档/metrics
2. **不双重排队**——推理排队归 mineru 任务管理，ARQ 只管后处理编排（poll → 归档 → 回调）
3. **注册表驱动**——gateway 不 import 模型代码，只查 models.yaml 转发；加模型=加容器+一行配置
4. **契约优先**——改 `/v1/*` 接口必须先改 openapi.yaml；mineru/deepseek-ocr.rs 升级前必须先跑绿 tests/
5. **无状态**——结果暂存 TTL=24h；永久归档是 DeepDocParse-Web/backend 的事；向量索引是可重建缓存
6. **MCP 只有一个工具** `ask_document`，签名永不变，检索升级只改内部实现

## 开发顺序（按 TODO(Mx) 标注走）
M1 解析平面 → M2 VQA 平面 → M3 MCP ask_document v1(BM25) → M4 prod+embedding v2 → M5 契约冻结

M1 第一步必须先拿到 mineru-api 的真实接口：启动官方容器后访问其 /docs，
把 /tasks 相关参数记录到 `docs/mineru-api-contract.md`，再实现 mineru_client.py。

## 技术约定
- Python 3.11+，全 async，类型标注；FastAPI + httpx(复用 AsyncClient) + redis.asyncio + ARQ
- 错误格式统一 OpenAI 风格 `{"error": {"message", "type", "code"}}`
- 镜像版本一律 pin，禁止 latest（compose 里的 PIN_VERSION 占位需替换为具体版本）

## 验证
- 单测/契约测试：`pytest tests/`（respx mock 上游，或 dev compose 起真环境）
- 本地起服务：`cd docker && docker compose -f compose.dev.yml --env-file ../.env up --build`
- 显存注意：dev 机只有 8GB，mineru(pipeline) 与 vqa-dsocr 不要同时启动
