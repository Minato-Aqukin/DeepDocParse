# 部署

## 一台机器，无 GPU（开发与小规模自部署）

```bash
scripts/dev.sh secrets      # 生成 infra/env/dev.env（三个密钥随机填好，chmod 600）
scripts/dev.sh up           # 起全栈
scripts/dev.sh status
scripts/dev.sh logs corpus-api
```

起来之后：

| 地址 | 是什么 |
|---|---|
| http://127.0.0.1:8080 | 统一入口（`/api` `/v1` `/mcp` `/files` `/healthz` `/readyz` `/metrics`） |
| http://127.0.0.1:19001 | MinIO 控制台 |
| http://localhost:5173 | 前端（`cd apps/web && npm run dev`） |

**只有入口映射了端口。** corpus-api / model-gateway / mcp 都只在内网可达 ——
它们不做用户鉴权，只信任入口下发的 actor 上下文头（见
`services/corpus-api/ddp_corpus/deps.py`）。把它们暴露到公网等于
任何人都能自称 admin。

无 GPU 档位注册的解析引擎是 `borndigital`（进程内抽 PDF 文字层与坐标，
出处三件套一样齐全；不处理扫描件、表格结构与公式）。

## 一台机器，有 N 卡

```bash
# infra/env/dev.env 里改成有 GPU 的注册表
sed -i 's/^MODELS_CONFIG=.*/MODELS_CONFIG=models.yaml/' infra/env/dev.env
scripts/dev.sh up --gpu
```

GPU 叠加层加的是模型运行时（mineru / DeepSeek-OCR-2 / Qwen3-Instruct /
TEI embedding / reranker），**应用侧五个服务一个字都不用改** —— 那正是
"注册表驱动"这条铁律的意义：加模型 = 加容器 + 注册表加一行。

两个 vLLM 共卡有两条硬约束，都写在 `infra/compose/compose.gpu.yml` 的注释里：

1. **不要用 `gpu-memory-utilization` 去分显存**。它算的是**整卡**已用，
   与启动前置检查叠加会把对方的占用扣两遍，怎么调都无解。
   正解是给第二个服务 `--kv-cache-memory-bytes` 直接写死 KV 大小。
2. **要钉"先后"，而"先起"不等于"先分配完"**。vLLM 要几分钟加载权重才真正
   吃显存，光按顺序敲命令等于两个进程并行 profiling —— 后分配的那个必定
   算出负 KV。所以用 `condition: service_healthy`（vLLM 的 `/health` 是
   引擎初始化完成后才开始监听的，200 ⟹ 显存已分配完）。

Arch 上的容器直通走 CDI，不再是 `--gpus`：

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

## 跑不了 docker 的机器（AutoDL 一类）

AutoDL 实例本身是非特权容器（无 `CAP_SYS_ADMIN`、`unshare --user` 返回
EPERM），dind / rootless / podman 全堵死。裸进程部署在 `infra/autodl/`。

架构没变 —— 网关只按注册表访问 HTTP 端点，endpoint 从容器名换成
`127.0.0.1:端口` 就行。同样没变的还有两个受限角色：长跑进程一律用
`ddp_control` / `ddp_corpus` 连库，超级用户只在迁移那一步出现。

```bash
# 本机先备好两样推不动就没法开始的东西：
#   1. 前端产物   cd apps/web && npm run build-only
#   2. Go 控制面  在目标机上现编（goproxy.cn 快），或本地交叉编译后 push
bash infra/autodl/stack.bash install     # apt + conda(3.12) + 四个服务包
bash infra/autodl/stack.bash migrate     # 两套迁移 + 授权
bash infra/autodl/stack.bash start       # PostgreSQL + 七个应用进程 + nginx
bash infra/autodl/stack.bash doctor      # 别跳过
```

配置全在脚本头部的 `${VAR:-默认值}`，外部 export 优先。**公网部署必须给的
只有两个**：`PUBLIC_HOST` 与 `PUBLIC_SCHEME` —— 预签名 URL 的签名覆盖 host，
稳定文件 URL 也要拼它。

### 下载源：这类机器上什么能用、什么不能用

实测（2026-09-03，AutoDL 北京 B 区）。**不要在不能用的源上重试** ——
它们是连不上，不是抖动：

| 源 | 结果 |
|---|---|
| `mirrors.aliyun.com`（golang / pypi） | ✅ 8MB/s |
| `repo.huaweicloud.com`（apt） | ✅ 镜像自带 |
| `mirrors.tuna.tsinghua.edu.cn`（conda） | ✅ 镜像自带 |
| `goproxy.cn` | ✅ |
| `pkg.cloudflare.com`（cloudflared 的 deb） | ✅ 19MB / 20s |
| `dl.min.io` | ❌ 20 秒 0 字节 |
| `github.com` releases | ❌ 20 秒 0 字节 |
| `packages.redis.io`（redis-stack） | ❌ 超时 |

由此推出两条做法：

- **MinIO 从源码编**（`go install github.com/minio/minio@latest` 走 goproxy.cn，
  两分钟）。它是纯 Go 项目，这比想办法把预编译包弄进来快得多。
- **cloudflared 用 Cloudflare 自家的 deb**，别走 GitHub；也别 `go install`
  ——它的 go.mod 有 replace，`go install pkg@latest` 会直接拒绝。
- 没有 redis-stack 就**没有 RediSearch**：网关那份块级向量索引退到 scan 兜底。
  产品主链路的向量检索走 PostgreSQL + pgvector，不受影响；`doctor` 会如实报。

### 注册表要反映"这次部署真的起了什么"

网关的 `/readyz` 是 `all(up)`：注册了却没起的条目会让探针恒 503，副本永远
不接流量。所以缺省的 `MODELS_CONFIG` 是 `models.local.yaml`（只有进程内的
borndigital），起了模型线之后再换。

`models.autodl.yaml` 默认把 `embedding_models` 段注释着 —— 起了 `embed.bash`
就要打开它。**别直接改仓库里那份**：下一次推代码会把它盖回去，而后果是索引
静默退回失败。把它复制一份到仓库之外（例如 `$DDP_ROOT/models.deploy.yaml`）
再把 `MODELS_CONFIG` 指过去。

解析引擎另说：`models.autodl.yaml` 里 `vlm-ocr` 标着 `default: true`，那是
**网关在请求没指定引擎时**的选择；上传路径由 corpus-api 的
`DEFAULT_PARSE_ENGINE` 决定。有文字层的 PDF 用 borndigital（零模型零显存），
需要扫描件 / 表格 / 公式时再点名 `engine=vlm-ocr`。

### 公网暴露：Cloudflare Tunnel

```bash
( umask 077; printf '%s' "<tunnel token>" > "$DDP_ROOT/cloudflared.token" )
bash infra/autodl/stack.bash tunnel
```

（别写成 `... > file && chmod 600 $_`：`$_` 取的是上一条命令的最后一个**参数**，
重定向目标不算参数 —— 它展开成 token 本身，chmod 失败、文件停在 644，
而且报错信息把 token 原样打回终端。）

只有**出站**连接（到 Cloudflare 边缘的 7844），不需要公网 IP，也不用开任何
入站端口 —— 这正是它适合"只映射了两个端口"的机器的原因。

**ingress（域名 → 哪个本地端口）配在 Cloudflare 那一侧**，cloudflared 启动时
拉下来。本地 `EDGE_PORT` 与它对不上的表现是**公网 502 而本机全绿**，
所以 `stack.bash tunnel` 会把下发的配置打出来并当场比对。

TLS 在 Cloudflare 边缘终结，回源是明文回环 —— 于是**内外两侧的 scheme 不同**。
这就是 `OBJECT_PUBLIC_SECURE` / `MINIO_PUBLIC_SECURE` 存在的理由：只有一个
开关时，关着则给浏览器的预签名 URL 是 `http://`，被浏览器按混合内容拦掉
（服务端零报错）；开着则内网 client 去 https 连回环，启动自检就断。

## 迁移

两套迁移**各管各的 schema，互不依赖**（没有跨 schema 外键）：

```bash
scripts/dev.sh migrate        # 两套一起跑

# 或者分开
docker compose ... run --rm corpus-migrate                      # alembic
docker compose ... run --rm --entrypoint control-migrate control-api \
    -database "$CONTROL_DATABASE_URL" up                        # Go
```

上线窗口应当把"改库"与"起服务"**分开做**：先迁移、看报告、再滚服务。
`control-migrate status` 会把每个迁移标成"已应用 / 待应用 / 内容已变（危险）"
—— 最后那个表示已经应用过的迁移文件被改过，库里是旧结构而代码读起来是新的。

## 数据库角色

`database/control/0002_roles.sql` 用**数据库权限**钉死写入所有权：

- `ddp_control`（Go）拥有 control schema，对 corpus 一个字都写不了
- `ddp_corpus`（Python）拥有 corpus schema，对 control 只读两张表
- 审计表连服务自己都没有 UPDATE/DELETE 权限

生产必须用这两个角色连库，不要用超级用户 —— 那样这一整层保护等于没有。

## 健康与就绪

| 探针 | 语义 |
|---|---|
| `/healthz` | 进程还在就算活着。**不查依赖** —— 依赖挂了不该被重启 |
| `/readyz` | 依赖不通就别往这个副本上导流量 |

入口的 `/readyz` 还会看 **outbox 积压**：投不出去的事件意味着上传完的文档
永远不出现，而那不该只在别人来问的时候才被发现。

## 可观测

`/metrics` 是 Prometheus 口径。要盯的几个（§13）：

- `ddp_control_outbox_oldest_seconds` —— **比积压数更能说明问题**：
  积压 100 可能只是刚来一批，最老一条 20 分钟没投出去才是故障
- `ddp_control_upload_finalize_failures_total{reason}` —— `digest_mismatch`
  非零意味着有人在传与声称不一致的内容
- 任务队列水位（`corpus.tasks` 的每种任务积压与最老年龄）

日志里**绝不能出现**：原文全文、JWT、API key、SERVICE_TOKEN、
预签名 URL 的查询串、上传内容。
