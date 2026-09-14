# DDP 本地运行时

本地工作区使用 SQLite、FTS5 和受控文件目录；无需 PostgreSQL、MinIO、Redis、中心账号或模型网关。CLI 与私有回环 HTTP 服务调用同一份应用流程。CPU PDF 解析与服务器共享 `ddp_core.application.borndigital`，版面归一化与编译共用 `layout` / `compile_layout`。

目前真实验证了有文字层 PDF 的解析、中文/英文/代码关键词检索、原文 bbox 出处、持久任务恢复及跨工作区 Bundle 往返。本地生成支持按清单安装并托管 CPU 模型，也可显式连接已运行的 OpenAI 兼容 Provider。真实模型输出与局限见 [P3 本地模型验证](../../docs/refactor/P3-LOCAL-MODEL-VALIDATION-v3.md)；协议模拟测试不计入模型质量或离线生成验收。

## 安装与 CPU 路径

从仓库根目录运行，Python 3.11 及以上：

```bash
python3 -m venv .venv-local
.venv-local/bin/python -m pip install -e python/ddp_contracts -e python/ddp_core -e 'python/ddp_local[cpu,http]'

.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace init
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace upload tests/fixtures/sample.pdf --key sample-1
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace work --once
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace search contract
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace tasks
```

第一次安装需要依赖下载；安装后上述 CPU 路径不发网络请求、不下载模型。`upload` 先固定输入快照，再创建持久解析任务；`work --once` 执行一个任务。相同幂等键和输入复用原任务，更换输入则返回冲突。`search` 返回证据 ID，传给 `evidence <id>` 可读取原文、物理页序与显示空间 bbox。

CPU profile 只承诺有文字层的 PDF。扫描件返回 `no_text_layer`；复杂跨栏阅读顺序、表格结构与公式理解没有获得额外能力。输入最多 32 MiB / 500 页；CPU 子进程限时 120 秒，输出最多 32 MiB。关键词检索始终如实返回 `embedding_unavailable`，不会暗中转到远端向量服务。

```bash
# upload / resources 输出中的 version_id，不使用文件 URL 作为身份
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace export VERSION_ID --output /absolute/path/to/result.ddp.zip
.venv-local/bin/ddp-local --workspace /absolute/path/to/second-workspace import /absolute/path/to/result.ddp.zip --key import-1
```

导入先完整验证 Bundle 的摘要、必需 schema、定位、路径和压缩预算，再登记可用版本。复制获得本地版本 ID，同时保留权威来源和原始证据身份。Bundle 不是任意 ZIP 解压接口。

## 私有 HTTP 与进程生命周期

```bash
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace serve
```

默认监听随机的 `127.0.0.1` 端口，自动处理已受理解析任务。stdout 只输出 URL 与会话凭证文件路径，凭证文件权限为 `0600`；退出时删除。所有请求校验 Bearer、精确 Host 和 Origin。Electron main 应读取凭证并代理受限操作，不能把会话 token 交给远端内容或渲染进程。接口清单见 [local-api.md](docs/local-api.md)。

数据库以 WAL、事务和持久事件记录任务。解析 lease 过期后允许新代次接管，旧代次不能发布结果；取消会阻止当前代次完成。运行时退出会终止自己拥有的 CPU 子进程。生成请求在模型调用前持久登记；崩溃后不自动重发可能已被 Provider 受理的请求，返回明确的不确定状态。

## 显式模型选择

内置清单固定模型/运行时版本、发布者 HTTPS URL、SHA-256、字节数、许可、架构、CPU 后端和资源要求。默认不下载；以下 `install` 是显式下载操作，进度写入 stderr 和持久事件。断线保留 `.part`，再次执行显式续传；只有大小、完整摘要和格式均通过才原子发布可用文件。模型目录须由当前用户拥有且权限为 `0700`。

```bash
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace models list
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace models install llama-cpp-cpu-linux-x64 --key install-llama-b10809
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace models install qwen3-1.7b-q8_0 --key install-qwen3-1.7b-q8
# 已取得发布者文件时，可显式本地导入，不重复下载：
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace models import qwen3-1.7b-q8_0 /absolute/path/to/Qwen3-1.7B-Q8_0.gguf
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace serve --installed-model qwen3-1.7b-q8_0
```

`serve --installed-model` 在后台 HTTP listener 中启动经过再次完整校验的 llama.cpp CPU 子进程；独立 `models run ID` 以前台方式托管同一模型。启动使用私有工作目录、随机回环端口/别名、私有 API key、离线参数与清洁环境；退出只终止本运行时创建的模型，Linux 父进程消失也会终止模型。外部服务仍可显式连接：

```bash
# 端口和模型名需对应实际运行的本地服务；这是调用模板，不会自动安装模型
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace \
  --model-endpoint http://127.0.0.1:MODEL_PORT/v1 --model MODEL_ID answer 'contract'
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace \
  --model-endpoint http://127.0.0.1:MODEL_PORT/v1 --model MODEL_ID wiki 'contract'
```

本地端点只接受字面回环地址。默认 `local_only`；即使配置了远端模型，也不会自行回退或外发。远端生成需同时选择 `--model-location remote` 与命令级 `--allow-remote`，模型凭证只从显式 `--model-key-env` 指定的环境变量读取。缺模型、服务不可达、OOM、无效输出和无原文引用都返回可见错误。

`capabilities.generation.status=configured_unverified` 只说明用户配置了端点，不能作为服务就绪或模型测试通过的证据。Wiki 结果是带原始出处的生成草稿，`semantic_review=needs_review`；结构化引用检查不证明每条结论语义成立。本地 HTTP/CLI 已提供持久页面、关系、不可变修订、人工段落编辑与 CAS；完整桌面编辑界面和发布流程仍需对应端验收。

## 验证

```bash
.venv-local/bin/python -m pip install -e 'python/ddp_local[dev]'
cd python/ddp_local
../../.venv-local/bin/python -m pytest -q
cd ../..
.venv-local/bin/python scripts/smoke_local.py

# 仓库开发 venv 装有 gateway / corpus 包时，可增加服务器适配器对拍
.venv/bin/python scripts/smoke_local.py --compare-server
.venv/bin/python scripts/smoke_local.py --compare-server --pdf tests/fixtures/code-corpus.pdf --query HttpRequestParser
```

`--compare-server` 对比同一冻结 PDF 的本地/网关版面、编译 provider、每个源原子的文本、类型、页码与 bbox，并真实渲染服务器出处图；不连接服务器数据库，也不替代中心 HTTP 端到端验收。未指定模型的 smoke 输出 `generation=not_run_model_unavailable`。

预装清单模型后，在支持无特权 user/network namespace 的 Linux 主机复跑真实离线评测：

```bash
unshare --user --map-root-user --net .venv-local/bin/python python/ddp_local/scripts/eval_model.py \
  --workspace /absolute/path/to/workspace --offline-namespace --output /absolute/path/to/new-evaluation.json
```

评测只启用 loopback，保留三组固定输入摘要、实际模型输入/输出、证据绑定、耗时与失败，不自动下载，也不自动重试不合格生成。本地 Wiki 页面/CAS 与模型选择的原文关系句已按下节实测；GPU 独立包及真实 GPU profile 仍需单独实现和验收。


## 持久 Wiki 页面与修订

新 `wikis` 命令使用与 HTTP 相同的 JSON 请求字段，详细形状见 [local-api.md](docs/local-api.md)：

```bash
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace wikis list --limit 50
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace wikis get WIKI_ID
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace wikis revisions WIKI_ID --limit 50
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace wikis get WIKI_ID --revision REVISION_ID
# build.json 包含 title、sources 和预算；模型需通过显式 Provider 参数选择：
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace \
  --model-endpoint http://127.0.0.1:MODEL_PORT/v1 --model MODEL_ID \
  wikis build --body /absolute/path/to/build.json --key wiki-build-1
# edit.json 包含 base_revision_id 与 paragraphs:[{id,text}]，无需调用模型：
.venv-local/bin/ddp-local --workspace /absolute/path/to/workspace \
  wikis edit WIKI_ID PAGE_KEY --body /absolute/path/to/edit.json --key wiki-edit-1
```

列表返回 has_more/next_cursor/visible_total；继续分页可传 `--cursor`。新构建、重建、编辑沿用持久任务与回执；
旧基准修订返回 revision_conflict，不能覆盖新修订。人工文字始终带 unsupported/unreviewed 状态，不能自造原始出处。
工作区从 SQLite schema 1 原子迁移至 2，旧资源与输出保留；失败回滚整个 Wiki 迁移。

[真实 Wiki 验证报告](../../docs/refactor/P2-LOCAL-WIKI-VALIDATION-v3.md) 保存了失败历史及最终两页、六句、两条原文关系的实际模型结果。
关系是模型选择的原文语句，方向仅表示原文词序，不宣称自由语义关系推理。当前没有本地发布或删除接口。
