# Windows 桌面支持实施计划（A+C）

> 2026-09-13。范围已与用户确认：**Tier A 仅连中心的 exe + Tier C WSL2 本地模式**。
> 复用现有 Linux 运行时，不移植原生 Python。执行权威为本文；冲突时以工作区
> 计划 v3 与仓库八条不变式为准。

## 决策记录

| # | 决策 |
|---|---|
| 1 | WSL 运行时随安装包捆绑（离线可用） |
| 2 | 默认发行版 + 设置可指定 |
| 3 | 允许 `cli.py` 两个增量：`--token-file -`（不落盘）+ stdout bootstrap 加 `pid` |
| 4 | NSIS 安装器 + 便携 exe 都出 |
| 5 | 允许在 windows-latest 上做 WSL spike |
| 6 | v1 不签名，文档写明 SmartScreen 警告 |

## 架构

```text
Windows Electron 宿主（exe）
  ├─ 远程模式：HTTPS → 中心（现有 client-runtime）
  └─ WSL2 本地模式：
       wsl.exe -d <distro> → ~/.deepdocparse/runtime（自包含，随包捆绑）
       127.0.0.1:port（WSL localhostForwarding）→ 现有 ddp-client/1 握手
```

两条经核对的结论：

1. **无文件系统桥**：导入/导出走字节流 HTTP（`client-host.importFile` 快照后
   POST），WSL 运行时不需要读 Windows 路径。工作区/SQLite/blob/模型全放 WSL
   ext4（`~/.deepdocparse/`），避开 `/mnt/c` 的 WAL 与权限问题。
2. **启动/停止换通道**：`cli.py serve` 已把 bootstrap JSON 打到 stdout；WSL 模式
   用 stdout 取（url/token/pid），`--token-file -` 不落盘；停止按内部 PID 经
   `wsl.exe -d <distro> -- kill`；**绝不** `wsl --terminate` 整发行版。

## 工作流与文件归属

| 工作流 | 内容 | 独占文件 |
|---|---|---|
| W1 宿主抽象 | ACL 语义替代 mode/uid、DPAPI、RuntimeBackend 缝、Windows 分支装配 | `apps/desktop/src/{credentials,client-host,runtime,runtime-native,runtime-backends,main,workspaces,policy}.mjs`、`apps/desktop/test/*`（增量） |
| W3 WSL 运行时 | 自包含 Linux 运行时（python-build-standalone + site-packages + ddp 包） | `scripts/build_wsl_runtime.py`、`packaging/windows/wsl-runtime-lock.json`、`packaging/windows/README.md`、`tests/test_wsl_runtime_build.py` |
| W4 打包更新 | Electron win 锁、electron-builder、Windows 构建/更新分支 | `packaging/windows/{electron-lock.json,electron-builder.yml}`、`scripts/build_desktop.py`、`scripts/update_check.py`、`tests/test_desktop_release.py` |
| W2W WSL 桥 | 检测/安装/启动/停止/孤儿清理、stdout bootstrap、bundle 校验转接 | `apps/desktop/src/runtime-wsl.mjs`、`runtime-backends.mjs`（注册）、`python/ddp_local/ddp_local/cli.py`（仅两个增量）+ 测试、`apps/desktop/src/workspaces.mjs`（WSL 映射，增量） |
| W5 CI/smoke | windows-latest 工作流、Windows 版 smoke | `.github/workflows/desktop-windows.yml`、`apps/desktop/scripts/smoke-windows.mjs` |
| W6 文档 | 安装/模式/更新/支持矩阵 | `docs/refactor/RELEASE-MANUAL-v3.md`、`docs/DEPLOY.md`、`docs/refactor/COMPATIBILITY-MATRIX-v3.md` |

## 验收门

- **全阶段**：`./scripts/check.sh` 29/29 不回退；`node --test
  apps/desktop/test/*.test.mjs` 全绿；每个新守卫做变异确认。
- **W1**：Windows 分支单测（platform 注入）；Linux 行为不变；无 WSL 时本地模式
  「不可用 + 引导」，不是崩溃或假就绪。
- **W3**：产物在 `ubuntu:24.04` 容器里解压即用（无系统 Python）；摘要/签名校验；
  两次构建可复现。
- **W2W**：Linux 上用假 `wsl.exe` 垫片跑通真实启动/握手/停止；`--token-file -`
  与 stdout `pid` 有单测；WSL 检测三态。
- **W4**：更新器 Windows 分支正负用例（平台归一、zip、`.exe`、manifest 拒绝
  非 windows）；electron-builder 配置可在 CI 出 NSIS + 便携；Linux 打包行为
  不变。
- **C 组实机矩阵**（必须真机/WSL）：无 WSL / WSL1 / 未配置 / 全链（导入中文 PDF
  →解析→检索→bbox→本地模型→Wiki→导出）/ 退出无孤儿 / 版本错配拒绝 /
  token 不落 Windows 盘。

## 明确不做

原生 Windows 本地运行时、strong/unelevated 分级沙箱、ARM64、MSIX/winget/
Authenticode、macOS。

## 主要风险

| 风险 | 对策 |
|---|---|
| WSL localhost 转发差异（NAT/mirrored） | 握手失败显式报错，不静默重试 |
| `wsl.exe` 被杀而内部进程存活 | 内部 PID 定向 kill + 启动孤儿清理 |
| electron-builder Linux 需 wine | 主路径 windows-latest CI；Linux 仅目录/便携兜底 |
| asar 与 `client-runtime/src/*.ts` 动态导入 | 打包显式 `files`/`extraResources` + 包内 smoke |
| 安装包体积 | 模型按需下载；后续可出 lite 版 |
| 无签名 SmartScreen | 文档写明，Authenticode 留后续 |
