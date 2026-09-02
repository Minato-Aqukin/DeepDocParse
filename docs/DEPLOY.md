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
`127.0.0.1:端口` 就行。

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
