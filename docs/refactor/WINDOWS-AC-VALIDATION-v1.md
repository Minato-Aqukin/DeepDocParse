# Windows A+C 切片验证记录（v1）

日期：2026-09-14。范围：**Tier A（只连中心的 exe）+ Tier C（WSL2 本地模式）**；
**不含原生 Windows 本地运行时**（`WINDOWS-AC-PLAN-v1.md`「明确不做」）。
执行权威是 `WINDOWS-AC-PLAN-v1.md`；宽粒度工作划分 W1–W6 见其表格
（本轮细化为 W1a/W1b/W1c/W2W/W3/W4b/W5）。

> **本文所有绿色都来自本机 Linux**（Arch/CachyOS，无 N 卡，未装 WSL）。
> **Windows 真机、真实 WSL2、真实 `wsl.exe`、CI workflow 一次都没跑过** ——
> 「本机已验证」绝不能读成「Windows 上可用」。未跑的格子一律 ⬜。

## 0. 一页结论

| 切片 | 本机（Linux） | 证据 | Windows/WSL2 实机 |
|---|---|---|---|
| W3 自包含运行时 tarball | ✅ 构建 + 容器烟测 + 三次同哈希 | `packaging/windows/README.md` | ⬜ 没在 WSL2 里解压运行过 |
| W4b 目录包 zip（win32-x64） | ✅ 构建 + 自检 | `dist/desktop/deepdocparse-0.1.0-win32-x64.release.json` | ⬜ |
| W4b 便携 exe（Linux 构建） | ✅ 构建 + 校验带负载 | `dist/desktop/windows/`，本文 §3.2 | ⬜ 没在 Windows 上双击 |
| W4b NSIS 安装器 | ⛔ 本机只得到卸载器 stub | `packaging/windows/README.md`「Build hosts」 | ⬜ 只能等 `windows-latest` |
| W1a/W1b 宿主/平台分支 | ✅ 57 passed / 0 skipped | 本文 §4 | ⬜ 真 NTFS ACL/DPAPI 未验 |
| W2W WSL 桥（垫片） | ✅ 8 条 WSL 用例 + 真 tarball 全链 | 本文 §5 | ⬜ 真 `wsl.exe`/PID 命名空间未验 |
| W5 CI workflow | ⬜ 首次 push 前未运行 | `.github/workflows/desktop-windows.yml` | ⬜ |
| C 组实机矩阵（无 WSL / WSL1 / 全链 / 孤儿 / token 不落盘…） | ⬜ 一条都没跑 | `WINDOWS-AC-PLAN-v1.md`「验收门」 | ⬜ |

## 1. 分层与前提

- **Tier A（远程）**：Windows 宿主直接用现有 `client-runtime` 走 HTTPS 连中心，
  不需要 Python、不需要 WSL；中心仍是 Linux/WSL2。
- **Tier C（本地）**：`wsl.exe -d <distro>` 启动随包捆绑的 Linux 运行时，
  经 `127.0.0.1:port` 走 `ddp-client/1` 握手。无文件系统桥；导入/导出走字节流 HTTP。
- 本机没有 Windows，也没有 WSL；所有 WSL 路径都是**垫片**（假 `wsl.exe`）或
  Linux 进程代替，见 §5。

## 2. W3：自包含 WSL 运行时（Linux 侧）

数值全部来自仓库内记录，非重跑现算；本次只复跑了测试计数。

| 项 | 实测值 | 来源 |
|---|---|---|
| 归档 | `deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz` | `packaging/windows/README.md` |
| 大小 | 128 592 543 字节（13:22 重建） | 同上；与 `dist/wsl/*.sha256`、`win-unpacked/resources/*.sha256` 一致 |
| SHA256 | `12cfa2f3f9111d3a699cd96978ef77670ff80300fb920feffbb152f4a6c44252` | 同上 |
| manifest 文件数 | 6033（stdlib + 三方字节码预编译） | 同上 |
| 解释器 | CPython 3.12.14，`cpython-312`，SOABI `cpython-312-x86_64-linux-gnu`，`x86_64` | 同上 |
| standalone 输入 | 111 368 545 字节 / `936c246d…ffa22` | 同上 |
| wheel | 18 个，共 15 383 217 字节 | 同上 |
| 守卫测试（离线） | 17 passed, 1 skipped（skip 名 `WSL_RUNTIME_BUILD`） | 同上；本轮复评修复后复跑 |

> **这些数字在 13:22 变过一次，原因是 cli.py 增量**：本轮给 `serve` 加了
> stdout `pid` 与 `--token-file -`（计划决策 3），打进包的 `ddp_local` 源码变了，
> 运行时 tarball 随之重建。11:55 那次构建的 pins 是
> 128 592 559 / `ac418ea0…b44e`（`dist/wsl-repro-a|b` 仍是那一版的产物），
> **本节记录当前树上的这一版**；`dist/wsl` 的 pins 随源码树变化。

- **`ubuntu:24.04` 无系统 Python 容器烟测**（`docker run`，`/dist` 只读）：
  `command -v python3` 无输出；`import ddp_contracts, ddp_core, ddp_local, fastapi,
  httpx, pypdfium2, PIL` 成功（0.249s）；`capabilities` 报 `parse.available=true`
  与 `degraded=["embedding_unavailable","vision_unavailable"]`；捆绑解释器里
  `pip`/`ensurepip` 均不存在；launcher 拒绝 PID 1 父进程（docker 即 PID 1，
  用 `sh -c` 保活一个非 1 父进程后通过）；畸形 bundle 让 `runtime-files.py`
  退出 1；脚本以 `SMOKE-OK` 结束。全文与输出见 `packaging/windows/README.md`。
- **确定性**：三次 `--skip-download` 构建（不同 `--output`）得到同一 SHA256，
  stage 路径不入档（`compileall -d` 重写 `co_filename`、`PYTHONHASHSEED=0`、
  mtime 钉死 `SOURCE_DATE_EPOCH`/1789257600、tar 排序、gzip `-n`）。
- **变异确认（4 条，已还原）**：skip-download 拒绝、改文件被检测、gzip 头确定性、
  `compileall -d` 路径无关 —— 改掉被守的那行都会红。见 README 同节。
- **最小发行形状门**（二轮复评 F1 残留，本轮修复）：`build_wsl_runtime.py` 定义
  `MIN_UNPACKED_BYTES = 100_000_000`、`MIN_FILES = 5_000`、
  `MIN_INTERPRETER_BYTES = 5 MiB`，`--verify` 与打包器共用同一份实现；必需要求
  由 pinned lock 的 `python.major_minor`、`local_packages`、`roots` 推导
  （`pillow → PIL/Image.py`），不再维护第二份硬编码清单。**它是自洽性下限，
  不是签名** —— 边界见 §3.4。
- ⬜ 未验证：真实 WSL2 内核/发行版上解压启动、`/mnt/c` 之外的用户目录权限、
  冷启动耗时。**本机跑的是 Linux 内核。**

## 3. W4b：打包（Electron win32-x64）

### 3.1 目录包与 Electron pin

- `scripts/build_desktop.py --platform win32-x64` 产出的目录包 zip：
  291 949 230 字节，SHA256 `bd6daf5bb4d82bf03585f0ecadfc4250d9c8419434f59b7d2462100c9315a717`，
  `python` ABI 为 WSL 运行时的 `cpython-312`/`x86_64`。
  来源：`dist/desktop/deepdocparse-0.1.0-win32-x64.release.json`（+ `.zip.sha256`）。
- Electron 归档 pin：`electron-v44.3.0-win32-x64.zip`，158 149 320 字节，
  SHA256 `26bf9a617d58d81772b3d68305d59ee48272969c15083c06db634a77358a8d9d`
  （`packaging/windows/electron-lock.json`；来源是官方 `SHASUMS256.txt`，是校验和
  pin，不是签名声明）。
- `build_desktop.py` 对 W3 tarball 是 **fail closed**：合成替身/占位符直接拒绝，
  没有跳过开关（`packaging/windows/README.md`「Packaging and package verification」）。
  二轮复评构造了一个**全自洽的 4 成员伪造包**（shell-stub python + 1 MiB padding
  + 真 lock + 逐成员正确摘要的 inner manifest）并通过全部摘要交叉核对；本轮加入
  最小发行形状门后它被拒绝，回归用例
  `test_wsl_runtime_pin_rejects_self_consistent_miniature`（去掉形状检查即红）。

### 3.2 便携 exe（Linux 构建成功）

`electron-builder@26.15.3` 在 Linux 上 `--win portable` 成功，并把真负载打进
`$PLUGINSDIR/app-64.7z`（`packaging/windows/README.md`「Build hosts」）。
本机留存的产物（本轮重哈希，路径即证据）：

| 产物 | 大小 | SHA256 |
|---|---|---|
| `dist/desktop/windows/DeepDocParse-0.1.0-win-x64-portable.exe` | 223 335 967 | `b53e83b6748aad220bd158846925be826e656c1f98d080052351df6c4007409b` |
| `dist/desktop/windows/win-unpacked/deepdocparse.exe` | 246 070 272 | `e048632e03fabc96de5e10afbebc0aa31f4e1674155ea3d270597fe7523f9d80` |

- 打包后校验（本轮实跑 `scripts/verify_windows_package.py`）：目录检查通过；
  便携 exe 的负载下限检查通过；打包树里的
  `resources/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz` 与
  `dist/wsl/wsl-runtime.json` **逐字节一致**（size 128 592 543 /
  SHA256 `12cfa2f3…4252`，sidecar 也一致）。
- **NSIS 在 Linux 上出不来，这是有记录的拒绝**：没有 wine 时 electron-builder
  死在 `wine process failed ENOENT`，只留下 ~190 KB 的卸载器 stub
  （`dist/desktop/windows/DeepDocParse-0.1.0-win-x64-setup.exe`，实测 189 816 字节）
  和完整的 `dist/desktop/windows/@ddpdesktop-0.1.0-x64.nsis.7z`（237 318 861 字节）；
  stub **不是可发布安装器**。`verify_windows_package.py` 的 100 MiB 下限会拒绝它
  （本轮实跑输出：`installer … is only 189816 bytes; the payload is missing
  (an electron-builder uninstaller stub?)`）。setup.exe 只能由 `windows-latest`
  产出（W5，见 §6）。⬜ 真实 NSIS 安装器未构建过。

### 3.3 fail-closed 守卫与变异确认（上轮 3 条 + 本轮 4 条，改完还原）

| 守卫 | 代码位置 | 变异 | 变红的用例 |
|---|---|---|---|
| 目录包内捆绑运行时必须与 release manifest 逐字节一致 | `scripts/build_desktop.py:710` | 删掉 digest/size 比较，只留 `is_file()` | `test_verify_windows_directory_rejects_wsl_runtime_drift` |
| 安装器负载下限 100 MiB | `scripts/verify_windows_package.py:34` | `100 << 20` → `0` | `test_verify_windows_package_installer_payload_floor` |
| 跨系统 manifest 拒绝（Windows 包不许在 Linux 上 apply） | `scripts/update_check.py:370` | `if system != host_system()` → `if False` | `test_cross_system_manifests_are_rejected` |
| W3 tarball 最小发行形状（100 MB / 5 000 文件 / 5 MiB 解释器） | `scripts/build_wsl_runtime.py` `shape_problems`（`build_desktop.py:587` 调用） | 把形状检查改为 `violations = []` | `test_wsl_runtime_pin_rejects_self_consistent_miniature` |
| 打包目录内的 `BUILD-MANIFEST.json` 不得作为锚 | `scripts/build_desktop.py:760` | 命中 in-dir manifest 时把它当 anchor | `test_verify_packaged_windows_ignores_in_dir_manifest` |
| `resources/app/**` 全量进负载 diff | `scripts/build_desktop.py:785` | 覆盖范围缩回 `*.mjs`/`*.ts` | `test_verify_packaged_windows_covers_all_app_files` |
| `build_wsl_runtime.py --verify` 走形状门 | `scripts/build_wsl_runtime.py:681` | `shape=True` → `shape=False` | `test_cli_shape_check_refuses_the_offline_fixture` |

前三条是上一轮的记录（本轮复跑；位置行号已按当前树更新，`build_desktop.py`
那处从旧文写的 445 变成 710）。后四条是本轮新增，同样每条实测
「破坏 → 红 → 还原」，且每处替换都先确认真的改到了那一行（`grep`/断言过）。
其余 W4b 守卫见 `tests/test_desktop_release.py`（本轮 **61 passed**，见 §7）。

### 3.4 二轮复评的 F1/F2 残留与诚实边界（本轮修复）

二轮复评给出的三条残留与处理：

1. **F1（MAJOR）：全自洽伪造包可通过。** 复评者用真 lock 复制件 + shell-stub
   python + 1 MiB padding + 逐成员摘要正确的 inner manifest 组了一个包，
   所有摘要都重算得上，因此通过当时的 `--verify`。修复不是签名（决策 6 已锁：
   v1 不签名），而是**最小发行形状 + 构建来源核对**：形状下限见 §2；来源核对
   仍是 `lock_sha256` 与仓库内 `packaging/windows/wsl-runtime-lock.json`
   一致（存在时）。验收命令
   `pytest test_wsl_runtime_build.py::test_cli_shape_check_refuses_the_offline_fixture`
   与 `test_desktop_release.py::test_wsl_runtime_pin_rejects_self_consistent_miniature`
   都做了变异确认。
2. **F2a（MINOR）：`verify_packaged_windows` 会信任被验目录内部的
   `BUILD-MANIFEST.json`（自指）。** 现在锚点顺序是：显式 `--stage` →
   输出目录旁边的组装 stage → 仓库源码；被验目录里的 manifest 一律忽略。用例
   `test_verify_packaged_windows_ignores_in_dir_manifest`。
3. **F2b（MINOR）：负载 diff 只覆盖 `src/*.mjs` + `client-runtime/*.ts`。**
   现在覆盖 stage manifest / 仓库里 `resources/app/**` 与
   `resources/client-runtime/**` 的**每一条**，`preload.cjs`、
   `runtime-launcher.py`、`runtime-files.py`、`ui/**` 都在内；`package.json`
   仍走 canonical view。用例 `test_verify_packaged_windows_covers_all_app_files`。

**诚实边界（文档口径，README 同款）**：这些检查是**对已固定公开输入的自洽性
核对（identity + digest + shape + 构建 lock 摘要），不是密码学来源证明**。
v1 不签名（计划决策 6），所以**一个有构建访问权的蓄意攻击者仍能伪造一个
足够大、字节与自家 manifest 相符的捆绑包**；形状门只是把伪造成本抬高，既不是
签名，也不得被描述成签名。现有的一切绿色都不提供 Authenticode / 签名保证。

## 4. W1a/W1b：宿主抽象与后端缝（Linux 侧）

- **用例计数**（W1c 后实跑，含真 W3 tarball 用例；F6 修 `wsl.exe` 解析时又加
  了一条 System32 路径用例）：`node --test apps/desktop/test/*.test.mjs` →
  `tests 57 / pass 57 / skipped 0`；
  `DDP_TEST_FORCE_WIN32=1` 同一套 → `pass 52 / skip 5`，skip 全部带理由
  「native runtime is Linux-only in the A+C slice」，且只覆盖
  `runtime.test.mjs`（2 条真原生运行时）与 `client-host.test.mjs`（3 条原生链路）。
  这组门控让 fresh windows-latest checkout 不会因缺 `.venv`/`/usr/bin/python3` 而红，
  也不静默放过原生用例。
- **后端缝**（`apps/desktop/src/runtime-backends.mjs`）：`createRuntimeBackend`
  只认 `kind: native | wsl`，返回统一的 `{name, isolation, spawn, stop, validateBundle}`；
  `OwnedRuntimeManager` 对任意后端走同一套 spawn→ready→handshake→wake→stop
  （`runtime-backend.test.mjs` 用假后端断言，并断言 `spawn` 只收
  `sessionDir/tokenFile/workspace` 三个键、`stop` 只收 `{graceMs:3000}`）。
  `wsl` 分支动态 import；WSL 缺失/异常统一折成 `wsl_backend_unavailable`，
  本地模式「不可用 + 引导」，不是崩溃或假就绪。
- **win32 分支靠 `platform` 注入测试**（`secureDirectory(…, {platform:'win32'})`、
  `runtimeEnvironment(…, 'win32', source)`）：NTFS 不做 mode 检查、报
  `ntfs_acl`；`runtimeEnvironment` 的 win32 只保留 `PATH/SystemRoot/SystemDrive/
  TEMP/USERPROFILE`，丢掉 `HOME/LANG/SERVICE_TOKEN`。
- ⬜ 未验证：真实 NTFS ACL/重解析点、DPAPI 持久化（只测了「加密不可用即
  session-only，原因 `session_only_dpapi_unavailable`」的分支）、真实 Windows 上的
  单实例锁与退出对话。
- **Windows 第二启动的 WSL 工作区**已由 W1c 修复：`client-host.mjs` 现在持久化并
  按 `workspaceKind` 分派（native → `selectedByNativeDialog`，wsl → `selectedWsl`），
  WSL 路径不再被判成非法绝对路径；回归用例 + 变异确认见 §8。
- **`DDP_TEST_FORCE_WIN32=1`**（W1c 新增，仅测试门控）：在 Linux 上把
  「win32 该跳的原生用例」演练成 skip，便于本地证明门控不是恒真；真实 win32
  语义（NTFS/符号链接/DPAPI）仍只有 Windows 机器能验。

## 5. W2W：WSL 桥（垫片测试，非真 `wsl.exe`）

- **先说清楚**：`apps/desktop/test/runtime-wsl.test.mjs` 的 WSL 用例全部跑在
  `apps/desktop/test/helpers/wsl-shim.sh`（假 `wsl.exe`）上，非 Linux 平台直接
  skip（`linuxOnly`，理由写着「Windows coverage is W5 smoke」）。
  加真 tarball 的那条在 Linux 上解压并用捆绑解释器启动，走的是**真 PID 与真回环
  端口**，但「发行版/PID 命名空间/转发」仍是 Linux 进程模拟。
- **检测与拒绝码**（垫片喂固定的 `wsl.exe -l -v` 输出，UTF-16LE 解码）：
  `* Ubuntu-22.04 Running 2` 为默认 → `{status:ok, distro:'Ubuntu-22.04'}`；
  `Debian Stopped 1` → `wsl1_unsupported`；缺失可执行 → `wsl_missing`；
  列表失败/垃圾输出 → `wsl_unavailable`；指定发行版不存在 → `wsl_distro_not_found`；
  默认发行版歧义 → `wsl_unavailable`；非法发行版名 → `invalid_wsl_distro`。
- **provision 失败即清根**：tarball size/sha 对不上、manifest 非法、
  安装后 ABI 探针与 manifest 不符 → 拒绝，且 `~/.deepdocparse/runtime` 不残留
  （ABI 不符时连 `.tmp-` 也不留）。
- **真 W3 tarball 全链**（本机 Linux）：`prepare()` 装到
  `~/.deepdocparse/runtime`，写 `INSTALLED.json`（version/sha256）；重复 prepare
  不重装也不动目录里额外文件；`spawn` 后 stdout bootstrap 解析出
  `http://127.0.0.1:<port>` + token（32–128 字符）；`pid` 不等于 relay 的 pid
  （垫片上即本机 Linux 进程；WSL 里应是发行版内部 pid）；握手返回 `ddp-client/1`；`stop` 只经
  `wsl.exe -d <distro> -- kill -TERM <innerPid>`（测试断言末两参是
  `['-TERM', String(innerPid)]`），**从不下发 `--terminate`**。
- **孤儿清理**：只杀 session 目录里记录过、且 `ps -o args=` 里带我们
  `runtime-launcher.py` 路径的 pid；外来进程与死 pid 不碰，处理完删 session 目录。
- **`--token-file -` 与 stdout bootstrap**（`python/ddp_local/tests/test_cli_serve.py`
  本轮实跑 **3 passed**，含 F3 的 SIGTERM 早处理器回归）：
  - `serve --token-file -`：stdout 一行（含 `url`/`token`/`pid`；测试断言
    `bootstrap['pid'] == process.pid`、无 `token_file` 键），**任何位置都不写文件**
    （不写 `-`、不写 `session-*.json`）；HTTP 握手可用。
  - 缺省/文件模式：stdout 里**没有** `token`，只有 `token_file`（0600，JSON 内含
    token）；进程收尾后文件删除。文件模式行为未变。
  - WSL 桥消费的正是 stdout 形态：解析 url/token/pid，并按**内部 pid** 停止。
- ⬜ 未验证（Windows/WSL2 实机才有的）：真 `wsl.exe` 的输出编码/退出码/本地化差异；
  WSL localhost 转发在 NAT 与 mirrored 两种模式下的差异（握手失败必须显式报错）；
  真 PID 命名空间里的 `kill`/`ps`（发行版缺 `ps` 时孤儿清理退出 2、不误杀但也不清理）；
  首次调用 `wsl.exe` 的冷启动延迟（桥的缺省 `startupMs=20000`、`provisionMs=600000`
  从未在真机标定）；打包版 smoke 的真实窗口（见 §6）。

## 6. W5：CI workflow（读完的静态事实，非运行结果）

`.github/workflows/desktop-windows.yml`，触发 `push` 到 `codex/desktop-federation-v3`
与 `workflow_dispatch`：

- **为什么两个 job**：W3 的 `build_wsl_runtime.py` 在非 Linux 上主动拒绝
  （`only Linux x86_64 WSL runtimes have been validated`），且要执行随包的 Linux
  解释器；根守卫 `tests/test_desktop_release.py`/`tests/test_wsl_runtime_build.py`
  是 POSIX-only（模块级 `os.uname()`、假解释器是 `#!/bin/sh`）。所以：
  - `wsl-runtime`（`ubuntu-latest`，60 分钟）：安装 `pytest`+`packaging` → 跑 W4/W3
    守卫 → 缓存/构建 W3 tarball（缓存键含 `wsl-runtime-lock.json` 哈希）→
    `--verify` → 上传 tarball+sidecar+manifest 制品（7 天）。
  - `package-windows`（`windows-latest`，120 分钟，`needs` 上者）：Python 3.12 +
    Node 24；`npm ci --registry=https://registry.npmmirror.com`；桌面主机测试；
    `npm run web:build`；按 `electron-lock.json` 下载并校验 Electron；`build_desktop.py
    --platform win32-x64`；`npx --yes electron-builder@26.15.3 --config
    packaging/windows/electron-builder.yml --projectDir . --publish never
    --win nsis portable --x64`（`--publish never` 是首跑后补的：不加时 electron-builder
    构建完两个 exe 会默认尝试发布到 GitHub Releases，无 `GH_TOKEN` 即失败）；
    `verify_windows_package.py`；生成逐文件 `.sha256` + `SHA256SUMS`；上传安装器制品。
- **CI 首跑已发生**（见 §9）。workflow 的「首跑前未运行过」注释只描述提交时的状态；
  截至 2026-09-14 已跑到 electron-builder 之后一步，途中五个真实缺陷全部修复。
  打包版 GUI smoke、WSL spike 结果与最终产物的实机验证仍待后续运行。


- WSL spike 是**诊断**（计划决策 5：只打印 `wsl.exe --status`/`-l -v`，不装发行版、
  不碰 reboot/admin，`continue-on-error`）；打包版 GUI smoke 首轮也是
  `continue-on-error`（runner 会话未必能开窗口；原先的
  `--smoke` 文案不一致已解决，见 §8.2）。
- 硬编码的 `0.1.0` 已从 verify/upload 步骤移除：tarball 名按 glob 取并要求唯一，
  版本随 `--version` 走（F9）。
- 安装器名（`artifactName`）：`DeepDocParse-<version>-win-x64-setup.exe` /
  `DeepDocParse-<version>-win-x64-portable.exe`；目录包：
  `deepdocparse-<version>-win32-x64.zip`。

## 7. 本机实跑命令与结果汇总

```bash
node --test --test-timeout=30000 apps/desktop/test/*.test.mjs   # 57 passed, 0 failed, 0 skipped
.venv/bin/python -m pytest tests/test_desktop_release.py -q     # 61 passed
.venv/bin/python -m pytest tests/test_wsl_runtime_build.py -q   # 17 passed, 1 skipped
cd python/ddp_local && ../../.venv/bin/python -m pytest tests/test_cli_serve.py -q   # 3 passed
.venv/bin/python scripts/verify_windows_package.py dist/desktop/windows/win-unpacked \
  --installer dist/desktop/windows/DeepDocParse-0.1.0-win-x64-portable.exe          # 通过
```

## 8. 与实现/文档的不一致（报告，未修代码）

1. ~~`DDP_TEST_FORCE_WIN32=1` 不存在~~ **已解决**：W1c 在
   `apps/desktop/test/helpers/platform.mjs` 增加了该开关（另有
   `posixFilesystemSkipReason`），并在 Linux 上实跑 `pass 52 / skip 5` 验证门控。
2. ~~打包版 smoke 文案停在旧状态~~ **已解决**：`main.mjs` 早已接受
   `DDP_DESKTOP_SMOKE==='1' && (!app.isPackaged || process.argv.includes('--smoke'))`；
   本轮已同步 `apps/desktop/scripts/smoke-windows.mjs` 的头部/失败文案与
   workflow 注释。打包版 smoke 仍属 Windows-only 未验证项。
3. **`python/ddp_local/docs/local-api.md` 的 bootstrap 描述落后于实现**：
   原文只列 `{url,token_file,protocol_version,identity,profile,capabilities}`，
   未写永远存在的 `pid` 与 `--token-file -` 的 stdout 形态。已在本轮文档中补齐
   （见该文件）。

## 9. CI 首跑记录（2026-09-14，windows-latest）

`desktop-windows.yml` 第一次真跑一路暴露的都是「Linux 测试形状看不见」的跨平台缺陷，
逐个修复并推送（每个 commit 都先过独立验收）：

| # | 失败点 | 根因 | 修复 |
|---|---|---|---|
| 1 | 桌面主机测试 | `CredentialBroker` 把「凭证策略平台」与「文件系统安全门平台」混用一个参数；Windows 上验证 Linux secret-service 语义时对 NTFS 跑了 POSIX mode 检查 | `d915c83`：拆出 `directoryPlatform`，回归测试 + 变异确认 |
| 2 | 共享 Vue UI 构建 | 根 lockfile 在 Linux 生成，缺 vite 依赖链（rolldown/lightningcss）的 **win32 原生绑定**，`npm ci` 装不出来 | `4a8df74`：把 win32 绑定声明为 `apps/web` 的 optionalDependencies 并重生成 lockfile |
| 3 | 组装 win32-x64 | Windows 检出把 `packaging/windows/wsl-runtime-lock.json` 转成 CRLF，摘要 `57489d78` → `02961a3e`，与 WSL tarball 内嵌 pin 不符 | `d1e3440`：`.gitattributes` 对 `packaging/{windows,arch}/*.json` 钉 `text eol=lf` |
| 4 | 组装 win32-x64 | F1 守卫用 `os.path` 解析 tar 成员名；Windows 上 `bin/2to3 → 2to3-3.12` 变成反斜杠名，符号链接全部读作「listed but absent」 | `9596fe5`：两个解析器改 `posixpath`，ntpath 垫片回归 + 变异确认 |
| 5 | electron-builder | 两个 exe 已构建成功后，electron-builder 默认尝试发布到 GitHub Releases，无 `GH_TOKEN` 报错退出 | 本次：固定调用加 `--publish never`（workflow、yml 头注释、发布手册同步） |

第 5 轮之前，桌面主机测试、Vue UI 构建、Electron 下载校验、win32 目录组装均已在
windows-latest 上真实通过；NSIS 与便携 exe 也已在本轮真实构建出来，失败发生在其后的
发布步骤。**CI 通过的最终结论以 workflow 的绿色运行为准，本文不代替它。**
