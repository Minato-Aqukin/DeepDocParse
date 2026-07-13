# DeepDocParse-Web 开发指南

## 必读上下文
- 总体架构：`../ARCHITECTURE.md`；本仓库说明：`README.md`
- 对 DeepDocParse 的调用**只依赖** `../DeepDocParse/openapi.yaml` 契约，禁止依赖其内部实现

## 职责边界
- 用户/API key/额度限流/计量全在本层；对 service 统一用 SERVICE_TOKEN
- 文件与解析结果的**永久存储**在本层（MinIO + PostgreSQL）；service 结果暂存仅 24h，
  收到完成回调必须及时取回归档
- `/v1/*` 对外 API 与 `/mcp` 反代：验 key → 额度 → 转发 → 记 usage（注意 SSE 流式透传）

## 技术约定
- backend：FastAPI + SQLAlchemy(async) + asyncpg + Alembic 迁移；JWT 用 jose，密码 bcrypt
- API key：sk- 前缀，明文只在创建时返回一次，库存哈希
- frontend：用 `npm create vue@latest` 生成（TS/Router/Pinia），不手写工程配置

## 开发时机
本仓库大部分工作排在 M5 联调阶段；此前只需保证契约对齐。
