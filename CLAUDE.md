# CLAUDE.md

给 Claude Code 的工作指南。**改代码之前先读这份。**

## 这是什么

面向技术手册与论文的多模态 PDF 检索工作台。三条系统级属性：
**可追溯 / 可复核 / 可更新**；一句话约束：**答案必须回到证据**。

由原 `DeepDocParse`（GPU 服务层）与 `DeepDocParse-Web`（产品层）两个仓库
合并而来，两段 Git 历史完整保留。合仓与企业化重构方案见仓库根的
`MERGE-REFACTOR-PROPOSAL.md`，执行记录在 `docs/refactor/`。

## 先读哪份

| 你要做什么 | 看哪份 |
|---|---|
| **知道重构做到哪、还欠什么** | `docs/refactor/STATUS.md` |
| 改契约 | `packages/contracts/`（**先改契约再改实现**，不变式 4） |
| 理解服务边界与谁能写哪张表 | `docs/refactor/DATA-OWNERSHIP.md` |
| 理解任务队列 | `docs/refactor/TASK-QUEUE.md` |
| 部署 | `docs/DEPLOY.md` |
| 前端视觉 | `../design-previews/DESIGN-GUIDE.md`（REV.04） |
| 避开已经踩过的坑 | `docs/refactor/FINDINGS.md`（28 条，每条都写了"为什么它没被早点发现"） |

## 八条不变式

前四条是产品的，后四条是企业化边界。**它们不是风格偏好，是这个项目
用真实事故换来的。**

1. 每条结论都能指回一个 bbox；指不回必须明确说明。
2. **任何降级都必须对 API、存储和 UI 可见。**
3. 生成物与原文必须可区分，生成物引用最终仍指向原始原子 bbox。
4. 契约先于实现。
5. 一个数据对象只能有一个写入所有者。
6. 大文件不得完整进入应用进程内存，也不得由应用进程长期中转下载流量。
7. 进程重启不得使已受理任务永久丢失或永远停在运行态。
8. 多组织模式下每一次查询都必须有组织边界。

## 目录

```
apps/web/                 Vue 3 前端
services/
  control-api/            Go：组织 / 鉴权 / API key / 配额 / 限速 / 计量 / 审计 / 统一入口
  corpus-api/             Python：文档 · evidence · citation · 检索 · 问答 · 抽取 · 知识
  model-gateway/          Python：注册表驱动的模型协议适配（**无状态，不装 ORM**）
  corpus-worker/          Python：编译 / 索引 / 抽取的持久 worker
  mcp/                    Python：语料级 MCP 五工具
python/
  ddp_contracts/          契约生成物（零依赖，依赖图最底层）
  ddp_core/               两侧共用的语料纯逻辑
packages/contracts/       OpenAPI · DDP-* · MCP · enums.yaml（三语言类型的唯一来源）
database/{control,corpus,migrator}/
eval/                     OCR / 出处 / 抽取 / Agent / 图谱评测
infra/{compose,images,registry,autodl,env}/
tests/fixtures/           全仓共享的冻结夹具
```

## 常用命令

```bash
scripts/dev.sh secrets        # 生成 infra/env/dev.env（随机密钥，chmod 600）
scripts/dev.sh up             # 起全栈（无 GPU 档位）
scripts/dev.sh up --gpu       # 叠加模型运行时
scripts/dev.sh logs corpus-api

./scripts/check.sh            # 全量门禁（22 项），与 CI 同一套判据
./scripts/check.sh guards     # 只跑守卫
```

单项：

```bash
cd python/ddp_core        && ../../.venv/bin/python -m pytest -q
cd services/corpus-api    && ../../.venv/bin/python -m pytest -q
cd services/control-api   && go test ./... && go vet ./...
scripts/check_db_boundary.sh                 # 数据所有权，对着真库
.venv/bin/python scripts/e2e_stack.py        # 真实用户路径（先 dev.sh up）
cd apps/web               && npm run test:unit && npm run test:e2e
```

**pytest 必须在包目录下跑** —— 传 rootdir 之外的路径参数会让 `asyncio_mode`
退回 strict，所有 async 用例集体报 "requested an async fixture"。

## 铁律

1. **契约先于实现。** 改 `/v1/*` 或 `/api/*` 的行为必须先改
   `packages/contracts/`。枚举**只在 `enums.yaml` 里写一次**，三种语言的常量、
   类型与用户可见文案都是生成的 —— 手写第二份会被 `check_enum_usage.py` 抓到。
2. **注册表驱动。** 加模型 = 加容器 + `infra/registry/models*.yaml` 加一行。
   网关从不 import 模型代码，路由层不认识任何具体引擎。
3. **无状态网关不装 ORM。** `services/model-gateway` 的依赖里没有
   `ddp-core[db]`，CI 有一个 job 专门干净装一遍来钉这件事。
4. **共享的是同一份实现，不是复制品。** 分块 / 裁图 / 分词 / 块类型 / 抽取
   schema 的唯一一份住在 `python/ddp_core`。这个项目因为"两份复制品靠注释同步"
   静默出错过三次。
5. **降级必须落到可见的字段上。** 静默降级是这个项目吃过最大的亏
   （M4a 向量检索悄悄退回 BM25，没人发现）。
6. **删对象前必须 claim。** `ddp_corpus/gc.py` 是全项目唯一会不可逆毁数据的
   地方：宽限期 + 条件 UPDATE 两道防护，缺一就会把"删了又传回来"的原件删掉。
7. **流式响应里不能用请求作用域的 DB session。** 它在响应体开始流之前就关了。
8. **httpx / Go Transport 一律不读代理环境变量。** 带代理的机器上内网调用
   会被塞进代理，表现是**卡住而不是报错**。

## 守卫必须做变异确认

**这个项目反复写出挡不住任何东西的守卫测试。** 新加一条守卫时，
改掉被它守的那一行，确认它真的红 —— 然后还原。

`docs/refactor/FINDINGS.md` 末尾记了两条合仓当天自己写出又当场改掉的假守卫
（一条测不出 `FlushInterval`，一条在 loopback 上永远绿）。
**测不出来的东西不要假装在测**：做成结构断言并写清为什么不能做行为测试。

**变异本身也会写错，而写错的变异看起来就是"守卫是假的"。**
实测踩过一次：往 YAML 里插一个重复键去改配置，`yaml.safe_load` 静默取
最后一个，于是配置根本没变、守卫当然绿 —— 差点据此把一条好守卫改掉。
判定"假守卫"之前先确认**变异真的生效了**（打印一下改完的值）。

## 提交与验收流程（用户硬性要求）

**验收的触发点是 commit，不是里程碑。**

```
自验（./scripts/check.sh 全绿；能跑的 e2e 也跑）
  → 启动 general-purpose 子 agent 独立验收（喂它本次 diff + 不变式 + 铁律）
  → 修复阻塞项
  → 必要时二次验收
  → commit → push
```

不提交的中间工作不验收。一次 commit 对应一次验收。

**这一步有实打实的价值**：历次验收抓到过信号量泄漏、BM25 除零、镜像版本残留，
以及多个**静默出错**。合仓这一轮抓到 27 条（`FINDINGS.md`），分三批：F-11..F-16 是**测试全绿时**由独立验收
读出来的；F-17..F-23 是**第一次真起全栈**当场炸出来的（每一条都让主链路完全不通，
而当时门禁 21/21 全绿）；F-24..F-28 是**修完前两批之后**二次/三次验收抓的 —— 包括一条守卫换个位置又变成假的、
一条修复引入的用户可见回归，以及 **F-28：迁移 0013 按改名后的列名去删外键、
`IF EXISTS` 把失败变成静默，导致迁移之后注册的用户开不了会话** ——
六道防线一条都没拦住，因为它们验的分别是源码、权限、模型，没有一条验
「迁移跑完之后库长什么样」。

## 本机环境

Arch Linux（CachyOS）· **核显 780M，没有 N 卡** · Python 3.14 · Node 26 ·
Go 1.27（装在 `~/.local/opt/go`）· locale `zh_CN.UTF-8` · 交互 shell 是 fish
（`VAR=value cmd` 不合法，写 `env VAR=value cmd`）。

**能干**：写代码、全部单测与守卫、前端、迁移演练、Web 后端 + PG/MinIO。
**干不了**：mineru pipeline（CUDA）· VQA 平面 · TEI 加速 —— 都要 GPU。

国内源仍然要用：pip 走 `mirrors.aliyun.com/pypi/simple`，
npm 装依赖要带 `--registry=https://registry.npmmirror.com`（lockfile 的
`resolved` 指向 npmmirror，跨 host 的 tarball 会被 npm 当成 remote 拒掉），
Go 走 `GOPROXY=https://goproxy.cn,direct`。

**镜像给坏包的时候，重试同一个源没有意义。** 实测 aliyun 在一次构建里
连着给出三个不同的坏 wheel（`THESE PACKAGES DO NOT MATCH THE HASHES`：
numpy / asyncpg / pillow）—— 坏的是那份缓存，不是网络抖动。
所以 `pip-retry` 第一次失败就**换源**（`PIP_FALLBACK_INDEX_URL`，
填成另一家），而不是原地重试。
同类的还有 goproxy 的 `unexpected EOF`，那个是真抖动，原地重试有用 ——
`scripts/dev.sh up` 会整体重试三次。

**Docker Hub 解析 manifest 超时（`DeadlineExceeded`）重试也没用**，
它是连不上而不是抖动。走镜像站拉下来再 tag 回原名：

```bash
docker pull docker.m.daocloud.io/library/golang:1.27-alpine
docker tag  docker.m.daocloud.io/library/golang:1.27-alpine golang:1.27-alpine
```

tag 回原名之后 Dockerfile 一个字都不用改 —— **别把镜像站写进 Dockerfile**，
那是这台机器的事，不是这个仓库的事。

**跑迁移一律 `env DATABASE_URL=... alembic ...` 写在同一条命令里**：
Claude Code 的 Bash 工具 cwd 跨调用持久、环境变量不持久，用 `export`
的话下一条命令会落到 `.env` 指向的 dev 库上（真发生过，删过 dev 库的表）。

**在远端判进程存活别用 `pgrep -f`**：它会匹配到你自己那条命令行，
永远回答"在跑"。写成 `ps -eo pid,cmd --no-headers | grep X | grep -v "bash -lc"`。
