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

## HTTPS 反向代理

TLS 可以在 nginx / Cloudflare 等边缘终结，应用与 MinIO 的内网连接仍用
HTTP。设置 `PUBLIC_BASE_URL=https://<中心入口>`、浏览器可达的
`OBJECT_PUBLIC_ENDPOINT=<对象域名[:端口]>`（不带 scheme）和
`OBJECT_PUBLIC_SECURE=true`，否则预签名 URL 仍是 `http://`，浏览器按混合
内容拦截而服务端没有错误。Compose 将这个公共 TLS 开关传给 control-api；
corpus-api / corpus-worker 的 `MINIO_PUBLIC_SECURE` 留空时跟随它，也可显式
设成 `true`。同一对象入口的两个开关必须一致。它们只控制签名 URL 的 scheme，
不要为此把内网 MinIO 的 `OBJECT_SECURE` / `MINIO_SECURE` 打开。
model-gateway 不签对象 URL，没有对应公共 TLS 开关。

代理须保留方法、路径、查询串与请求头，禁用请求/响应缓冲，给长请求足够的
超时；对象桶路径独立转给 MinIO，保留原始 Host 与路径（签名覆盖它们）。
`infra/autodl/stack.bash` 的 nginx 已配置这些边界；不要把 corpus-api 另行暴露
到公网。

## 中心间联邦

每个中心只需要同一个 HTTPS 统一入口，无需额外的 TLS facade。入口精确转发
corpus-owned peer 端点：`probes`（含读取）、`admissions`（含 `lookup`）、
`tasks/{executor_task_id}`（含 `cancel`）、`resources/locate`、`results/resolve`、
`evidence-sets/{set_ref}` 与 `published-collections`，均位于
`/api/v1/federation/`。这些端点无需用户会话，corpus-api 自行验证
`X-DDP-Node-Credential` 和目标节点/操作/范围；入口保留 peer 凭证、目标节点、
幂等键与请求 ID，剥掉内部 actor/service 头、Authorization 与会话 cookie，
**绝不注入 SERVICE_TOKEN**。`node`、`nodes`、`members`、`collections`、
`generation-descriptor`、`scopes`、`member-snapshots` 等仍由 control-api 管理；
会话型语料路由仍需会话，`/internal/*` 不会转发给 corpus。这些端点无需会话即可访问，
而 corpus 在验证凭证前要读完请求体，所以入口对请求体设 **8 MiB** 传输上限（含 chunked，
边转发边计数），超限返回 413 `too_large`、超出部分不转给 corpus；前面的反向代理可以
更严，但不要依赖 nginx 的 `client_max_body_size 0` 兜底。

建立 A ↔ B 信任时：

1. 各中心先设置自己的公网 `PUBLIC_BASE_URL`，保留持久 node-identity 卷。
2. 在 A 获取 B 的 `GET /api/v1/federation/node`：核对节点 ID、`public_key`、
   `key_fingerprint` 与入口（通过独立可信渠道核对指纹）。描述符只有 **5 分钟**
   有效，注册前现取，过期就重新取。
3. 用 A 的组织管理员会话向 A 的 `POST /api/v1/federation/nodes` 提交
   `descriptor`、`public_key`、`visible_to_org` 和 `allowed_subjects`；
   再调用 A 的 `POST /api/v1/federation/nodes/{B_node_id}/approve` 批准。
4. 在 B 对 A 重复获取、注册与批准步骤；单向批准不是双向信任。信任只允许
   节点凭证验证，资源仍受各中心本地 ACL 与外发许可约束。
5. 各中心设置 `FEDERATION_PEERS` 的对等节点目录，例如
   `{"node-b":{"endpoint":"https://b.example"}}`（节点 ID 换成实际值，endpoint
   是公网 origin，不带 `/api/v1/federation`）。**control-api、corpus-api 和
   corpus-worker 三处必须一致，并重启全部三个服务**；Compose 的共享环境
   已传递该变量。目录不存 SERVICE_TOKEN 或用户 token；公网保持
   `FEDERATION_ALLOW_LOOPBACK=false`。

撤销节点（`POST /api/v1/federation/nodes/{node_id}/revoke`）是终态：不能再批准，
要恢复只能让对方以新身份重新入网并双向重新批准。别的中心目录里仍列着这个节点时，
本中心的穷查范围会把它记成 `denied` 的未展开子树（不签名、不联系），范围因此保持
partial——这是如实记录，不是故障；要消除只能请对方中心也撤销它。

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

- **MinIO 从源码编**（走 goproxy.cn，两分钟）。上游 2026-09 已撤回全部社区发行、仓库归档，
  `@latest` 不再有意义：按 `infra/images/minio.Dockerfile` 钉住的模块版本编，
  开发栈与 CI 也是用这个 Dockerfile 现编的。
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

混合推理模型的非推理生成还需关闭**聊天模板**，仅设置 reasoning budget 为 0
并不保证正文里没有 `</think>`。在支持该参数的运行时对应条目中声明：

```yaml
vqa_models:
  qwen3-4b-instruct:
    endpoint: "http://chat-instruct:8000"
    capabilities: [instruct]
    options:
      chat_template_kwargs:
        enable_thinking: false
```

随仓库的 `models.yaml`、`models.autodl.yaml` 已为该 Qwen3 条目声明。
网关将模板选项作为请求默认值发送（请求方显式值优先），不按模型名特殊处理，
也不向未声明的 provider 添加未知参数；返回的 JSON/SSE 正文不做标记剥离。
抽取与视觉出处核对也沿用所选 `vqa_models` 条目的模板声明；VLM OCR 则读取
`parse_engines` 对应条目的 `options.chat_template_kwargs`，而非另一个模型条目。
corpus-api 的 `CHAT_URL` 留空并设置对应 `CHAT_MODEL`，普通问答及联邦生成会走
这个注册表边界；显式直连 `CHAT_URL` 时由目标运行时配置模板策略。
关闭模板只修复 JSON 前缀泄漏，不能保证引用正确、页面预算或多来源覆盖通过验收。

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

## 桌面端（Linux/Arch）：构建、更新、回滚

Electron 在 Linux 上没有 autoUpdater。桌面包走**版本化 manifest + 校验 +
预升级备份 + 显式回滚**，完整流程、支持矩阵与验证日志见
`docs/refactor/RELEASE-MANUAL-v3.md`；本地容量基线见
`docs/refactor/CAPACITY-LOCAL-v3.md`。

```bash
# 构建（2026-09-13 在 Arch x86_64 / Python 3.14 上验证；目录包连打三次同哈希）
npm run web:build
.venv/bin/python scripts/build_desktop.py
.venv/bin/python scripts/build_desktop.py --verify dist/desktop/deepdocparse-0.1.0-linux-x64
scripts/build_desktop_arch.sh --reproduce      # makepkg 两次，同 SHA256 才算过

# 更新（把新版本的 tar.gz + .release.json[+ .sig] 放到目标机）
.venv/bin/python scripts/update_check.py verify --root /opt/deepdocparse \
  --manifest <new>.release.json --archive <new>.tar.gz \
  --allowed-signers release.allowed_signers
.venv/bin/python scripts/update_check.py apply  --root /opt/deepdocparse \
  --manifest <new>.release.json --archive <new>.tar.gz \
  --allowed-signers release.allowed_signers --models /var/lib/deepdocparse/models
.venv/bin/python scripts/update_check.py rollback --root /opt/deepdocparse
```

未签名的 manifest 必须显式 `--allow-unsigned`；降级默认拒绝
（`--allow-downgrade` 才放行）。升级前旧整树保留在
`/opt/deepdocparse.previous`，模型目录只读校验、不被触碰。服务端升级前
先按下方「迁移」同一条规矩备份控制/语料库（`pg_dump -Fc` + `pg_restore -l`）。

### 桌面端（Windows）：Tier A 远程 + Tier C WSL2 本地

Windows 只出 Electron 宿主（NSIS per-user 安装器 / 便携 exe，v1 不签名），
**不移植原生 Windows 运行时**：本地模式是 WSL2 里的自包含 Linux 运行时，
由安装包捆绑、按 manifest 校验后解到 `~/.deepdocparse/`。更新/回滚与 Linux
共用 `scripts/update_check.py`（Windows 走 zip + `deepdocparse.exe`，
跨系统 manifest 拒绝）。完整安装/更新/WSL 设置流程与实测范围见
`docs/refactor/RELEASE-MANUAL-v3.md` §9 与
`docs/refactor/WINDOWS-AC-VALIDATION-v1.md`。

**中心不因 Windows 客户端改变**：控制面/语料面仍部署在 Linux（或服务器的
WSL2）上，Windows 端 Tier A 只是现有 `client-runtime` 的 HTTPS 客户端。

## 数据库角色

`database/control/0002_roles.sql` 用**数据库权限**钉死写入所有权：

- `ddp_control`（Go）拥有 control schema，对 corpus 一个字都写不了
- `ddp_corpus`（Python）拥有 corpus schema，对 control 只读两张表
- 审计表连服务自己都没有 UPDATE/DELETE 权限

生产必须用这两个角色连库，不要用超级用户 —— 那样这一整层保护等于没有。

## 数据清理与保留边界（T30）

**删除资产不是整机擦除，也不是撤销已有备份。** 同一份去重内容仍被任一
ResourceVersion、运行任务、待确认 compute、有效 Bundle 副本或引用中的证据绑定
使用时必须保留。只有最后一个引用释放且宽限期结束后才回收可重建产物。

| 类别 / 位置 | 当前保留与清理规则 | 显式清除方式 |
|---|---|---|
| 原件：MinIO `uploads/`、临时 compute input | 文档 GC 默认 `GC_GRACE_SECONDS=3600`，持久化精确 key 清单，claim 后重查引用；部分失败保留剩余 key 和 `documents.gc_error`，后续重试 | 正常删除资源/版本，等待最后引用释放与 worker GC；不要按桶前缀强删共享对象 |
| 全文 / layout / 解析图片 / crops：`results/{parse}/`；固定快照：`bundles/{version}/` | 与原件同一个 reference-safe GC 清单；迁移 parse 的 `result_prefix` 与当前 job crop 前缀都覆盖；不删其它 job / 文档共用前缀 | 同上；列举失败或对象删除失败时不宣称回收完成 |
| 检索 chunk 文本及 pgvector：`chunks.text/search_text/embedding` | 对象清单完全清空后删除该 Document 的 chunk 行；共享 Document 有活版本时原件、图片与向量都留存 | 同上；不是仅清向量而留下全文缓存 |
| 上传拒绝 / 失败 / 过期：`control.upload_sessions` | housekeeping 每 5 分钟领取；从 expiry 与终态时间的较晚者起至少 1 小时，corpus 再独立执行自己的 GC 宽限；短事务以 `SKIP LOCKED` 领取并持久化 `reclaim_attempted_at` 的 5 分钟租约，提交后调用 corpus 检查所有原件引用并删除、abort 精确 key 的 multipart，网络调用不持有 control 事务或行锁；租约未过期时其它 collector 跳过，崩溃后到期可重试；第二个短事务仅在领取时间戳仍匹配且尚未回收时写入结果，旧 collector 不能覆盖新结果；`reclaimed_at/reclaim_error/reclaim_attempted_at` 可查，失败从结果写入起至少 5 分钟后重试 | 已知 receipt 的 `ready` 或尚未分配的 `pending` 可自动清理；`allocating/unknown` 保留并需先确认 S3 在途创建不会再完成，再按 upload-control 契约对账，不能凭超时强删 |
| 证据原子 / citation / Wiki dependency / 审计 | **长期保留，无自动 TTL**。证据 `content` 可以保留原文片段，GC 不清除此内容；已绑定引用还可能保护整份原件。证据最终保留期限是尚未决策的产品事项，不以本次清理擅定 | 当前无受支持的按期限清除接口；需要完整离线销毁时应关闭服务、清除整个选定工作区/数据库及其所有备份，而非破坏引用外键或审计权限 |
| 桌面模型文件：所选 runtime 工作区的 `models/`（部署也可显式指定 `--models DIR`） | 权重按 `artifact-id.sha256`，验证状态 `.state.json`、锁及中断下载 `.part` 同目录；**不属于文档派生数据，无自动 TTL**，文档 GC / 升级 / 默认卸载都保留，partial 留给显式续传 | 先停止该 runtime 与模型进程，删除所选模型对应的权重、状态、partial 与锁文件；或明确删除整个选定 `models/`。下次使用必须重新安装/校验，无隐式卸载按钮 |
| 运行日志 | control JSON stdout、Python/桌面 stderr 由启动器/容器采集；Compose 未声明应用级轮转/TTL，遵循宿主 Docker logging driver；AutoDL 在 `${LOG_DIR:-$DDP_ROOT/logs}/*.log`，启动覆盖该进程日志但运行中无轮转；本机 drill 的 `.dev-logs/` 不自动过期 | 运维显式设置 Docker/logrotate 保留上限；文件日志停止对应写入进程后按选定路径删/截断；不要误删 node-identity、数据库或模型目录。保留多久由部署策略配置，仓库当前不承诺统一天数 |
| 备份 / 旧版本 | 中心 `pg_dump -Fc`、MinIO/卷快照是运维管理；桌面 `<root>.previous`、指定 `--backup-dir` 的应用备份及 `<root>-<old-version>-workspaces/` 的 SQLite backup API 副本由 `UPDATE-STATE.json` 记录；**无自动 TTL**，恢复会恢复备份时仍存在的原文 | 校验新版本和恢复窗口后，显式删除选定离线备份/快照及外部副本；应用卸载可显式 `--remove-backups --backup-dir DIR` 清理其记录的备份，但默认保留工作区与模型。要求不可恢复删除时也必须清理备份、WAL/存储历史版本，不能只调用文档 DELETE |

一致性边界：文档 GC 只处理可重建的文档数据；模型、诊断日志、备份、证据审计有
各自明确的保留所有者，**没有声称随文档一起销毁**。删除 API / GC 不承诺介质级安全
擦除；中心部署若开启 S3 versioning，还需运维清理历史版本/delete markers 与备份。
隔离 PostgreSQL/pgvector + MinIO 的实测（含共享内容、证据保留、终态上传与 multipart）
见 `docs/refactor/artifacts/derived-data-cleanup-20261003.json`；不是对生产备份的清除证明。

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
