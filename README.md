# DeepDocParse

面向技术手册与论文的**多模态 PDF 检索工作台**。三条系统级属性：
**可追溯 / 可复核 / 可更新**；一句话约束：**答案必须回到证据**。

> 本仓库由原 `DeepDocParse`（GPU 服务层）与 `DeepDocParse-Web`（产品层）
> 两个仓库合并而成，两段 Git 历史完整保留。合仓与企业化重构方案见
> `MERGE-REFACTOR-PROPOSAL.md`。
>
> **重构做到哪一步、还欠什么，看 `docs/refactor/STATUS.md`。**

## 目录

```
apps/web/                 Vue 3 + Vite + TypeScript 前端
services/
  control-api/            Go：组织 / 鉴权 / API key / 配额 / 限速 / 计量 / 审计 / 统一入口
  corpus-api/             Python：文档 · evidence · citation · 检索 · 问答 · 抽取 · 知识
  model-gateway/          Python：注册表驱动的模型协议适配（mineru / VLM / TEI / rerank）
  corpus-worker/          Python：编译 / 索引 / 抽取的持久 worker
  mcp/                    Python：语料级 MCP 五工具
python/ddp_core/          两侧共用的语料纯逻辑（分块 / 裁图 / 检索 / 抽取 schema）
packages/
  contracts/              OpenAPI · JSON Schema · DDP-* 契约（Go/TS/Python 类型的唯一来源）
  sdk-ts/                 @deepdocparse/sdk
database/
  control/                Go 拥有的迁移
  corpus/                 Python/Alembic 拥有的迁移
eval/                     OCR / 出处 / 抽取 / Agent / 图谱评测
infra/                    compose · kubernetes · autodl · 镜像 · 模型注册表
tests/fixtures/           全仓共享的冻结夹具
```

## 四条产品不变式

1. 每条结论都能指回一个 bbox；指不回必须明确说明。
2. 任何降级都必须对 API、存储和 UI 可见。
3. 生成物与原文必须可区分，生成物引用最终仍指向原始原子 bbox。
4. 契约先于实现；先改 OpenAPI / DDP-* / MCP 文档，再改消费方。

## 四条企业化边界

5. 一个数据对象只能有一个写入所有者（见 `docs/refactor/DATA-OWNERSHIP.md`）。
6. 大文件不得完整进入应用进程内存，也不得由应用进程长期中转下载流量。
7. 进程重启不得使已受理任务永久丢失或永远停在运行态。
8. 多组织模式下每一次查询都必须有组织边界，不能只靠调用方自觉过滤。

## 快速开始

见 `docs/DEPLOY.md`。本机开发：

```bash
scripts/dev.sh secrets     # 生成 infra/env/dev.env（三个密钥随机填好，chmod 600）
scripts/dev.sh up          # 数据面 + 五个服务 + 两套迁移
scripts/dev.sh status
scripts/dev.sh logs corpus-api

cd apps/web && npm run dev # 前端 http://localhost:5173
```

入口是 http://127.0.0.1:8080 —— **只有它映射端口**。其余服务不做用户鉴权，
只信任入口下发的 actor 上下文头（有守卫钉着这件事）。

## 验证

```bash
./scripts/check.sh          # 全量门禁 20 项，与 CI 同一套判据
./scripts/check.sh guards   # 只跑守卫
```

单项：

```bash
cd python/ddp_core        && pytest -q      # 27
cd services/model-gateway && pytest -q      # 149 + 6 skip
cd services/corpus-api    && pytest -q      # 265
cd services/corpus-worker && pytest -q      # 10
cd services/mcp           && pytest -q      # 12
cd eval                   && pytest -q      # 45
cd services/control-api   && go test ./...  # 36
cd apps/web && npm run test:unit && npm run test:e2e   # 26 + 72
```

## 许可

见 `LICENSE` 与 `NOTICE`。
