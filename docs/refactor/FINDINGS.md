# 重构期间发现的旧系统缺陷

> 每一条都是**在合仓/重构过程中被工具抓到**的既有缺陷，不是重构引入的。
> 记在这里是因为 §11.5 要求「固定夹具与冻结的旧系统输出逐字段比较；
> 任何有意差异都有获批记录」—— 这些就是那些差异的记录。

---

## F-1 · 渲染依赖缺失伪装成「这一页裁不出来」

**严重度**：中（可见但归因错误的降级）
**发现方式**：合仓后在新 venv 里跑 `test_crops_threadsafety.py`，
守卫报「并发渲染有页面返回空」，指向线程安全 —— 实际原因是新 venv 没装 Pillow。

`ddp_core/crops.py` 的三个入口都是 `except Exception: return None`。
于是**「镜像少装了 Pillow」与「这一页真的裁不出来」产生完全相同的可观测结果**：
调用方如实打上 `crop_failed`，界面如实显示降级，一切都"正常工作"，
而真实原因是部署错误。

**处理**：`ImportError` 单独放行（带 traceback 炸出来），其余异常仍然安静降级；
`pillow` 补进 `ddp_core` 的硬依赖。守卫两条：一条钉 ImportError 必须抛出，
一条反哨兵钉住"真正裁不出来时仍返回 None"（防止有人把 `except Exception` 一起删掉，
让畸形 PDF 打断整条抽取链）。两条都做过变异确认：去掉 `except ImportError: raise` 后必红。

## F-2 · 分块回归守卫从 2026-08-28 起一直是红的，没人看见

**严重度**：高（守卫失效，且它守的是"历史出处指错块"）
**发现方式**：把 `scripts/check_chunk_regression.py` 从跨仓对拍改写成
单实现回归时，发现它在**旧仓库里就已经 rc=1**。

`eae7c3d`（2026-08-28，`feat: add evidence compilation primitives`）**有意**
让没有 caption 的 `figure` 块也产出一个原子 —— 阶段 5 的 VLM 理解正是为了让
"只有图、没有文字"的区域进索引，丢掉它视觉链路就永远没有输入。
块数因此 9 → 10。这个行为变更是对的，但：

1. 基线 `tests/fixtures/chunk-regression-baseline.json` 没跟着更新；
2. **这个脚本不在任何 CI 里**（两份 `ci.yml` 都没有调用它）。

于是它红了四天没人知道。守卫不进 CI 等于没有守卫。

**处理**：按当前实现重记基线，并在基线文件的 `history` 字段里写全来龙去脉；
守卫挂进 CI（`.github/workflows/guards.yml`）。

## F-3 · `chunk_index_name` 有两份实现，靠注释同步

**严重度**：高（漂开的后果是永久零命中且不报错）
**发现方式**：拆分 MCP 测试时看到两处同名函数，注释互相写着"必须保持同一命名规则"。

写入侧（`gateway/app/services/task_store.py`）与检索侧（`mcp_server/server.py`）
各有一份。两边漂开的表现是：写入往 A 索引写、检索查 B 索引，
**永久零命中、永久静默退回 BM25，而 `index_status` 一直是 ready**。

原来的守卫是 `assert mcp.chunk_index_name(1024) == gateway.chunk_index_name(1024)` ——
只验一个维度上的字符串相等，两份实现漂开但恰好在 1024 上算出同一个名字时它是绿的。

**处理**：收进 `ddp_core/vector_index.py`，两侧再导出。判据升级为
`writer is canonical and reader is canonical`（同一个函数对象，不是相等）。

## F-4 · 块类型对拍守卫在合仓后会变成恒真

**严重度**：中（守卫退化）
**发现方式**：重写 `scripts/check_blocktype_parity.py` 时。

原脚本起两个子进程，在两个仓库里各算一遍 25 个取值再比结果。
合仓后两个"仓库"是同一份代码，这个脚本会**永远绿**，而它要防的事
（有人重新实现一份块类型判据）一件没少。

**处理**：判据换成两条更强的 ——
① 所有再导出点必须是同一个函数对象（`is`）；
② 25 个取值的结果与冻结基线逐条比对（改判据必须同时改基线，让它进 diff）。

## F-5 · 顶层包名 `app` 撞车（旧系统已知，此处记录处置）

**严重度**：高（静默导入错包）
两个发行包都声明 `packages = ["app"]`，`import app` 解析到哪一个取决于
cwd 与 `.pth` 的字母序。在有环境变量的机器上会当场报 pydantic `extra_forbidden`，
在**没有**那些变量的地方（容器 / CI / 干净 checkout）却会完全正常地加载成功，
直到第一次访问某个字段才以 `AttributeError` 爆掉 —— 报错位置离病根十万八千里。

**处理**：四个 Python 服务包名互不相同（`ddp_gateway` / `ddp_corpus` /
`ddp_worker` / `ddp_mcp`），并加静态守卫
`tests/test_architecture_guards.py::test_service_packages_do_not_share_a_top_level_name`。

---

## 重构期间自己写出并当场修掉的假守卫

> 记在这里是因为「项目反复写出挡不住任何东西的守卫测试」是这个代码库的
> 已知失败模式。新守卫**必须做变异确认**（改掉被守的那行，看它是不是真的红）。

### G-1 · `FlushInterval: -1` 的行为测试测不出那一行

第一版 `TestProxyStreamsWithoutBuffering` 直接测代理，声称守的是
`FlushInterval: -1`。变异确认时把它改成 `0`，**测试照样绿**。

原因：Go 的 `httputil.ReverseProxy` 对 `Content-Type: text/event-stream`
与 `ContentLength == -1` 的响应本来就立即 flush，与 `FlushInterval` 无关。

**改法**：把用例改成经过**生产同款的中间件包装**（`httpx.StatusRecorder`），
守的换成"链路里没有人把响应缓冲住"。这条是真的 —— 去掉 `StatusRecorder.Flush`
的透传后必红，且是干净的 FAIL（第一版还会挂死，因为上游 goroutine 卡在
channel 上让 `httptest.Server.Close()` 一直等在途请求；守卫红的时候挂死
等于没有守卫，所以顺手改成不阻塞的写法）。

### G-2 · `Transport.Proxy = nil` 在 loopback 上测不出来

`TestProxyIgnoresProxyEnv` 第一版设 `HTTP_PROXY` 指向一个必然连不上的地址，
再断言请求成功。变异确认时改成 `http.ProxyFromEnvironment`，**照样绿**。

原因：Go 的 `net/http/httpproxy` 对 loopback 地址一律绕过代理，
而单测只能用 `httptest` 起 loopback 上游。

**改法**：老老实实做成结构断言（读 `up.Transport().Proxy`），
并在用例注释里写清"为什么这里不能做行为测试"。
**测不出来的东西不要假装在测。**

## F-6 · 包改名后，迁移里残留的 `from app.*` 没有任何守卫抓得到

**严重度**：高（全新部署会死在迁移这一步）
**发现方式**：写 0013 迁移时顺手 grep `from app\.`，在
`0008_citation_rank_and_backfill.py` 里发现一处残留。

`test_migrations_and_scripts_import_modules_that_exist` 只查
"**本项目的包**存不存在"，而它的名单是 `("ddp_corpus.", "ddp_core.")` ——
改名之后 `app` 压根不在名单里，于是残留的 `from app.backfill import backfill`
**一条守卫都碰不到**：单测不 import 迁移，本机跑迁移时那一句在函数体内
（只有真跑到那一步才执行），于是它会一路绿到全新部署。

**处理**：加 `DEAD_PREFIXES` 与
`test_migrations_and_scripts_do_not_import_the_dead_app_package`，
把"引用已经不存在的顶层包"变成一条独立的守卫。做过变异确认。

**这条的教训比它本身大**：守卫的名单如果只列"现在有什么"，
它就抓不到"以前有、现在没有"的引用 —— 而改名恰恰只产生后一类残留。

## F-7 · `control.schema_migrations` 建了两次，全新库跑不起来

**严重度**：高（从零部署直接失败）
**发现方式**：第一轮空库迁移演练。

`0001_control_schema.sql` 里建了 `control.schema_migrations`，而迁移器的
bootstrap 已经建过它（账本必须先于第一个迁移存在，否则第一次跑时查不到账本、
0001 会被重复执行）。全新库上 `control-migrate up` 直接
`relation "schema_migrations" already exists`。

**这是"从零部署"路径独有的缺陷**：任何已有库都不会撞到它，而开发机上
从来没有"全新库"—— 这正是空库演练存在的理由。

**处理**：从 0001 里删掉建表，账本归 bootstrap 独有。

## F-8 · 迁移 revision id 混用两种命名，alembic 直接 KeyError

**严重度**：高（迁移根本跑不起来）
**发现方式**：第二轮生产快照演练。

`0013` 的 revision id 写成 `"0013_persistent_tasks"`、down_revision 写成
`"0012_knowledge_layer"`，而既有迁移用的是纯数字（`revision = "0012"`）。
alembic 解析版本图时 `KeyError: '0012_knowledge_layer'`。

**本机单测碰不到它**：单测走 `Base.metadata.create_all`，一行 alembic 都不跑。
只有真的对着一个库执行迁移才会发现 —— 而那正是演练存在的理由。

**处理**：改成纯数字，并在文件里写清"与既有迁移保持同一形状"。

## F-9 · dry-run 必然报 4 条 FAIL

**严重度**：中（会训练人忽略红色）
**发现方式**：第二轮演练。

迁移器第一版的 dry-run 会跑完整的 12 项对账，而 dry-run 一行都没写 ——
4 条行数对账**必然**失败。

**必然失败的检查比没有检查更糟**：它会让人学会"dry-run 红是正常的"，
而那一刻真正的问题就再也看不见了。

**处理**：dry-run 只做**源侧预检**（数据本身有没有问题，必须通过才允许进入
切换窗口），完整对账留给 `--apply`。

## F-10 · go.sum 少一条，本机全绿，镜像构建第一步就炸

**严重度**：高（镜像构建 100% 失败，即"部署不了"）
**发现方式**：第一次真的 `docker compose build`。

`go.mod` 是历次 `go get` 攒出来的，从没 tidy 过：`github.com/jackc/puddle/v2`
（pgxpool 的间接依赖）**在 go.mod 与 go.sum 里都不存在**。

而本机 `go build ./...`、`go vet`、`go test ./...`、`-race` **全绿** ——
因为本机的 module cache 里早就有 puddle 了，Go 不需要再校验它。
容器里是空 cache，第一句 `go build` 就是：

```
missing go.sum entry for module providing package github.com/jackc/puddle/v2
```

**这一条的形状与 F-2、F-7、F-8 完全一样**：判据本身是对的，
但**执行判据的环境**与部署环境不同，于是它在唯一重要的地方失效。
本机绿 ≠ 干净环境绿，而干净环境正是 CI 和镜像构建。

**处理**：`go mod tidy`（补进 `puddle/v2` 与 `golang.org/x/sync`），
并在 `scripts/check.sh` 与 `.github/workflows/go.yml` 各加一条
`go mod tidy -diff`。**已做变异确认**：把 go.mod 换回旧的那份，守卫立刻红。

顺带修的一条：`go mod download` 撞上 goproxy.cn 的 `unexpected EOF`
会让整个镜像构建白跑几分钟，Dockerfile 里改成重试三次。

---

## 附：本机能跑到哪一步

`docs/refactor/STATUS.md` 里「真实用户路径」那一栏的状态，
以 `scripts/e2e_stack.py` 对着**真起来的全栈**跑出来的结果为准 ——
不是单测，也不是"应该能跑"。无 GPU 时它会**显式 SKIP** 索引与问答那一段
并说明原因，同时反过来断言"没有 embedding 时 index_status 必须是可见的失败"。

---

# 独立验收（2026-09-02）抓到的六条

前面 F-1..F-10 是工具在合仓过程中抓到的。下面这六条是**独立验收 agent**
读代码 + 做变异实验抓到的，形状与前面那批不同：它们全都**测试全绿、门禁全绿**，
因为问题恰恰出在"没有测试"或"测试测不到"。

## F-11 · 下载原件仍然整份进内存，而 §19.7 已经标了 ✅

**严重度**：高（不变式 6，用户可达）

上传方向早就直传对象存储了，还有一条静态守卫（`test_corpus_api_accepts_no_file_bodies`）
钉着。**下载方向漏了**：`GET /api/documents/{id}/download?format=source` 用
`await storage.get(object_key)` 把整份原件读成 `bytes` 再当响应体返回，
还要再经 Go 反代转发一次 —— 两个应用进程同时做下载中转。
前端的"下载原件"按钮直连它。

**为什么没被发现**：守卫只扫了上传方向（`UploadFile`）。
不变式写的是"大文件不得完整进入应用进程内存"，而守卫只落实了半句。

**处理**：改成 302 到一条 5 分钟有效的直读 URL（`filename` 与 `content-type`
由签名覆盖，调用方改不了）。守卫用**地雷**而不是静态扫：
把 `storage.get` 换成一个必然抛异常的函数，这条路径再想读字节就炸 ——
静态扫拦不住"换个写法读"，地雷拦得住。前端那半边也一起改了：
不能让 XHR 去跟这个 302（Authorization 会跟到对象存储，
表现是一个看起来像签名错误的 400，直传上传踩过同一个坑）。

## F-12 · Go 生成了 754 行契约常量，然后一个都不用

**严重度**：中（铁律 1 名存实亡）

`internal/contracts/enums.go` 是从 `enums.yaml` 生成的，**被零个 Go 文件 import**。
Go 那边手抄了一份：`rbac.go` 重新声明四个角色、`uploads.go` 与
`upload_handlers.go` 手写 upload_status 字面量、`handleInternalUsage`
把 `kind` 直接塞进 `usage_ledger`（而那一列没有 CHECK 约束）。

**当时没有漂移**，所以任何测试都不会红 —— 这条只能靠读代码发现。
而一旦漂移，后果是静默的：契约里加个角色，Go 判成"未知"，那个角色的人全部 403。

**处理**：角色常量改成从 `contracts` 派生（`Role(contracts.RoleViewer)`），
upload_status 用生成的常量比较，`handleInternalUsage` 加
`contracts.UsageKind(kind).Valid()`。
新增 `TestRankCoversContractRoles`：契约里有几个角色，`rank` 里就得有几个。

## F-13 · 入口中间件零测试，而它是全站唯一的准入判断

**严重度**：高（越权 / 超额 / 免费用，没有一个会自己报出来）

`internal/api/middleware.go` 的 `requireAPIKey` 90 行里同时做：key 校验、
撤销与过期、作用域、限速、配额。`internal/store`、`internal/ratelimit`、
`internal/api`（handler 层）三个包**一个 `_test.go` 都没有**，
而 `INVENTORY.md` §6.2 三行写着"等价覆盖：Go"。

**"有等价覆盖"这句话本身是可以造假的**，而且造假不会被任何工具发现 ——
除非有人真的去 `ls` 那个目录。

**处理**：给 `requireAPIKey` 抽了 `apiKeyStore` 接口做测试缝（只列真正用到的
四个方法），补 9 条门禁用例 + 7 条限速用例 + 4 条计量用例 + 2 条审计日志用例。
五条关键分支做了变异确认（去掉作用域检查、去掉 Live 判断、去掉限速拒绝、
去掉配额检查、去掉 JWT 识别）—— 全部真红。

## F-14 · 计量的行为全在 SQL 里，于是"没法测"变成了"不测"

**严重度**：中（用量报表错了不会报错）

按天/按种类聚合、按人隔离、`event_id` 幂等、时间窗口 —— 四件事全在
`UsageSeries` / `RecordUsage` 的 SQL 里，没有一处能用假 pool 测出来。
旧系统那两条用例跑的是真库；搬到 Go 之后就没有了。

**处理**：`usage_pg_test.go` 连真 PostgreSQL，`CONTROL_TEST_DATABASE_URL`
指过去就跑、没有就 skip；**CI 的 go job 起了 postgres 服务**，
并加了一条反哨兵：如果那几条被跳过就直接报错 ——
否则 skip 与 pass 在 `go test` 的总结里长得一模一样。
四条 SQL 判据都做了变异确认。

## F-15 · 注释说"必须留下日志"，代码写的是 `_, _ =`

**严重度**：中（以为有审计记录，其实没有）

`store.Audit` 的审计写入是 `_, _ = s.pool.Exec(...)`，紧挨着的注释写着
"审计写失败不该让业务请求失败，但**必须留下日志** —— 静默丢审计比不做审计更糟
（它让人以为有记录）"。

**注释与代码矛盾时出错的总是代码，但读代码的人会先信注释。**

**处理**：失败走 `auditFailed`，记 ERROR 级日志（action / target / request_id / err，
**不记 detail**）。测试捕获 slog 输出确认它说话了，并有反哨兵确认捕获本身有效。

## F-16 · `check_enum_usage.py` 报着 38 处用法，一处 `degraded` 都没量到

**严重度**：高（不变式 2 的机械保障当时是空的）

守卫只认单目标赋值、关键字参数、字典字面量三种形状。
而 `degraded` 的真实写点全在另外三种：元组解包（`degraded, verified = "x", False`，
4 处）、`degraded.add("x")`（6 处）、列表字面量（`compile_degraded=["x"]`）。
逐个变异确认：**这三种全部绿**。

**守卫报绿而完全没覆盖目标，比没有守卫更危险** —— 它让人以为这件事有人管。
总数阈值那条反哨兵也没救回来：38 > 20，而那 38 处全是别的枚举。

**处理**：补三种形状（顺手发现 `compilation.py` 的局部 `degraded` 装的其实是
compile_degraded，加了逐文件的显式映射，**没有**用"两个枚举取并集"糊过去）。
反哨兵改成**逐个枚举点名**：`degraded` 与 `compile_degraded` 必须各自被量到，
哪个降到 0 就说明写法变了而扫描没跟上。用法数 38 → 52。

## 附：还有两条不算阻塞但记一笔

- `INVENTORY.md` §3.2 曾写"对账循环迁入 corpus-worker"，实际仍挂在
  corpus-api 的 lifespan 上。已改成事实。
- `STATUS.md` §18 决定 1 曾写"所有查询从第一天起就带 organization_id"。
  控制面是，语料侧不是（只有 7 张表带这一列，`_visible()` 明说语料是
  整个部署共享的）。已改成事实。


---

# 第一次真起全栈（2026-09-02）抓到的七条

**这四条的共同形状：所有单测绿、所有守卫绿、四条 CI 工作流绿、21/21 门禁绿，
每个容器 healthy，每一层都"正确地"报错 —— 而产品主链路一次都没通过。**

它们全都住在"两个进程之间"或"部署配置里"，进程内测试原理上看不见。
`scripts/e2e_stack.py` 与 `.github/workflows/stack.yml` 就是为这一类存在的。

## F-17 · 给浏览器签名的那个 client，签名前要先连一次浏览器地址

**严重度**：高（上传功能完全不可用）
**发现方式**：第一次 `docker compose up` 后跑 `POST /api/uploads`。

对象存储 SDK（Go 的 minio-go、Python 的 minio 都一样）在签名前会发一次
`GET /{bucket}/?location=` 去问区域 —— **向被签名的那个 endpoint 发**。
而给浏览器签名用的 client 指的是浏览器可达的地址（`127.0.0.1:19000`），
容器里根本连不上它。

表现：`POST /api/uploads` 502 `objectstore_error`，
而启动自检（走内网 client）一切正常、`/readyz` 里 `objectstore: ok`。

**只在"内外两个 endpoint"的部署形态下出现** —— 而那个形态是这套系统的
默认形态，本机单测与进程内 e2e 都用同一个地址，碰不到。

**处理**：两侧都显式传 `Region`（MinIO 是 `us-east-1`），SDK 就不再去问。

## F-18 · 迁移建了数据库角色，但角色没有口令

**严重度**：高（全新部署起不来）
**发现方式**：同上，corpus-api 日志里一片 `InvalidPasswordError`。

`CREATE ROLE ddp_corpus LOGIN` **不带口令**，而 PostgreSQL 默认的
scram-sha-256 对没有口令的角色一律拒绝。全新部署的表现是：
两套迁移全部成功、control-api 健康、`/readyz` 全绿，
而 corpus-api 的**每一次**查询都认证失败 —— 它自己的 `/healthz` 还回 200，
因为那条只证明进程活着。

**口令不能写进迁移文件**：那是 schema 的一部分，会进 git，而且迁移文件的
校验和被账本钉着，改口令等于"迁移被篡改"。

**处理**：`migrate.SetRolePasswords` 在迁移之后按环境变量设置口令，
没设变量就跳过并**打日志说出来**。

## F-19 · outbox 只发了一半的 actor 上下文头，事件永远投不出去

**严重度**：高（上传完成后文档永远不入库 —— 产品主链路断在这里）
**发现方式**：同上。

corpus 的 `current_actor` 要求四个头一个都不能少（缺任何一个 401），
这是刻意设计：缺头给默认值的话，"入口挂错中间件"会表现为
"这个人突然变成只读了"，而不是一个能一眼看出的鉴权失败。

而 control-api 的 outbox 投递只发了 `X-DDP-Organization` 与
`X-DDP-Actor-Kind`。于是每一条 `DocumentSubmitted` 都 401 ——
**而系统的每一层都在正确地工作**：投递器忠实重试、如实记下
"corpus-api 返回 401"、`/readyz` 如实报 outbox stale。
一切都"正确地"坏着。

单测碰不到它：corpus 那边的测试直接调消费函数，不经过 HTTP 头这一层；
Go 这边当时一个测试都没有（见 F-13）。

**处理**：改用 `identity.Actor.Apply`（本来就是干这个的），
并加 `TestServiceActorSendsEveryHeaderCorpusRequires` 钉在发送侧。

## F-20 · 「Go 对语料一个字都写不了」这句话从来没有生效过

**严重度**：高（一半是服务起不来，一半是边界 5 形同虚设）
**发现方式**：修完 F-19 之后，`/internal/events` 从 401 变成
500 `permission denied for table processed_events`。

两个问题叠在一起：

1. alembic 用属主 `ddp` 跑，建出来的表归 `ddp`；服务进程用 `ddp_corpus`，
   对这些表一个权限都没有。**没有任何一步给它授权。**
2. `database/control/0002_roles.sql` 里那段授权写的是
   "如果 corpus schema 存在就授权" —— 而语料表根本不在名为 corpus 的
   schema 里，它们在 `public`（alembic 从一开始就建在那儿）。
   那段 SQL **永远跳过**。

第 2 条的后果比第 1 条更严重：`DATA-OWNERSHIP.md` 通篇在说
"隔离靠数据库权限，不靠自觉"，而实际部署里 control-api 用超级用户 `ddp`
连库 —— 它想 UPDATE 哪张语料表都可以。边界 5 当时只是一条静态守卫的自觉。

**处理**：
- 新增 `database/corpus/grants.sql`，对**表真正所在的 schema**（public）授权，
  并显式 REVOKE 掉 `ddp_control` 的表权限；
- corpus 的迁移入口改成 `migrate.sh`（alembic + 授权绑在一起，
  `ON_ERROR_STOP=1`）；
- compose 里 control-api 改用 `ddp_control` 连库，迁移拆成独立的
  `control-migrate` 一次性容器（建表要 DDL、设口令要 CREATEROLE，
  长跑进程不该有这两样）；
- `DATA-OWNERSHIP.md` 加了警告说明语料表在 public，以及这件事曾经
  让规则整个失效。

**没做的**：把 30 张语料表搬进 `corpus` schema。那需要一次独立迁移
加一轮对拍，不在本轮范围内 —— 但文档不再假装它已经在那儿了。

## F-21 · 解析平面根本没有 worker

**严重度**：高（解析功能完全不工作）
**发现方式**：真实用户路径跑到第 4 步，解析永远停在 `running`。

旧仓库的 compose 里有一个 `arq-worker` 服务，合仓时**整个漏掉了**。
网关只负责受理与转发，真正去调引擎、轮询、归档的是那个进程。

表现极难归因：网关健康、`POST /v1/parse` 200 Accepted、状态查得到、
`error` 是 `null`、进度停在 0.5 —— 看起来像"模型很慢"，
而实际上队列里的任务**一个消费者都没有**。

**为什么没人发现**：契约测试 mock 掉了上游，进程内 e2e 直接调任务函数，
两者都不需要真的有 worker 在跑。而部署清单的正确性没有任何测试。

**处理**：compose 补 `model-gateway-worker`（与 corpus-worker 是两回事：
那个跑索引/抽取/GC 走 PG 队列，这个跑解析轮询与归档走 arq/Redis）。

## F-22 · 稳定文件 URL 签的是浏览器地址，而下载它的是容器里的进程

**严重度**：高（解析必然失败）
**发现方式**：补上 worker 之后，解析从"永远 running"变成
`failed: All connection attempts failed`。

`/internal/file-grants` 用 `PUBLIC_BASE_URL`（缺省 `http://127.0.0.1:8080`）
拼稳定文件 URL。而这条 URL 的消费者是 model-gateway —— 一个容器里的进程，
它解析不了 `127.0.0.1:8080`。

**与 F-17 是同一个形状**：把"给人看的地址"与"给服务用的地址"混成一个。
这个系统里已经有过一次（MinIO 的 internal/public 双 endpoint），
教训没有推广到 HTTP 这一侧。

**处理**：加 `INTERNAL_BASE_URL`，缺省回落到 `PUBLIC_BASE_URL`
（单机部署两者本来就一样，不该多配一项）。


## F-23 · 语料侧的 outbox 只有写入端，没有投递端

**严重度**：高（用量与账单永远是空的）
**发现方式**：真实用户路径跑到第 7 步，`/api/usage` 一条记录都没有。

`usage.py` 把用量事件写进 `corpus_outbox`，**与业务写入同一个事务** ——
这一半一直是对的，还有测试钉着原子性。
而**全仓没有任何代码读那张表**：事件躺在里面，`attempts` 一辈子是 0。

Go 侧有 `deliverOutbox`，Python 侧没有对应实现。合仓时只搬了写入端。

**每一层都不报错**：写入成功、事务原子、`/api/usage` 如实返回"没有数据"、
`/readyz` 只看 control 侧的 outbox 所以也是绿的。
**现有测试原理上抓不到**：它们全都在验"写进去了没有"，
而缺的恰恰是"写进去之后有没有人管"。

**处理**：新增 `ddp_corpus/outbox.py`（照着 Go 侧写：认领时
`FOR UPDATE SKIP LOCKED`、指数退避、409 当成功），挂进 lifespan，
补 7 条单测。

顺带发现自己刚写的封顶是死代码：`min(2 ** min(attempts, 8), 300)` ——
`2**8 = 256 < 300`，那个上限永远不生效。
测试里断言"退避封顶等于上限"当场把它照出来了。


---

# 二次独立验收（2026-09-02 晚）抓到的四条

第一次验收抓的是"声称与事实不符"，第一次真起全栈抓的是"部署形态里的洞"。
**这一轮抓的是"修复本身带进来的东西"** —— 每一条都发生在前两轮全部改完、
门禁 22/22、真实用户路径 19 条全绿之后。

## F-24 · 审计守卫换了个位置又变成假的

**严重度**：中（守卫存在但不起作用）

F-15 修好了"审计写失败要记日志"，配的测试却直接调 `auditFailed()` ——
验的是 logger 的形状，而不是**调用点有没有接上**。
验收当场演示：保留 `auditFailed`、把 `Audit()` 里那句改回 `_, _ =`，测试全绿。

测试自己的 docstring 写着"**不要改回 `_, _ =`**" —— 那是一句叮嘱，不是守卫。
**与 F-15 是同一个形状（注释说一套、代码是另一套），只是换到了测试里。**

**处理**：从调用点验 —— 给一个建好就 `Close()` 的 pool，调真正的 `Audit()`，
断言日志里出现了这次审计的 action。变异确认：改回 `_, _ =` 当场红。

## F-25 · 「下载原件」的文件名变成了 document id

**严重度**：中（用户可见回归，本轮引入）

F-11 把下载改成 302 到签名 URL，前端也改成走 `download-url`。
但 `file_grants` 里**没有存文件名**，`handleDownloadURL` 拿 `document_id`
当文件名签进 `response-content-disposition`。

**为什么前端补不了**：那条 URL 跨源，浏览器按规范**忽略跨源
`<a download>` 的文件名提示** —— 只有服务端签的那个算数。
改之前走同源 blob，`a.download` 生效，所以这个问题是"改成直读"的代价。

顺带两条：Python 侧签的是裸 UTF-8 `filename="报告.pdf"`（不合 RFC 6266，
各家浏览器解读不一，而名字里一个 `"` 就能截断这个头）；Go 侧写对了。

**处理**：`file_grants` 加 `filename` 列（迁移 0003，老凭证就地补而**不换
token** —— 换 token 就换了 doc_hash）；Python 侧改成
`filename="<ASCII 回退>"; filename*=UTF-8''<百分号编码>`。

## F-26 · MCP 还在用超级用户连库

**严重度**：高（F-20 修了三个长跑客户端，漏掉第四个）

`compose.dev.yml` 里 MCP 的 `CORPUS_DATABASE_URL` 用的是属主 `ddp`
（`rolsuper = t`）。**超级用户绕过全部 GRANT/REVOKE** ——
F-20 那一整套授权对它一条都不生效，它想写 `control.usage_ledger`、
`api_keys`、`users` 都可以。

而 `check_db_boundary.sh` 也看不见它：那 17 条断言测的是
`ddp_corpus` 与 `ddp_control` 两个受限角色，MCP 用的根本不是它们。

**处理**：MCP 改用 `ddp_corpus`；边界检查加四条 ——
"除属主外没有可登录的超级用户"、"属主确实是超级用户"（反哨兵）、
两个服务角色都不是超级用户。**这一类断言必须排在权限断言前面**：
只要有一个多余的超级用户，后面每一条都是空的。

## F-27 · 降级枚举守卫的第二个洞：`return` 与 `a or "x"`

**严重度**：高（不变式 2 的机械保障有一半是空的）

F-16 补了三种形状（元组解包 / `.add()` / 容器字面量），用法数 38 → 52。
验收又数出**两种没盖到**：

- `return None, "vision_unavailable"` —— return 里按位置放的降级（18 处）
- `degraded or "no_hits"` —— 兜底表达式，`qa.py` 里最常见的写法

后果：21 个 `degraded` 取值里 6 个、8 个 `compile_degraded` 里 1 个
**一处写点都没被量到**。补完之后 38 → 52 → **104**，`degraded` 从 28 → 59。

**补完当场抓到一条真漂移**：`ddp_mcp/corpus.py` 的 `ask_impl` 返回
`degraded="no_relevant_chunks"` —— 契约里没有这个词，于是它没有用户可见
文案。语料侧同样的情形用的是 `no_hits`。**自己造一个同义词就是
"降级不可见"**，已改。

**反哨兵也走到第三版了**：

    v1 总数阈值      -> 报着"38 处"而一处 degraded 写点都没量到
    v2 逐枚举非零    -> 拆掉两段 scan 后 52->40，照样绿（验收实测）
    v3 逐取值覆盖    <- 契约里每个降级取值都必须至少有一处写点被扫到

三种拆 scan 的变异对 v3 全红，而且报的是**具体哪几个取值掉了覆盖**。
唯一没有写点的 `upstream_interrupted` 登记在 `KNOWN_UNPRODUCED` 里并写清
理由（它是 SSE error 帧的 code，不是 degraded 字段的取值，有测试钉着）。

## F-28 · 迁移 0013 按改名**之后**的列名去删外键，`IF EXISTS` 把失败变成静默

**严重度**：高（主链路不通 —— 迁移之后注册的用户开不了会话）
**发现方式**：第三次独立验收在核对删表工具的前置条件时，
查 `DROP ... CASCADE` 会连带删掉什么，顺着连带项查到的根因。

0013 先把 `user_id` 改名成 `actor_id`，然后按**改名后**的列名拼约束名去删：

```python
op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_fkey')
#  column 已经是 "actor_id"  ->  conversations_actor_id_fkey
```

而 **PostgreSQL 改列名不会改约束名** —— 真实约束仍叫
`conversations_user_id_fkey`。`IF EXISTS` 于是静默匹配不到、静默成功。
同一个循环里那些没改过名的（`documents.uploaded_by`、`parse_jobs.api_key_id`…）
名字对得上，全删掉了；**只有三张改过名的没删掉**，正好印证根因。

## 后果不是不整洁

`conversations` / `extraction_templates` / `extraction_runs` 三张**活语料表**
仍然要求 `actor_id` 存在于遗留的 `public.users` 里。而账号已经搬去
`control.users` —— 迁移之后经 control-api 注册的用户，id 根本不在旧表里：

```
ERROR:  insert or update on table "conversations"
        violates foreign key constraint "conversations_user_id_fkey"
DETAIL: Key (actor_id)=(...) is not present in table "users".
```

**开不了会话、建不了抽取模板、跑不了抽取批次** —— 而那正是新架构的常态用户。
在当天由 `scripts/dev.sh up` 全新建出来的库上一样复现。

## 为什么六道防线一条都没拦住

| 防线 | 为什么没拦住 |
|---|---|
| corpus-api 单测 | 走 `Base.metadata.create_all`，ORM 里没有这个外键 |
| 0013 自己的测试 | 那段带 `if not is_sqlite`，单测连碰都不碰 |
| `check_data_ownership.py` | 扫**源码**，而源码是干净的 |
| `check_db_boundary.sh` | 验的是**权限**，不是**约束** |
| `e2e_stack.py` | 没有 embedding 端点，跳过了问答 —— 一条会话都没插过 |
| CI 的真库迁移作业 | 只验"upgrade/downgrade 跑得通"，不验**跑完之后库长什么样** |

`models.py` 里那句"一个指向 users.id 的外键都没有"是**模型**的实情，
不是**库**的实情 —— 这个区别就是这条缺陷藏身的地方。

## 处理

- 新增迁移 `0014_drop_legacy_user_fkeys.py`：**不猜名字，从 `pg_constraint`
  查出来删**。名字可以被改过（手工建的库、pg_dump 恢复），而"指向
  public.users 的外键"这个性质不会变。因为名字是查出来的，所以不需要
  `IF EXISTS` —— 而不需要 `IF EXISTS` 意味着删不掉会报错而不是静默跳过。
- `python.yml` 的真库迁移作业后面加断言：**跑完迁移之后**查 `pg_constraint`，
  没有语料表指向 `public.users`（带反哨兵：旧表本身必须还在，
  否则那条断言恒真）。
- 删表工具补依赖检查：`CASCADE` 会连带删掉四张表之外的东西就拒绝 ——
  它差一点用一次不可逆的 DROP 顺手"修好"这个缺陷，还不告诉任何人。

**这一条是删表工具替我们照出来的**：写那个工具的时候去查"CASCADE 会连带
删什么"，才看见三张活表挂在旧表上。不查的话，第一次真删就会同时发生
"表没了"和"一个 schema 缺陷被无声修好了"。
