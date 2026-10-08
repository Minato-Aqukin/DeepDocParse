# 开发与验证

本仓库是包含 Go、Python、Web 与桌面应用的 monorepo。以下命令从仓库根运行；
部署入口见 [DEPLOY.md](DEPLOY.md)，历史重构证据见 [refactor/STATUS.md](refactor/STATUS.md)。

## 准备本地依赖

Python 包要求 3.11 及以上，CI 使用 3.12；Go 版本见
[`services/control-api/go.mod`](../services/control-api/go.mod)（当前 1.27），
整仓默认门禁要求 Node 24.12.0 及以上（见
[`apps/desktop/package.json`](../apps/desktop/package.json) 的 `engines`；
Web 单独支持的版本见 [`apps/web/package.json`](../apps/web/package.json)），
CI 使用 24。默认门禁需要这三套工具链。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install \
  -e python/ddp_contracts \
  -e 'python/ddp_core[db,cjk,dev]' \
  -e 'python/ddp_local[dev]' \
  -e 'services/model-gateway[dev]' \
  -e 'services/corpus-api[dev]' \
  -e 'services/corpus-worker[dev]' \
  -e 'services/mcp[dev]' \
  jsonschema
env ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci --registry=https://registry.npmmirror.com
```

Python 包在同一次安装里列出，让 pip 能解析本地包及其测试依赖。
npm 在 workspace 根安装，并沿用 lockfile 的 registry。
这里跳过的 Electron 二进制下载不影响默认门禁；完整桌面打包另有发行流程。

## 默认门禁与可选目标

```bash
./scripts/check.sh                 # guards python go web
./scripts/check.sh guards          # 单独跑守卫
./scripts/check.sh python go       # 选择多个组
./scripts/check.sh web-e2e         # 显式运行浏览器门禁
./scripts/check.sh --help
```

参数只接受表中的目标；未知目标、空参数及混用的 `--help` 会在运行检查前失败。
目标按表中顺序执行，某项失败后继续执行其余检查并在末尾汇总；任一失败均返回非零。
无参数仍选择前四组，`web-e2e` 不在默认范围内。

| 目标 | 执行内容 | 依赖与边界 |
|---|---|---|
| `guards` | 契约生成物与路由、数据所有权、块类型与分块、枚举、日志脱敏、联邦契约、验收台账、迁移副本、配置文档及根目录 pytest | 需要 Python 开发依赖和 gofmt；开发 PostgreSQL 容器可用时追加真实数据库边界检查，否则明确提示跳过 |
| `python` | Ruff 的 F/B 规则与 ddp_core、ddp_local、model-gateway、corpus-api、corpus-worker、MCP、eval 单测 | 按包目录运行；需要上面的 Python 开发依赖。测试自身的环境相关 skip 仍会显示 |
| `go` | `go vet`、`go test`、gofmt、`go mod tidy -diff` | 真库计量用例需要 `CONTROL_TEST_DATABASE_URL`；未配置时提示跳过。默认不包含 race、独立构建或迁移演练 |
| `web` | `npm run build`（类型检查与生产构建）、组件单测、共享连接层类型与协议测试、桌面主机测试 | 需要 npm 依赖；不需要浏览器或 Electron GUI。桌面用例按平台和运行时产物报告 skip |
| `web-e2e` | `npm run test:e2e`（构建产物与 Playwright） | 需要 Chromium 及系统依赖；覆盖前端浏览器行为，不替代真实后端全栈验收 |

脚本默认优先使用仓库 `.venv/bin/python`，不存在时查找 PATH 上的 `python3`。
显式 `PY` 可以是 PATH 中的命令名、绝对路径，或**相对于调用目录**的路径：

```bash
env PY=python3 ./scripts/check.sh python
env PY=.venv/bin/python ./scripts/check.sh guards python
```

解释器会在切换到包目录前解析为绝对路径。显式空值、不可执行文件或不存在的
`PY` 都会立即失败，不会换成系统解释器。`--help` 无需可用的 Python 环境。

## 浏览器与真实服务验证

浏览器只需首次安装，随后用单独目标运行。关闭占用测试端口的旧 dev/preview
服务，或指定空闲端口，避免复用旧页面：

```bash
npm exec --workspace deepdocparse-web-frontend -- playwright install chromium
env E2E_PORT=15173 ./scripts/check.sh web-e2e
```

Linux 缺少浏览器系统依赖时，按 Playwright 提示安装；CI 使用
`playwright install --with-deps chromium`。此步骤不由默认门禁自动执行。

Go 真库测试应连接**独立测试数据库**，先应用 control 迁移，再运行测试。
将连接串通过 `CONTROL_TEST_DATABASE_URL` 注入环境后执行（连接串只走环境变量 ——
argv 在 ps 与 CI 日志里可见，而连接串里有口令，control-migrate 认 `CONTROL_DATABASE_URL`）：

```bash
(cd services/control-api && CONTROL_DATABASE_URL="$CONTROL_TEST_DATABASE_URL" go run ./cmd/control-migrate up)
./scripts/check.sh go
```

门禁只显示变量是否已配置，不输出连接串。开发容器中的数据库边界检查独立于
这个变量，默认容器名为 `ddp-postgres-1`，可用 `DDP_PG_CONTAINER` 指定。
`scripts/dev.sh up` 会构建并启动完整开发栈，不支持用 `up postgres` 只启动数据库。

真实注册、上传、解析、证据和计量链路需要先按部署指南启动开发栈：

```bash
scripts/check_db_boundary.sh
.venv/bin/python scripts/e2e_stack.py
```

无 GPU 路径使用 borndigital；GPU 模型解析、VQA、embedding、rerank 的真实质量与
性能仍需模型运行时和相应硬件。单测的 mock、CPU 降级或显式 skip 不代表这些路径已验收。

## CI 与提交验收

本地默认门禁复用部分 CI 检查，**不是完整 CI 的替代品**。当前工作流还覆盖：

- [`python.yml`](../.github/workflows/python.yml)：独立环境安装各 Python 包、网关无 ORM 最小依赖、真实 PostgreSQL 迁移往返与迁移后约束检查。
- [`go.yml`](../.github/workflows/go.yml)：真实 PostgreSQL 计量测试及禁止跳过检查、race 检测与构建。
- [`web.yml`](../.github/workflows/web.yml)：生产构建、前端测试及安装 Chromium 后的浏览器门禁。
- [`guards.yml`](../.github/workflows/guards.yml)：干净环境中的跨服务守卫与根目录 pytest（包含门禁 CLI 的行为回归）。
- [`stack.yml`](../.github/workflows/stack.yml)：真实 Docker 栈、数据所有权边界和完整用户路径。
- [`desktop-windows.yml`](../.github/workflows/desktop-windows.yml)：按输入路径触发的 Linux WSL 运行时构建、Windows 打包与校验；GUI smoke 为阻塞门（失败则来源清单与安装包构件不上传），smoke 证据仍照常留档。发布闭环见 `scripts/release_publication.py` 与 `tests/test_release_publication.py`。

提交前运行默认门禁和当前环境能执行的 e2e，记录失败与 skip 的原因，再交独立
审查。修复阻塞项并复验后才能提交。新增守卫须通过有效变异确认能挡住目标缺陷。
具体测试数以本次运行输出为准，历史状态文档中的数字保留为当时记录。
