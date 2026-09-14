# P2 本地 CPU 运行时整合验证

后续模型安装、真实 CPU 生成与 OOM 验证见 [P3-LOCAL-MODEL-VALIDATION-v3.md](P3-LOCAL-MODEL-VALIDATION-v3.md)。下文测试数字和未完成项保留为该轮历史记录，不能替代后续模型报告或全阶段验收。

2026-09-12。范围为现有本地 runtime 与服务器共享编译的整合；这不是 P2 全阶段完成声明，也不是独立提交验收。

## 已整合

- `ddp_core.application` 提供基础设施端口、共享 CPU PDF 解析、DDP-Layout、编译、检索候选门与带出处生成流程。gateway 保留原导入路径的薄再导出；corpus 编译器只保留存储裁图和视觉服务编排，调用同一 `compile_layout`。
- PDFium 解析与裁图共用进程锁。CPU 解析修复 CropBox 偏移和页面可见区域外的文本坐标；四种页面旋转均验证了 bbox 与真实渲染图。
- `ddp_local` 使用 SQLite/FTS5、文件快照、受控 CPU 子进程、持久任务与会话鉴权 HTTP。已纳入 Python workspace、`scripts/check.sh` 与 CI 包矩阵；README 包含可执行安装、CLI、HTTP 与 Bundle 命令。

## 本轮结果

环境：Linux x86_64 / CachyOS，Python 3.14.7，SQLite 3.53.4，pypdfium2 5.13.0，httpx 0.28.1，FastAPI 0.141.1，uvicorn 0.52.4。没有运行 GPU 或模型下载。

| 验证 | 结果 | 证据 |
|---|---|---|
| 本地 runtime 测试 | 23 passed | `/tmp/ddp-local-integration-runtime.log` |
| core 全量（含 4 种 CropBox 旋转、parse/render 并发子进程） | 59 passed | `/tmp/ddp-local-integration-core.log` |
| gateway 原契约 | 149 passed，6 个现有 GPU 条件 skip | `/tmp/ddp-local-integration-gateway.log` |
| corpus 编译回归 | 23 passed | `/tmp/ddp-local-integration-compile.log` |
| sample.pdf 真实 CLI/SQLite/回环 HTTP/Bundle | passed；本地与服务器适配器 1 个出处逐项一致 | `/tmp/ddp-local-integration-smoke.log` |
| 24 页 code-corpus.pdf 同一链路 | passed；192 个出处逐项一致，可检索 HttpRequestParser | `/tmp/ddp-local-integration-code-smoke.log` |
| 块类型、枚举、数据所有权守卫 | passed | 本轮执行输出 |
| 本轮文件 ruff F/B | passed | 本轮执行输出 |
| ddp-local wheel 构建 | passed | `/tmp/ddp-local-integration-wheel.log` |

本地测试同时覆盖真实中文 PDF 检索、输入变化拒绝、跨工作区身份、恶意 Bundle、HTTP Host/Origin/Bearer、显式缺模型/OOM、取消、lease 与重启 fencing、生成不确定状态不自动重发。生成协议测试使用模拟 Provider，不能计为真实本地模型运行。

可复跑命令（仓库根）：

```bash
.venv/bin/python scripts/smoke_local.py --compare-server
.venv/bin/python scripts/smoke_local.py --compare-server --pdf tests/fixtures/code-corpus.pdf --query HttpRequestParser
```

`--compare-server` 真实调用 gateway PDF 解析和 corpus 编译/裁图适配器，对比冻结版面、provider、seq/文本/类型/页码/bbox；它不连接中心数据库，不代替中心用户 HTTP 的导入/鉴权/索引验收。

## 不能据此勾选的出口

- **T16 生成部分 / T51：未通过。** 未安装并运行真实本地指令模型。两个 smoke 均输出 `generation=not_run_model_unavailable`，不存在 mock 转为成功的路径。
- 模型/引擎安装清单、摘要与兼容校验、按需下载、OOM 恢复和 GPU 独立环境仍需实现或实机验收；当前 Provider 只接入用户显式配置的已运行服务。
- 当前 Wiki 是结构化出处绑定的生成草稿，语义状态为 `needs_review`；完整本地 Wiki 修订 CAS、人工编辑合并、关系检验与发布工作流尚未验收。
- 本次证实公共模块导入没有中心数据库、网络或模型副作用，CPU 路径在无模型条件下真实运行；尚无操作系统层完整离线网络审计，不能宣称全产品零外发已验收。
- 单个 Python wheel 能构建，不等于 Electron 安装包、升级/回滚、干净机器安装或 Linux 安全凭证后端验收通过。
