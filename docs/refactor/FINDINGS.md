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
