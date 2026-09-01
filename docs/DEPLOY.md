# 部署到服务器

一台干净的 Linux 服务器，ssh 上去三条命令：

```bash
git clone https://github.com/Minato-Aqukin/DeepDocParse-Web.git
cd DeepDocParse-Web
./init.sh docker --host <服务器公网IP或域名> --chat-url http://127.0.0.1:11434/v1 --chat-model qwen3:8b -y
```

初始化完成后运行 `./start.sh docker start`，再打开 `http://<你的IP>` 注册第一个账号。

`init.sh` 负责初始化，`start.sh` 负责运行；每一步都能单独重跑：

| 子命令 | 做什么 |
|---|---|
| `configure` | **环境配置**：clone service 仓库、生成两份 `.env`（随机密钥）、前端 `.env.local`、注册表、compose 覆盖层、nginx 配置 |
| `tune` | **优化配置**：按 CPU/内存/GPU 定并发、批量、上传上限（就是 `configure` 的同一条计算链） |
| `models` | **模型权重**：下 bge-m3，必要时转 safetensors；`--with-rerank` 时多下一份 reranker |
| `start` | **服务启动**：容器 → 数据库迁移 → backend → nginx |

其余：`deps`（装 docker/python/node）、`build`（venv + npm + 镜像）、`stop` / `restart` /
`status` / `logs <名>` / `doctor`（自检）、`systemd`（开机自启）。
全部选项见 `./init.sh docker help`。

> **选项会被记住**（存在 `.quickstart/state.env`）：`configure` 过一次之后，
> 后面的子命令不用再传一遍。反过来说，开关要关掉就得显式关 ——
> `--no-rerank` / `--no-mineru` / `--with-models`。

## 跑起来之后长什么样

```
                 :80 (nginx, network_mode host)
                  |  静态托管 frontend/dist + 反代 /api /files /v1 /mcp /internal
                  v
        127.0.0.1:8080  backend (uvicorn, 只监听回环)
                  |
      +-----------+-----------+------------------+
      v           v           v                  v
  PG 15432   MinIO 19000  Redis 16379     gateway 9000 (容器)
  (语料/知识)  (原件/裁图)   (多副本才起)          |
      ^           ^                          +-----+-----+
      |           |                          v           v
      +--- mcp-server（五个语料工具）    redis-stack   TEI 18080
                                             6379       (bge-m3)
```

**对外只开一个端口**。backend 绑 127.0.0.1，公网够不着；所有流量走 nginx。
前端是 hash 路由（`createWebHashHistory`）、axios 的 `baseURL` 是 `/`，
所以静态托管不需要任何 SPA 回退规则，也不存在跨域。

## 五件必须知道的事

### 1. `--host` 不能是 127.0.0.1

`PUBLIC_BASE_URL` 同时是三样东西的基地址：给 service 的稳定文件 URL、解析完成回调
（`/internal/parse-callback`）、前端拿到的原件链接。而 gateway 跑在容器里 ——
写回环地址的话它打到的是容器自己，解析结果永远回不来（只能靠 60s 一轮的对账兜底）。

脚本缺省取主网卡地址。域名 + NAT 的场景用 `--public-base-url` 显式指定一个
**服务器自己也能访问到**的地址。`./start.sh docker doctor` 会从 gateway 容器里
真的 curl 一次来验证这条链路。

### 2. 不配 `--chat-url` 就没有问答、抽取和知识生成

本层只要求一个 **OpenAI 兼容**的 chat 端点，不绑定任何具体部署（ADR #17）。
本地 Ollama / llama.cpp / 任意托管 API 都行：

```bash
./init.sh docker configure --chat-url http://127.0.0.1:11434/v1 --chat-model qwen3:8b
./start.sh docker restart
```

端点在宿主机上（`127.0.0.1`）时，脚本会自动把容器侧地址改写成
`host.docker.internal` 并给 gateway 加 `extra_hosts: host-gateway` ——
Linux 上不加这一条容器解析不了这个名字。

不配也能用：上传、解析、检索、出处三件套都不受影响；问答、抽取与图谱/Wiki 生成会
返回明确错误，已有知识的读取不受影响。

### 3. 解析引擎名有三处，必须一致

`.env` 的 `DEFAULT_PARSE_ENGINE`、`frontend/.env.local` 的 `VITE_DEFAULT_ENGINE`、
service 注册表里的引擎名。对不上时上传第一步就是 `404 unknown_engine`。
脚本把三处一起写，`doctor` 会逐个核对。

> 浏览器里的 `localStorage.ddp.pref.engine` 会盖过前端缺省值 —— 换过引擎的浏览器
> 要去设置页重选一次。

### 4. 一台机器只能跑一套

compose 项目名固定在仓库的 compose 文件里（`ddp-web` / `ddp-service`），**不是**按目录名取的。
另一份 checkout 在同一台机器上 `up` 一次，会把这一套的同名容器直接换掉、数据卷也一并接管
（这个项目真发生过：起 service 的 redis 顶掉了 Web 的 redis，backend 当场失联）。

`start` 之前会读容器上的 compose 标签认出这种情况并要求确认，**非交互时直接中止**。
要并存就先停掉另一套：`docker compose -f <那边的 compose.web.yml> down`。

### 5. MCP 五工具直接读语料库

`search` / `ask` / `get_evidence` / `read_wiki` / `graph_neighbors` 不再走旧
`ask_document` 的 Redis 文档别名，而是从同一套 PostgreSQL/MinIO 读取 evidence、bbox、
图谱与 Wiki；`get_evidence` 还会返回原件裁图。quickstart 生成的 service 配置会把
`CORPUS_DATABASE_URL` 与 `CORPUS_MINIO_*` 指向本机数据面。数据库或对象存储不可达时，
工具会显式失败，不会静默退回另一份索引。

`/mcp` 仍经 nginx → Web backend 做 API key 鉴权，再反代到语料旁的 mcp-server；
不要把 mcp-server 的内部端口直接暴露到公网。

## 硬件与调参

`configure` 按内存分三档写配置。这套系统里几乎每个上限都是**内存**约束而不是算力约束：
上传体要整个进内存算 sha256，TEI 按 `--max-batch-tokens` 预热（bge-m3 支持 8192 上下文，
不限住会直接 OOM）。

| | < 6GB | 6–14GB | > 14GB |
|---|---|---|---|
| backend workers | 1 | 2 | 4（且不超过核数的一半） |
| 上传上限 | 50MB | 100MB | 200MB |
| embedding 批 | 8 | 16 | 24 |
| TEI `--max-batch-tokens` | 2048 | 4096 | 8192 |
| 抽取并发（字段 × 文档） | 2 × 1 | 4 × 2 | 6 × 3 |
| 解析队列上限 | 50 | 100 | 200 |

两条硬联动，不是随便调的：

- **`EMBEDDING_BATCH_SIZE` 必须严格小于 TEI 的 `--max-client-batch-size`**，
  否则长文档整批被拒 413。
- **backend workers > 1 就必须有 Redis**。限速令牌桶与对账选主都要跨进程共享，
  否则限速变成"每个 worker 各限各的"、对账每个 worker 都跑一遍。
  脚本据此自动起 Web 侧的 redis 并写 `REDIS_URL`。

探到 NVIDIA 卡时自动切 `--profile gpu`：TEI 换 GPU 镜像。
要 OCR / 表格 / 公式再加 `--with-mineru`（镜像构建很久，且必须有 GPU）。

## 模型权重

`models` 子命令下 `BAAI/bge-m3` 到工作区的 `models/bge-m3`（与两个仓库同级 ——
service 的 compose 里挂载路径写死了 `../../models`）。

优先走 ModelScope，失败回落 hf-mirror，两条都支持断点续传。
**TEI 只认 safetensors 而官方只发 `.bin`**，所以下完还要转一次：脚本会另找一个
3.11~3.13 的解释器建独立环境装 CPU 版 torch（torch 的 wheel 一直落后解释器一两个小版本，
系统 python 是 3.14 的话装不上）。bge-m3 走仓库自带的 `prepare_bge_m3.py`，
它额外校验 sha256 —— 多来源续传能拼出大小对但内容错的文件。

不想下（约 2.3GB 下载 / 6GB 峰值磁盘）就加 `--skip-models`：
注册表里不写 embedding 段，检索退回关键词路。
**代价是问答不可用**，Web 会把 `index_status` 标成 `failed` 并写 `index_error` ——
这是可见降级，不是故障。

## 开机自启

容器都带 `restart: unless-stopped`，重启机器会自己回来；**宿主上的 backend 不会**。
要它也自动起来：

```bash
./init.sh docker systemd     # 需要 root/sudo
```

装完之后 `start` / `stop` / `status` / `logs backend` 会自动改走 `systemctl`
（不然脚本会再起一个 uvicorn 跟它抢 8080）。日志去 `journalctl -u ddp-web -f`。

不装也没关系：脚本起 backend 时套了一层 `nohup`，ssh 退出登录不会把它带走。

## 排查

```bash
./start.sh docker doctor          # 密钥 / 引擎名一致性 / 容器回访 / 探针
./start.sh docker status          # 容器、进程、各层探针
./start.sh docker logs backend    # 也可以 gateway | arq-worker | embed | postgres | edge
```

`doctor` 覆盖的都是这个项目**真踩过**的坑：占位密钥（两个仓库的 config 都会拒绝启动）、
两侧 `SERVICE_TOKEN` 不一致（所有转发 401）、引擎名三处不一致（上传即 404）、
`PUBLIC_BASE_URL` 写成回环（回调永远回不来）。

几个常见现象：

| 现象 | 多半是 |
|---|---|
| `gateway /readyz` 恒 503 | 注册表里登记了没起的容器。`curl -s :9000/readyz` 看哪一项 down |
| 上传 404 `unknown_engine` | 引擎名三处不一致，跑 `doctor` |
| 解析一直 pending，60s 后才好 | 回调打不回来，走的是对账兜底；跑 `doctor` 看容器回访那一项 |
| 问答答不出且标 `embedding_unavailable` | 权重没下或 TEI 没起 |
| 问答报 chat 相关错误 | 没配 `--chat-url` |
| 关键词检索突然变差 | 分词器实现变了（jieba 装没装），**换实现后必须重建索引** |

## 更新到新版本

```bash
git pull && ./init.sh docker build && ./start.sh docker restart
```

`configure` 重跑是安全的：已有的密钥（`JWT_SECRET` / `SERVICE_TOKEN` / 数据库与
MinIO 凭据）原样保留，只刷新脚本管的那些键。`JWT_SECRET` 变了等于全员掉线，
所以它绝不会被自动换掉。

升级会运行到迁移 `0012`，新增知识层六张表。空知识库可 downgrade；一旦已有图谱、Wiki
或复核数据，迁移会拒绝破坏性 downgrade 并报告各表行数。

> **从 M9 之前的版本升上来时不要对老文档点重建索引**：分块规则变了，
> 老出处接不回去（不会指错地方，`attach_resolution` 会比对内容并标失效）。

## 数据与卸载

数据全在三个 docker 卷里：`ddp-web_pgdata`（Postgres）、`ddp-web_miniodata`（原件与
解析结果）、`ddp-service_redis-data`（24h 暂存，丢了重新解析即可）。
`./start.sh docker stop` 只停容器，卷不动。

```bash
# 真要清空（不可逆）
./start.sh docker stop
docker volume rm ddp-web_pgdata ddp-web_miniodata ddp-service_redis-data
```

## 脚本生成了哪些文件

| 路径 | 是什么 | 进 git 吗 |
|---|---|---|
| `.env` / `../DeepDocParse/.env` | 两侧配置与密钥（chmod 600） | 否 |
| `frontend/.env.local` | 前端构建期变量 | 否 |
| `.quickstart/models.registry.yaml` | service 注册表（只登记真的起了的容器） | 否 |
| `.quickstart/compose.service-override.yml` | 叠在 service compose 上的调参层 | 否 |
| `.quickstart/nginx.conf` | 边缘层配置 | 否 |
| `.quickstart/ddp-web.service` | systemd 单元（`./init.sh docker systemd` 才会装到系统里） | 否 |
| `.quickstart/state.env` | 上次的部署参数 | 否 |
| `.quickstart/logs/` `.quickstart/run/` | 日志与 pid | 否 |

**注册表由脚本生成而不是直接用仓库里的 `models.cpu.yaml`**：gateway 的 `/readyz`
是 `all(up)`，注册了却没起对应容器会让探针恒 503。谁被登记完全取决于这次部署
真的起了什么（`--skip-models` 就不写 embedding 段，`--with-rerank` 才写 rerank 段）。
