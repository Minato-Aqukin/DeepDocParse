# 桌面端发行手册（v3，本机可验证部分）

> 2026-09-13。本文只写**这台机器上真正跑过**的构建、安装、更新、回滚、备份
> 流程；跑不了的（签名发布、AUR、GPU、生产迁移）逐条标出。所有数字都附
> 日志或产物路径。
>
> 容量数字在 `CAPACITY-LOCAL-v3.md`，那是本地 CPU 环境，不是生产能力承诺。

## 0. 一页结论

| 能力 | 状态 | 证据 |
|---|---|---|
| 可复现目录包构建（Linux x86_64） | ✅ 三次构建同一 SHA256 | `dist/desktop/*.tar.gz.sha256`，`/tmp/opencode/repro-final.log` |
| Electron 下载校验 | ✅ 122830582 字节 / SHA256 与 pin 一致 | `packaging/arch/electron-lock.json`，`build_desktop.py --verify-electron` |
| 运行期依赖锁 + 许可清单完整性 | ✅ 18 个发行包 + Electron 两份声明，缺一即失败 | `BUILD-MANIFEST.json` 的 `license_manifest`，`--verify` |
| Arch 包（makepkg） | ✅ `--verifysource` + 两次构建同 SHA256 | `/tmp/opencode/arch-reproduce.log`，`c9353b42…` |
| 桌面真实冒烟（Wayland） | ✅ Electron 44.3.0 / 26 个主机测试 | `apps/desktop/artifacts/smoke-report.json` |
| 包外 CPU 烟测（不带 checkout） | ✅ parse/search/evidence/bundle 全通 | 本文 §2.4 |
| 更新/回滚（真实 0.1.0→0.1.1→0.1.0） | ✅ 校验、备份、原子交换、模型零改动 | `/tmp/opencode/update-demo.log` |
| 更新守卫测试 | ✅ 20 passed；8 处变异确认 | `tests/test_desktop_release.py` |
| DB 升级前备份命令 | 🟡 只在 dev 库上验证过命令本身 | `/tmp/opencode/db-backup.log` |
| 发布签名基础设施 / AUR / 其他平台 / GPU | 🔴 未做，见 §7 | — |

## 1. 构建前提（本机实测）

- Arch/CachyOS，x86_64，Python 3.14.7（系统 `python`，ABI `cpython-314`）。
- Node 26.8.2 / npm 12.0.2；Electron 44.3.0 已按 `apps/desktop/package-lock.json`
  装好（`apps/desktop/node_modules/electron`）。
- 仓库 `.venv`（Python 3.14）里有 `packaging`。
- 构建**自身不需要网络**；只在 `npm ci` / 首次下 Electron 时需要。

Electron 下载的独立锚是 `packaging/arch/electron-lock.json`：

```json
{"version": "44.3.0", "file": "electron-v44.3.0-linux-x64.zip",
 "size": 122830582,
 "sha256": "8b49b9efdd73c0f467edc3c1cd5678392c384ccf224f34ff54179f736e2f384b"}
```

本机实际校验（`/tmp/opencode` 里那条命令的输出）：

```console
$ sha256sum apps/desktop/artifacts/download/electron-v44.3.0-linux-x64.zip
8b49b9efdd73c0f467edc3c1cd5678392c384ccf224f34ff54179f736e2f384b  apps/desktop/artifacts/download/electron-v44.3.0-linux-x64.zip
$ .venv/bin/python scripts/build_desktop.py --verify-electron
{"electron": {"version": "44.3.0", "file": "electron-v44.3.0-linux-x64.zip",
              "size": 122830582, "sha256": "8b49b9ef…f384b"}}
```

## 2. 构建（目录包 → Arch 包）

### 2.1 完整流程（本机跑过的原命令）

```bash
# 1. 共享 Vue UI（仓库根）
npm run web:build

# 2. 桌面主机测试（26 条）
node --test --test-timeout=30000 apps/desktop/test/*.test.mjs

# 3. 目录包：锁校验 + Electron 校验 + 许可清单 + tar.gz + release.json
.venv/bin/python scripts/build_desktop.py

# 4. 产物自检（重算每个文件、runtime-lock、许可清单、ABI）
.venv/bin/python scripts/build_desktop.py \
  --verify dist/desktop/deepdocparse-0.1.0-linux-x64

# 5. 包外 CPU 烟测（系统 /usr/bin/python3 + 包内依赖，无 checkout）
node apps/desktop/scripts/package-smoke.mjs \
  dist/desktop/deepdocparse-0.1.0-linux-x64 tests/fixtures/sample.pdf

# 6. Arch 配方：准备 + makepkg（不安装、不发布）
scripts/build_desktop_arch.sh --build        # 或 --reproduce 连打两次比哈希
```

2026-09-13 的实测输出摘要：

- 第 2 步：`ℹ tests 26 / pass 26 / fail 0`。
- 第 3 步：`942` 个文件落进 `BUILD-MANIFEST.json`；`licenses` 覆盖
  Electron 的 `LICENSE` + `LICENSES.chromium.html` 与全部 **18** 个 Python 发行包；
  `dist/desktop/deepdocparse-0.1.0-linux-x64.release.json` 随后写出
  `sha256=daaf1e88396435accc0ace446174bf44d8310a9a443aed0bbea4171b70bbef64`。
- 第 4 步：`{"files": 942, "distributions": 18, "electron": "44.3.0", "version": "0.1.0"}`。
- 第 5 步：`{"passed":true, … "parse":"succeeded","hits":1,"evidenceBBox":[…],
  "bundleBytes":3896,"stopped":"stopped","sqliteDraftAndReceipt":true}`。
- 第 6 步：`makepkg --verifysource` 三个 source 全部 `通过`；两次 `makepkg -f`
  产物同为 `c9353b42f61ee57550c3075366b7a21c05fc8e6f79896584f1a6dfa9cb1d591e`，
  120 691 965 字节。

### 2.2 可复现性（本机验证）

- **目录包**：同一输入连打三次（含 `--output` 到不同目录）：
  `daaf1e88…` ×3，见 `/tmp/opencode/repro-final.log`。
- **Arch 包**：不加 `SOURCE_DATE_EPOCH` 时两次构建的 `.BUILDINFO`
  `builddate` 是墙钟时间，包哈希必然不同（本机实测 `269736ac…` vs `e06e6f98…`）。
  修复：`scripts/build_desktop_arch.sh` 从产物 `BUILD-MANIFEST.json` 读
  `epoch` 并把它作为 `SOURCE_DATE_EPOCH` 传给 makepkg；随后
  `--reproduce`（连打两次）输出 `first == second == c9353b42…`，
  `/tmp/opencode/arch-reproduce.log` 末尾是 `REPRODUCIBLE`。

### 2.3 许可清单完整性

`build_desktop.py` 现在**生成并校验** `license_manifest`：Electron 两份声明
+ 每个锁定依赖 `dist-info/licenses/**` 的文件清单与 SHA256。任何一个锁定
依赖或 Electron 声明缺失，构建当场失败（测试
  `test_license_manifest_requires_*` 钉住）。

### 2.4 GUI 冒烟（Electron 真实窗口）

```bash
env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=niri \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    npm run smoke --workspace @ddp/desktop
```

实测返回：`{"passed":true,"electron":"44.3.0","secretBackend":
{"backend":"basic_text","persistentAvailable":false,"reason":
"session_only_secret_service_required"},"display":{"wayland":"wayland-1","x11":":1"}}`；
截图与报告在 `apps/desktop/artifacts/{desktop.png,smoke-report.json}`。
本机 `basic_text` 意思是**凭证只进内存、不落盘**，不是缺陷。

## 3. 安装

### 3.1 Arch 包（推荐通道，AUR 尚未发布）

```bash
cd dist/desktop/arch
makepkg -f --noconfirm
sudo pacman -U deepdocparse-desktop-spike-0.1.0-1-x86_64.pkg.tar.zst
```

**本机只做到 `makepkg` 为止**（不装系统包、不动 sudo）：包已生成但从未
安装。依赖声明在 `packaging/arch/PKGBUILD`（`python>=3.14 <3.15`、gtk3、
nss、mesa 等）；`chrome-sandbox` 权限 755，**不允许** `--no-sandbox`。

### 3.2 目录包（开发/自部署）

```bash
sudo cp -a dist/desktop/deepdocparse-0.1.0-linux-x64 /opt/deepdocparse
sudo ln -sf /opt/deepdocparse/deepdocparse /usr/local/bin/deepdocparse
```

本机的更新演练就是在 `~/.cache/ddp-uptest/opt/deepdocparse` 这样的目录
根上做的（§4.2），没有动 `/opt`。

## 4. 更新与回滚（Linux 没有 autoUpdater）

Electron 在 Linux 上没有 Squirrel/autoUpdater 通道，更新完全由
`scripts/update_check.py` + 版本化 release manifest 承担。

### 4.1 通道形状

- 构建产出 `deepdocparse-<ver>-linux-x64.release.json`（含
  `sha256`/`size`/`build_manifest_sha256`/`python` ABI/`platform`）。
- 可选 detached 签名：`update_check.py sign --key <ssh-key> …`，签名块
  `scheme=ssh-ed25519`、`namespace=ddp-desktop-release`；用
  `--allowed-signers` 验证（`ssh-keygen -Y verify`）。**签名覆盖的是去掉
  signature 字段后的规范化 JSON**，避免自指。
- `verify` 顺序：签名 → manifest 形状 → 大小/SHA256 → 包内
  `BUILD-MANIFEST.json` 摘要 → 版本一致性 → 许可清单 → ABI/版本单调性。
- `apply`：先完整解包到同文件系统的 `<root>.staging-<pid>` 并重算整树；
  再把旧树 `rename` 成 `<root>.previous`（同文件系统原子操作），最后
  `rename` 新树就位。旧版本整棵保留，回滚不需要网络。
- `rollback`：当前树改名成 `<root>.rolledback-<ver>` 留证，再
  `rename` 回 `<root>.previous`。
- 中断语义：两次 rename 之间断电/被杀时 `<root>` 短暂不存在，但
  `<root>.previous` 完整可运行；`status` 报 `interrupted: true`，
  `rollback` 恢复；`apply` 在没有先回滚时拒绝（有 `--force-stale` 才覆盖）。
- 模型缓存：`--models DIR` 在升级前后各算一次树摘要，不一致或模型目录
  位于应用树内部都直接拒绝。

### 4.2 真实命令（本机 0.1.0 → 0.1.1，日志 `/tmp/opencode/update-demo.log`）

```bash
UP=$HOME/.cache/ddp-uptest
.venv/bin/python scripts/build_desktop.py --version 0.1.1 --output dist/desktop-update-demo

.venv/bin/python scripts/update_check.py status --root "$UP/opt/deepdocparse"
.venv/bin/python scripts/update_check.py verify --root "$UP/opt/deepdocparse" \
  --manifest dist/desktop-update-demo/deepdocparse-0.1.1-linux-x64.release.json \
  --archive  dist/desktop-update-demo/deepdocparse-0.1.1-linux-x64.tar.gz \
  --allow-unsigned
.venv/bin/python scripts/update_check.py apply  --root "$UP/opt/deepdocparse" \
  --manifest dist/desktop-update-demo/deepdocparse-0.1.1-linux-x64.release.json \
  --archive  dist/desktop-update-demo/deepdocparse-0.1.1-linux-x64.tar.gz \
  --allow-unsigned --models "$UP/var/models"
.venv/bin/python scripts/update_check.py status --root "$UP/opt/deepdocparse"
.venv/bin/python scripts/update_check.py rollback --root "$UP/opt/deepdocparse"
```

实测要点（日志逐字可查）：

- `apply` 输出 `{"applied": true, "version": "0.1.1", …
  "archive_sha256": "675f8a678392…"}`；`status` 随后显示
  `current.version=0.1.1`、`previous.version=0.1.0`。
- 模型文件 `sha256=19e74f44471a…` 在升级前后、回滚前后四次一致。
- `rollback` 输出 `{"rolled_back": true, "version": "0.1.0",
  "kept_copy": ".../deepdocparse.rolledback-0.1.1"}`。
- 降级（装 0.1.1、候选 0.1.0）默认被拒：
  `rejected: DOWNGRADE: 0.1.1 -> 0.1.0 … -- refusing`；加
  `--allow-downgrade` 后成功并打印警告。
- 真实签名验证：临时 ed25519 密钥签 0.1.1 manifest → 正确 allowlist
  `accepted`；篡改 manifest 后
  `rejected: … incorrect signature`；用别人的公钥当 allowlist
  `rejected: … signature mismatch`。

### 4.3 不带签名时的显式风险

未签名 manifest 默认拒绝；必须显式
`--allow-unsigned`（打印 `UNSIGNED release manifest accepted…`）。
**正式发布前必须换成签名通道**（§7）。

### 4.4 守卫测试与变异确认

`tests/test_desktop_release.py`：**20 passed**（`.venv/bin/python -m pytest
tests/test_desktop_release.py -q`）。8 处变异逐条确认过会红，然后原样还原：

| 变异 | 变红的测试 |
|---|---|
| SHA256 比较短路为假 | `test_bad_checksum_is_rejected` |
| 降级拒绝短路 | `test_downgrade_is_rejected_then_warns_with_flag` |
| 删掉签名失败分支 | `test_signed…wrong_signer`、`test_tampering…` |
| 整树校验短路 | `test_verify_directory_detects_modified_file` |
| 许可缺失短路 | `test_license_manifest_requires_*` |
| 中断时可运行标记 | `test_interrupted_update_leaves_old_version_runnable` |
| 中断标记反转 | 同上 |
| 模型目录越界检查短路 | `test_models_inside_root_are_rejected` |

## 5. 升级前数据库备份（服务端）

桌面目录包不带数据库；**服务端部署**升级前先备份。命令在 dev 栈真库上
跑过（`/tmp/opencode/db-backup.log`），不是生产量级快照：

```bash
docker exec ddp-postgres-1 sh -c \
  'PGPASSWORD=$POSTGRES_PASSWORD pg_dump -U ddp -d deepdocparse -Fc' \
  > /tmp/opencode/dev-deepdocparse.dump
# 校验可读性（本机 pg_restore 只认文件路径，所以先 cp 进容器）
docker exec -i ddp-postgres-1 sh -c \
  'cat > /tmp/dev-backup.dump; pg_restore -l /tmp/dev-backup.dump' \
  < /tmp/opencode/dev-deepdocparse.dump
```

实测：335 506 字节、`TOC Entries: 606`、`TABLE DATA` **80** 张表。
生产还应：先停写入、记录迁移版本（`alembic current` /
`control-migrate status`）、备份后跑 `pg_restore --list` 验读、再走
`docs/DEPLOY.md` 的「先迁移、看报告、再滚服务」。**生产快照上的演练
本机做不了**（没有生产库）。

## 6. 版本兼容性检查

- 候选包 ABI（`python.major_minor` / `cache_tag`）必须等于宿主机
  `python` 的 ABI；PKGBUILD 声明 `python>=3.14` 且 `<3.15`。
- `platform.machine` 必须等于宿主机；跨架构直接拒绝。
- 已装版本从 `<root>/RELEASE-MANIFEST.json` 读；相同版本拒绝、旧版本
  默认拒绝、升级才放行。
- 包内 `BUILD-MANIFEST.json` 与 manifest 的 `build_manifest_sha256`
  不一致即拒绝（防"清单和包不是一次构建"）。

## 7. 支持矩阵（本机验证范围）

| 维度 | 已实测 | 明确未验证 |
|---|---|---|
| 发行版 | Arch/CachyOS x86_64 | 其他发行版（glibc/mesa 差异） |
| 会话 | Wayland（niri，`wayland-1`） | X11（`DISPLAY=:1` 存在但冒烟走 Wayland）、GNOME/KDE 交互 |
| GPU | Radeon 780M（核显，Chromium 软件/VAAPI 路径） | NVIDIA/Intel 独显、容器内 GPU 直通 |
| 凭证 | `basic_text` → 仅会话内存 | GNOME Keyring/KWallet 持久化 |
| Python | 系统 3.14（`cpython-314`） | 3.13/3.15、非 x86_64 ABI |
| 更新 | 目录根上的真实 0.1.0⇄0.1.1 + 签名/篡改/降级负例 | 已安装 pacman 包的热更新、AUR 渠道 |
| 数据 | dev 库 `pg_dump` 命令可跑 | 生产快照迁移、RPO/RTO 演练 |

## 8. 正式发布还欠什么

1. **签名基础设施**：发布私钥进离线/HSM，`allowed_signers` 随产品分发；
   目前只用临时密钥在本地证明了验证路径。
2. **AUR/仓库发布**：`pkgrel`、`.SRCINFO`、维护者、校验源 URL；
   当前 PKGBUILD 是本地配方（`pkgname=deepdocparse-desktop-spike`）。
3. **其他平台**：X11/其他桌面会话、aarch64、macOS/Windows 均未构建。
4. **GPU 容量**：`CAPACITY-LOCAL-v3.md` 只有 CPU 数字；mineru/TEI/VQA
   的真实并发与容量要 GPU 机器。
5. **服务端生产迁移**：生产快照演练、回滚演练、旧账号表删除
   （`docs/refactor/STATUS.md` 已列）。
6. **更新分发**：目前需要手动把 `tar.gz + release.json(+sig)` 放到目标机；
   没有更新服务器/CDN，也没有"检查更新"的 UI（刻意不做：先保证通道可信）。

## 9. Windows 桌面（Tier A 远程 + Tier C WSL2 本地，v1）

> 本节补在 2026-09-14，记录 Windows A+C 切片**设计上已具备**的安装/更新/WSL 流程。
> **本机是 Linux，Windows 真机与真实 WSL2 一次都没跑过**；逐条验证范围（含哪些
> 只是垫片测试）见 `docs/refactor/WINDOWS-AC-VALIDATION-v1.md`，实施计划见
> `WINDOWS-AC-PLAN-v1.md`。中心侧部署不变，仍是 Linux（见本文件前面各节）。
> 注：§8.3 写于本切片之前（"macOS/Windows 均未构建"）—— Windows 现在有了
> Linux 侧构建产物，但真机未验证；macOS 状态不变。

### 9.1 支持范围与要求

- **推荐 Windows 11 24H2+**；Windows 10 22H2 为 best-effort（不承诺全功能）。
- **Tier A（连中心）**：只装 exe，走 HTTPS 连现有中心；不需要 WSL、不需要 Python。
- **Tier C（本地模式）**：**要求 WSL2**。WSL1 会被明确拒绝（`wsl1_unsupported`），
  没有可用发行版时本地模式「不可用 + 引导」，远端照常工作。
- **原生 Windows 本地运行时明确不做**：本地数据面（工作区/SQLite/blob/模型）
  全在 WSL2 的 ext4 上，宿主不跑 Python。
- ⬜ ARM64 未验证（`update_check.py` 里有 `arm64→aarch64` 的映射与拒绝分支，但没有产物）。

### 9.2 安装（NSIS per-user 与便携 exe）

- 产物（`packaging/windows/electron-builder.yml` 的 `artifactName`）：
  `DeepDocParse-<version>-win-x64-setup.exe`（NSIS，`oneClick:false`、`perMachine:false`，
  即按用户安装、可改目录；`allowElevation:true` 只在需要时提权）与
  `DeepDocParse-<version>-win-x64-portable.exe`
  （`requestExecutionLevel:user`）。
- **v1 故意不签名**（计划决策 6）：`signAndEditExecutable:false`。因此首次运行
  **Windows SmartScreen 会警告**，需要用户点「更多信息 → 仍要运行」；
  Authenticode 与发布签名留后续（§8.1）。
- 卸载默认保留应用数据（`deleteAppDataOnUninstall` 未开）。
- ⬜ 安装向导、升级安装、卸载流在本切片没有实机跑过；NSIS 安装器只能在
  `windows-latest` 上产出（Linux 无 wine 只得卸载器 stub，见 §9.3 / W4b 记录）。

### 9.3 CI 流水线（两个 job，为什么）

`.github/workflows/desktop-windows.yml` 把构建拆成两段：

1. `wsl-runtime`（`ubuntu-latest`）：跑 W3/W4 发行守卫，构建自包含 Linux 运行时
   tarball，`--verify` 后作为 artifact 传给下一段。
   **必须在 Linux 上**：`scripts/build_wsl_runtime.py` 在非 Linux 上直接拒绝
   （只验证过 Linux x86_64），且要执行随包捆绑的 Linux 解释器做 ABI 探针；
   根守卫 `tests/test_desktop_release.py` / `tests/test_wsl_runtime_build.py`
   也是 POSIX-only。
2. `package-windows`（`windows-latest`，`needs` 上者）：按 pin 校验 Electron zip、
   `build_desktop.py --platform win32-x64` 组装目录包、electron-builder 出
   NSIS + 便携 exe、`verify_windows_package.py` 校验、记录 SHA256，最后跑
   Windows 版 smoke（首轮 `continue-on-error`，仅诊断）与 WSL spike（诊断，
   决策 5：不装发行版、不碰 reboot/admin）。

**这条 workflow 已经在 windows-latest 上真跑**（2026-09-14 起，连续五轮各修掉一个
跨平台缺陷，见 `WINDOWS-AC-VALIDATION-v1.md` §9）。在它出现绿色运行之前，
不要拿它当"CI 已绿"的证据。

### 9.4 固定调用与产物

```bash
python scripts/build_desktop.py --platform win32-x64
npx --yes electron-builder@26.15.3 \
  --config packaging/windows/electron-builder.yml --projectDir . --publish never \
  --win nsis portable --x64
.venv/bin/python scripts/verify_windows_package.py dist/desktop/windows/win-unpacked \
  --installer dist/desktop/windows/DeepDocParse-*-setup.exe \
  --installer dist/desktop/windows/DeepDocParse-*-portable.exe
```

- Electron 版本与 zip 由 `packaging/windows/electron-lock.json` 钉死；
  electron-builder **固定 26.15.3**（版本宏解析到 `apps/desktop` 的 `0.1.0`）。
- 发布校验：`win-unpacked` 里捆绑的 WSL 运行时必须与 `dist/wsl/wsl-runtime.json`
  逐字节一致；两个 exe 必须带真负载（安装器 < 100 MiB 视为卸载器 stub，拒绝）。
- CI 附带 `SHA256SUMS` 与每个 exe 的 `.sha256` sidecar。

### 9.5 Windows 版 smoke

```bash
npm run smoke:windows --workspace @ddp/desktop       # = node scripts/smoke-windows.mjs
node apps/desktop/scripts/smoke-windows.mjs <win-unpacked 或 .exe> \
  --timeout 120000 --report apps/desktop/artifacts/smoke-report.json
```

它启动打包版（或 `--host` 用仓库里的 Electron），断言 renderer 拿不到
`require`/`process`、sandbox/contextIsolation 开启、hostStatus/startLocal 可用、
ready→suspend→resume→stop 与共享客户端的 PDF 渲染/导出结果。

> ~~打包门与 `main.mjs` 的 `--smoke` 文案不一致~~ **已解决**（镜像验证记录
> §8.2）：`main.mjs` 早已接受 `DDP_DESKTOP_SMOKE==='1' &&
> (!app.isPackaged || process.argv.includes('--smoke'))`，本轮已同步
> `smoke-windows.mjs` 的头部/失败文案与 workflow 注释。

**首轮 CI 里它仍然是诊断**：runner 会话未必能开 Electron 窗口；产物与失败原因
照常上传，从第二轮起按阻塞项处理。

### 9.6 更新与回滚（Windows 分支）

更新通道与 Linux 共用 `scripts/update_check.py`（stdlib-only）与同一份
`deepdocparse-desktop` release manifest，Windows 分支的差异：

- **归档是 zip**（Linux 是 tar.gz）；解包用安全的 zip 恢复（禁链接/路径穿越）。
- manifest 的 `platform.system` 必须是 `windows`；`platform.machine` 归一化后
  是 `amd64`，而 manifest 的 `python` 块描述的是**捆绑 WSL 运行时**的 Linux ABI，
  经 `WINDOWS_TO_LINUX`（`amd64→x86_64`、`arm64→aarch64`）换算比对；
  readiness 二进制是 `deepdocparse.exe`。**跨系统 manifest 直接拒绝**
  （`built for windows, host is linux` 之类）。
- **签名**：`sign`/`verify` 用 detached `ssh-ed25519` 签名，namespace
  `ddp-desktop-release`，签名覆盖去掉 `signature` 字段后的规范化 JSON；
  未签名 manifest 必须显式 `--allow-unsigned`，降级默认拒绝
  （`--allow-downgrade` 才放行）。
- **语义与 Linux 相同**：`verify`（签名→形状→大小/SHA→包内 BUILD-MANIFEST→
  版本单调性→许可→ABI）→ `apply`（staging 整树重算 → 旧树 `rename` 为
  `<root>.previous` → 新树就位）→ `rollback`（保留 `<root>.rolledback-<ver>`）。
  中断时 `status` 报 `interrupted:true`，回滚不需要网络。
- ⬜ Windows 上的 `apply`/`rollback` 实机没跑过：只有 Linux 上的真实
  0.1.0⇄0.1.1 演练 + 跨平台单测（`tests/test_desktop_release.py`）。

### 9.7 本地数据放哪

- **宿主（Windows）**：Electron `userData` 下的 `host/`（凭证、runtime session
  记录等），Windows 上即 `%APPDATA%\DeepDocParse\host\`；隐私依赖用户 profile 的
  NTFS ACL，`hostStatus` 会如实报 `ntfs_acl` 而不是假装有 mode。
- **WSL2 内部**：运行时在 `~/.deepdocparse/runtime`，默认工作区在
  `~/.deepdocparse/workspaces/default`；不上 `/mnt/c`（避开 WAL 与权限问题）。
- 导入/导出走字节流 HTTP，WSL 运行时**不读 Windows 路径**；token 不落到
  Windows 盘上（`serve --token-file -`，见 `python/ddp_local/docs/local-api.md`）。

### 9.8 WSL 设置/修复流程（detect → provision → start）

1. **检测**：`wsl.exe -l -v`（UTF-16LE 解码）→ 有 WSL2 且能选中发行版才算可用；
   缺失/列表失败/WSL1/发行版不存在分别给出 `wsl_missing` / `wsl_unavailable` /
   `wsl1_unsupported` / `wsl_distro_not_found`，本地模式不可用但宿主不崩。
   设置 `DDP_WSL_DISTRO` 可指定发行版；v1 **不自动安装发行版**（用户先
   `wsl --install`）。
2. **provision**：按 `runtime/wsl-runtime.json` 校验随包 tarball 的 size/SHA256，
   解压到 `~/.deepdocparse/runtime`（去掉顶层目录），再用捆绑解释器跑 ABI 探针；
   对不上就整体回滚、不置 ready。重复启动不重装。
3. **start/stop**：`runtime/python/bin/python3 -S -P app/src/runtime-launcher.py
   --workspace <ws> serve --port 0 --token-file -`，从 stdout 一行取
   `{url, token, pid}`；停止按**内部 pid** 走
   `wsl.exe -d <distro> -- kill -TERM <pid>`（超时再 KILL）。
   **绝不 `wsl --terminate` 整个发行版**。启动前会做孤儿清理：只杀 session
   记录过、且 argv 带我们 launcher 路径的 pid。
