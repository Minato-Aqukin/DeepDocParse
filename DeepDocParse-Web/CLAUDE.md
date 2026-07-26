# DeepDocParse-Web 开发指南

## 必读上下文
- 总体架构：`../ARCHITECTURE.md`（ADR #11/#12/#13 是本层的设计前提）；本仓库说明：`README.md`
- 对 DeepDocParse 的调用**只依赖** `../DeepDocParse/openapi.yaml` 契约，禁止依赖其内部实现

## 职责边界
- 用户/API key/额度限流/计量全在本层；对 service 统一用 SERVICE_TOKEN，service 不感知用户
- 文件与解析结果的**永久存储**在本层（MinIO + PostgreSQL）；service 结果暂存仅 24h
- `/v1/*` 对外 API 与 `/mcp` 反代：验 key → 限速 → 额度 → 转发 → 记 usage

## 铁律
1. **不 import service 的代码**。需要同样行为的地方（OpenAI 错误体、SSE 逐跳头过滤）在本仓库
   各写一份，注释注明"形态对齐 gateway"——耦合面只有 openapi.yaml
2. **回调不可信**。归档必须同时有回调路径与对账路径，且 `archive_task` 幂等（状态机 claim）
3. **计量在转发前记**。流式响应的 relay 跑在响应体生成器里，那时 DB session 已关，
   在里面写库必炸（按页计量放在取结果时做，那是 JSON 可缓冲）
4. **给 service 的 URL 必须稳定**（`/files/{token}`），不用预签名——URL 一变，
   service 的幂等与向量索引全部失效
5. **httpx 一律 `trust_env=False`**，本机 SOCKS 代理会污染 localhost 调用

## 技术约定
- backend：FastAPI + SQLAlchemy 2.0(async) + asyncpg + Alembic；JWT 用 jose，密码 bcrypt，
  API key 用 sha256（每请求都要验，bcrypt 会压垮代理路径）
- 模型只用可移植类型（String/JSON/DateTime），不用 PG 专有的 UUID/JSONB —— 单测因此能跑 SQLite
- 库里读出的时间要过 `models.as_aware()` 再和 `utcnow()` 比（SQLite 存 naive，PG 存 aware）
- frontend：Vue3 + TS + Router + Pinia + Element Plus；解析结果渲染**必须过 DOMPurify**
  （文档内容是不可信输入）

## 本机陷阱
1. **端口**：MinIO 用 19000/19001（9000 被 gateway 占），PG 15432，backend 8080，前端 5173；
   Windows 保留段 7964–8063 一律避开
2. **`alembic.ini` 必须纯 ASCII** —— configparser 用系统 locale(GBK) 读它，中文注释直接崩
3. **改了 backend 代码要重启 uvicorn**，否则真机验证会验到旧代码。
   注意 `--reload` 模式下杀父进程**不会**释放端口：真正 listen 的是那个
   `multiprocessing.spawn` 子进程，父进程一死它就不再热重载，却继续用旧代码服务 8080。
   按命令行找 `python.exe ... spawn_main(parent_pid=<被杀的 pid>)` 一并杀掉。
4. Docker Desktop 偶尔会把 Linux 引擎跑挂（API 返回 500 / `docker ps` 卡住），
   重启 `D:\Docker\Docker Desktop.exe` 后轮询 `docker info` 等就绪
5. Git Bash 会把 `docker run` 里的 `/data/xxx` 改写成 Windows 路径，
   容器内路径要加 `MSYS_NO_PATHCONV=1`

## 验证
```bash
cd backend && ../.venv/Scripts/python -m pytest        # 34 例，纯进程内
cd frontend && npm run build                            # 含类型检查
REDIS_URL=redis://localhost:6379/0 .venv/Scripts/python scripts/e2e_web.py   # 真环境全链路
```
