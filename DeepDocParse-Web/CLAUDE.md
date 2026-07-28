# DeepDocParse-Web 开发指南

## 必读上下文
- 总体架构：`../ARCHITECTURE.md`（ADR #11–#16 是本层的设计前提）；本仓库说明：`README.md`
- 对 DeepDocParse 的调用**只依赖** `../DeepDocParse/openapi.yaml` 契约，禁止依赖其内部实现
- **下一轮设计已定稿在 `docs/DESIGN.md`**（M6：Web 端问答、Document/ParseJob 拆分、多副本）。
  改数据模型、加检索、动前端结构之前先读它——尤其 §2.5（pgvector 与 SQLite 单测并存）

## 职责边界
- 用户/API key/额度限流/计量全在本层；对 service 统一用 SERVICE_TOKEN，service 不感知用户
- 文件与解析结果的**永久存储**在本层（MinIO + PostgreSQL）；service 结果暂存仅 24h
- `/v1/*` 对外 API 与 `/mcp` 反代：验 key → 限速 → 额度 → 转发 → 记 usage

## 铁律
1. **不 import service 的代码，也尽量不给 service 加东西**。需要同样行为的地方（OpenAI 错误体、
   SSE 逐跳头过滤、结构感知分块）在本仓库各写一份。耦合面只有两处：解析契约 openapi.yaml，
   以及 OpenAI 兼容的 embedding/chat 端点（可配成任意兼容服务）
2. **数据留在本层**。分块、向量索引、问答会话全在 Postgres；分块的输入是本层归档的
   `layout.json`，不依赖 service 的 24h 暂存窗口
3. **降级必须可见**。检索零命中/视觉模型不可用/不能裁剪，都要落到 `messages.degraded`
   并在 UI 上打标——静默降级是这个项目吃过大亏的地方（M4a 悄悄退回 BM25）
4. **回调不可信**。归档必须同时有回调路径与对账路径，且 `archive_job` / `index_document`
   都用 claim 做成幂等（多副本天然安全）
5. **流式响应里不能用请求作用域的 DB session**。它在响应体开始流之前就关了——
   问答落库、计量都必须另开 session（`get_sessionmaker()`），这条踩过两次
6. **给 service 的 URL 必须稳定**（`/files/{token}`），不用预签名——URL 一变，
   service 的幂等与向量索引全部失效。另注意 `/files` 要按 MIME 白名单决定 inline/attachment，
   否则上传 text/html 就是本站同源 XSS
7. **httpx 一律 `trust_env=False`**，本机 SOCKS 代理会污染 localhost 调用

## 技术约定
- backend：FastAPI + SQLAlchemy 2.0(async) + asyncpg + Alembic；JWT 用 jose，密码 bcrypt，
  API key 用 sha256（每请求都要验，bcrypt 会压垮代理路径）
- 模型只用可移植类型（String/JSON/DateTime），不用 PG 专有的 UUID/JSONB —— 单测因此能跑 SQLite。
  **向量列走 `app/types.py::Vector`**（PG 上是 pgvector，其它方言退回 JSON），
  检索本身抽在 `app/search.py::SearchIndex` 协议后面：生产 `PgVectorIndex`，单测 `MemoryIndex`。
  这两处是"单测不需要任何外部依赖"的命门，别绕过去直接写 SQL
- 库里读出的时间要过 `models.as_aware()` 再和 `utcnow()` 比（SQLite 存 naive，PG 存 aware）
- frontend：Vue3 + TS + Router + Pinia + Element Plus；解析结果渲染**必须过 DOMPurify**
  （文档内容是不可信输入）

## 本机陷阱
1. **端口**：MinIO 用 19000/19001（9000 被 gateway 占），PG 15432，Redis 16379，前端 5173。
   **Windows 保留段会漂移**——重启 WSL 后重新分配，实测出现过 8079–8178 覆盖 8080，
   uvicorn 直接 `WinError 10013`。先查 `netsh interface ipv4 show excludedportrange protocol=tcp`，
   落在段里就换端口（真机验证时用过 18888），同时改 `PUBLIC_BASE_URL` 与前端的
   `VITE_API_TARGET`，否则 service 回访与前端代理都会断
2. **内存装不下所有运行时**：mineru(GPU 常驻) + TEI + VQA(CPU) 同时开会把 Docker 引擎压挂
   （`docker ps` 卡住 / API 500）。真机 e2e 要分两段跑：
   `e2e_web.py --phase parse --user X`（要 mineru+TEI）→ 停 mineru、起 VQA →
   `--phase qa --user X`（要 VQA+TEI）。
   WSL 停容器后不还内存，必要时 `wsl --shutdown` 强制回收（实测能收回 ~6GB）
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
cd backend && ../.venv/Scripts/python -m pytest        # 62 例，纯进程内
cd frontend && npm run build                            # 含类型检查
REDIS_URL=redis://localhost:6379/0 .venv/Scripts/python scripts/e2e_web.py   # 真环境全链路
```
