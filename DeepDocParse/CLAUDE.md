# DeepDocParse 开发指南

## 必读上下文
- 总体架构与决策记录：`../ARCHITECTURE.md`（10 条 ADR，勿违背）
- 本仓库说明：`README.md`；对外契约：`openapi.yaml`；模型注册表：`models.yaml`

## 铁律
1. **不重写 mineru-api / mineru-router**——官方成品，配置启动；gateway 只做协议转换/鉴权/归档/metrics
2. **不双重排队**——推理排队归 mineru 任务管理，ARQ 只管后处理编排（poll → 归档 → 回调）
3. **注册表驱动**——gateway 不 import 模型代码，只查 models.yaml 转发；加模型=加容器+一行配置。
   - 适配器选择由注册表的 `runtime` 决定（`app/services/engines.py`），路由与 worker 都不认识任何具体引擎
   - **边界澄清（2026-08-18）**：`borndigital` 引擎跑在**进程内**，不是容器。
     这条铁律禁的是"import 模型代码"（权重、推理框架、CUDA 依赖那一整套），
     而 pypdfium2 是个 PDF 库 —— 无权重、无 GPU、无重依赖，且 mcp_server 早就在用它裁图。
     判断标准是**它会不会把模型运行时的依赖地狱和许可证传染带进 gateway**，不是"有没有容器"。
     真正的模型一律进容器，这条不变
4. **契约优先**——改 `/v1/*` 接口必须先改 openapi.yaml；mineru/deepseek-ocr.rs 升级前必须先跑绿 tests/。
   **openapi.yaml 已在 M5 冻结为 v1.0**：此后只许向后兼容的新增，不得删改既有字段语义
5. **无状态**——结果暂存 TTL=24h；永久归档是 DeepDocParse-Web/backend 的事；向量索引是可重建缓存
6. **MCP 只有一个工具** `ask_document`，签名永不变，检索升级只改内部实现

## 开发顺序（按 TODO(Mx) 标注走）
M1 解析平面 → M2 VQA 平面 → M3 MCP ask_document v1(BM25) → M4 prod+embedding v2 → M5 契约冻结（已完成）

M1 第一步必须先拿到 mineru-api 的真实接口：启动官方容器后访问其 /docs，
把 /tasks 相关参数记录到 `docs/mineru-api-contract.md`，再实现 mineru_client.py。

## 技术约定
- Python 3.11+，全 async，类型标注；FastAPI + httpx(复用 AsyncClient) + redis.asyncio + ARQ
- 错误格式统一 OpenAI 风格 `{"error": {"message", "type", "code"}}`
- 镜像版本一律 pin，禁止 latest（compose 里的 PIN_VERSION 占位需替换为具体版本）
- **SERVICE_TOKEN 是占位值时 gateway 拒绝启动**（`config.assert_secrets_configured`）。
  它是本服务唯一的鉴权凭据，留着 change-me 等于 /v1/* 无鉴权开放。
  一次性容器/CI 可用 `ALLOW_INSECURE_DEFAULTS=true` 显式跳过
- **队列水位是记名的在途集合**（`queue:inflight` zset），不是计数器。
  释放按 task_id 幂等，超过 `QUEUE_INFLIGHT_TTL` 的成员自动淘汰 ——
  worker 被杀导致的漏释放会自愈，不会把 /v1/parse 永久顶在 429 上
- **裁剪/渲染这类 CPU 活一律 `asyncio.to_thread`**：mcp_server 是单进程事件循环，
  同步跑整页渲染会让所有并发 ask_document 一起停摆

## 验证
```bash
cd gateway && ../.venv/bin/python -m pytest -q     # 52 例，respx mock 上游 + fakeredis，~1s
python scripts/check_contract.py                    # 契约守卫：openapi.yaml ←→ gateway 端点
python scripts/gen_config_docs.py --check           # 配置文档有没有过期
```
- **pytest 必须在 `gateway/` 下跑**：给它传 rootdir 之外的路径参数会让 rootdir 重新推断，
  `asyncio_mode` 退回 strict，所有 async 用例集体报 "requested an async fixture"
- 无 GPU 也能起全链路：`cd docker && docker compose -f compose.cpu.yml --env-file ../.env up -d --build`
- 有 GPU 的完整配置：`compose.dev.yml`。显存注意：8GB 机器上 mineru(pipeline) 与 vqa-dsocr 不要同时启动
