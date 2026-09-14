# P3 本地模型安装与真实 CPU 生成验证

后续本地页面/修订/CAS/模型关系验证见 [P2-LOCAL-WIKI-VALIDATION-v3.md](P2-LOCAL-WIKI-VALIDATION-v3.md)。本报告保留原六组生成的历史结果；其英文旧 Wiki 失败未改写。

2026-09-12。范围：`ddp_local` 模型清单、显式安装、受控进程，以及共享 Answer/Wiki 的真实模型调用。**生成结果 5/6 通过，英文 Wiki 未通过；本报告不是 P2/P3 全阶段完成声明，不是提交前独立验收。**

## 模型、运行环境与输入

- CPU：AMD Ryzen 7 255，16 逻辑 CPU；模型实际使用 8 个 CPU 线程、0 GPU 层、8192 context、1 个并发槽位。Linux 7.2.4-1-cachyos x86_64 / glibc 2.44 / Python 3.14.7。
- 模型：Qwen/Qwen3-1.7B-GGUF，Q8_0，固定 revision `90862c4b9d2787eaed51d12237eafdfe7c5f6077`，1,834,426,016 bytes，Apache-2.0。SHA-256：`061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a`。
- 运行时：官方 llama.cpp `b10809` Ubuntu x64 CPU tar.gz，16,734,586 bytes，MIT。SHA-256：`5e34434ddc6d03cd1584f403201aff0d4bd1a5793a72ff7e286532dfd1e4b941`。CachyOS 上实际加载与推理通过，其他发行版/架构未经本报告验证。
- 两个文件由发布者来源取得，完整摘要验证后通过 `models import` 显式导入本地工作区。启动再校验完整文件与实际传给模型的描述符；没有用模型下载器的模拟传输测试冒充这次真实安装。
- 输入：仓库 `tests/fixtures/sample.pdf`、脚本生成的 STSong-Light 中文 PDF、24 页 `tests/fixtures/code-corpus.pdf`。第一轮真实 CPU 解析建立冻结版本；后续生成复跑复用这些版本。每份输入的完整 SHA-256、字节数、来源版本和 parse revision 均在 JSON 中。

清单见 [`catalog.json`](../../python/ddp_local/ddp_local/model_runtime/catalog.json)，包含发布者与许可链接。Qwen profile 标为 `partial_real_cpu_validation`，不把运行时健康误当成语义质量通过。

## 真实运行结果

运行命令：

```bash
unshare --user --map-root-user --net .venv/bin/python python/ddp_local/scripts/eval_model.py \
  --workspace /tmp/ddp-real-model-workspace --offline-namespace \
  --output /tmp/ddp-local-model-eval-final.json
```

模型启动（含摘要校验）耗时 3.288 s。生成延迟是本机固定小样本记录，不是吞吐或 SLA；请求 `temperature=0`、`max_tokens=1024`，运行时关闭 thinking。评测直接调用真实模型 HTTP adapter，没有 mock、输出替换或自动重试。

| 输入 | 操作 | 期望值 | 生成耗时 | 结果 |
|---|---|---|---|---|
| english | answer | 42 | 1.265 s | PASS |
| english | wiki | 42 | 1.807 s | FAIL：unsupported_generation |
| chinese | answer | 125 | 1.704 s | PASS |
| chinese | wiki | 125 | 1.216 s | PASS |
| code | answer | HttpRequestParser | 4.117 s | PASS |
| code | wiki | HttpRequestParser | 3.477 s | PASS |

通过条件同时包括期望值存在、每条断言引用的是实际取回的证据、证据为原始资料、bbox 非空、source_version/source_digest 与指定输入一致。人工复核这五个通过输出，数值/标识符确实由对应原文支持；这不代表通用语义蕴含检验。通过产物仍为 `semantic_review=needs_review`。

英文 Wiki 实际输出：

> The answer in the contract is 42. This is stated in the evidence provided [1].

第一句没有引用，系统返回 `unsupported_generation`。SQLite 实测该任务为 `failed`，`result=NULL`；没有发布伪成功 Wiki，也没有将第二句的引用扩给第一句。其余五个任务为 `succeeded` 且具有持久结果。

首轮真实输出也抓到了共享出处投影缺陷：`42 [1].` 与 `125件[1]。` 会把尾标点拆成无引用断言。现已由共享 core 修复，完整 core **66 passed**，新增尾标点正例与后接无引用句负例；该修复没有放松上述英文 Wiki 的拒绝规则。

证据文件：

- [完整实际模型输入、输出、证据及耗时](artifacts/local-model-eval-v3.json)
- [六个持久任务回执核对](artifacts/local-model-receipts-v3.json)
- [真实内存限制失败记录](artifacts/local-model-oom-v3.json)

## 安装与进程约束

- 只接受包内已审阅清单的 artifact ID；请求不能传任意 URL、命令、模型路径或 manifest。默认不下载，显式 CLI/HTTP install 才触发网络请求。下载进度进入持久事件，CLI 同时输出 stderr 进度。
- 中断保留 `.part`，显式重试支持 Range；拒绝错误 Content-Range 与未许可跳转，200 忽略 Range 时从零重写。最终大小、完整 SHA-256、GGUF v3 验证后原子发布；未完成文件不进入 installed 状态。
- 模型目录必须当前用户拥有且为 0700；文件用固定目录 fd 和 `O_NOFOLLOW` 打开，只允许单链接的自有普通文件。先检查再截断，恶意 `.part` 硬链接不能截断目录外文件。运行时归档拒绝路径越界/特殊文件/压缩与成员超额，允许的内部库链接只物化为普通文件。
- 取消校验/解包时必须等后台线程真正退出才关闭 fd 或删除临时目录，防止 `to_thread` 被取消后继续操作已复用描述符。模型与 runtime 均使用已验证描述符，运行环境去掉外部代理/模型下载配置。
- 实际启动使用随机字面回环端口、随机别名、0600 API key 文件，`--offline` 与 `--no-webui`。pidfd 与直接子进程对象限定信号目标；Linux parent-death signal 在父进程消失时停止模型。测试验证不终止另一个无关进程。
- 真实模型正常结束后，进程已退出且私有 runtime 工作目录已删除，见 JSON `shutdown`。

完整 local suite **46 passed**（`/tmp/ddp-local-model-suite.log`），ruff F/B 通过；wheel 构建通过，确认包含当前模型清单、安装器和子进程启动器（`/tmp/ddp-local-model-wheel.log`）。安装 Range/断线/恶意跳转、受控 HTTP handshake、伪运行时 OOM 后显式恢复等属于协议/负向测试；真实模型能力只以上节实际运行计数。

## 实际 OOM 与离线边界

真实执行 `ulimit -v 1048576`（1 GiB **虚拟地址空间**限制）后，官方 llama.cpp 加载 Qwen 失败，运行时明确返回 `out_of_memory` 并清理私有进程目录。恢复正常内存限制后，用同一模型和工作区成功启动并完成五项生成。这个实验是 CPU 分配失败与恢复，**不计为真实 GPU OOM 或 GPU profile 验收**。

严格网络用例在独立 user/network namespace 内只启用 `lo`；最终报告记录唯一接口 `[[1,"lo"]]`，IPv4 与 IPv6 外部路由均为空，模型与解析子进程继承同一隔离边界，无法把任务资料发到外部网络。依赖、模型都在进入 namespace 前预装；没有账号、中心数据库或在线服务依赖。

这证明该 runtime/模型评测路径可在 OS 隔离下运行，**不是整个 Electron App 的 T17 零未经授权外发报告，也不是“从未尝试外连”的 syscall 审计**。本机没有 strace；未把仅阻断外发描述成完整应用所有请求的审计。

## 仍然不通过或未覆盖

- 英文 Wiki 首句缺引用的真实失败仍存在；不把总体评测 JSON 的 `status=failed` 改成 passed。
- Wiki 目前只有一页带结构化引文的生成草稿；本地多页关系检验、修订 CAS、人工编辑合并、更新与发布工作流没有由本报告验收。
- 无 embedding/rerank/视觉模型，检索仍明确降级为关键词；简单中文文字层样本不代表复杂中文、代码版面、扫描件、公式和跨栏质量已通过。
- GPU 独立环境、真实 GPU profile、安装器真实网络中断续传、干净机器 Electron 安装/升级、Linux 凭证后端、中心 Provider 与跨环境授权交付均须在对应阶段另验。
