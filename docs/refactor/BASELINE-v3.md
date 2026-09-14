# v3 源码基线与迁移决策

本文件记录本轮开始时的源码事实。执行状态和新验证见 `IMPLEMENTATION-v3.md`。
执行依据是工作区 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md`；沿用旧工程约束，不沿用与 v3 冲突的共享语料授权语义。

## 冻结范围

- 源码 HEAD：`c09783d3555fae2ce86f229f18f37418b036f276`。
- 开始分支：`deploy/autodl-cloudflare-tunnel`，并非 main；工作树已有未提交的 v3 schema、枚举和 0015 草案。
- 工作分支：`codex/desktop-federation-v3`。已有变更保留并继续修正。
- 工作区根不是本次提交的仓库；实际系统已合并在 `DeepDocParse/` 单仓库。旧双仓库目录说明仅作历史参考。
- 本机支持真实 CPU 解析与多进程协议实验；缺少 NVIDIA GPU。缺 GPU 不等于不能部署六个逻辑节点，容量和真实模型质量分别实测。

## 目录校准

| 职责 | 既有实现 | v3 处理 |
|---|---|---|
| Web | `apps/web`，Vue 3 | 保留技术栈、复用 REV.04 视觉 |
| 契约 | `packages/contracts`，Go/Python/TS 生成器 | 在同一来源新增资源、证据和联邦 schema |
| 业务纯逻辑 | `python/ddp_core` | 提取应用端口，服务器和本地共用 |
| 控制面 | `services/control-api`，Go | 用户、组织、计量、入口、文件凭证 |
| 语料面 | `services/corpus-api`，Python | 资源授权、编译、检索、知识及永久产物 |
| 持久执行 | `services/corpus-worker` | 复用已有任务、租约和 Outbox |
| 模型适配 | `services/model-gateway` | 注册表驱动，模型环境独立 |
| MCP | `services/mcp` | 原直接访问全库方式必须迁到语料授权 API |
| 数据迁移 | `database/control` 与 `database/corpus` | 独立写入所有者；扩展后切换 |
| 桌面/本地/连接 | 基线没有可用实现 | P2/P3 新增，不能以 Web 包壳代替本地运行 |
| 可枚举联邦 | 基线没有可用实现 | P4–P6 必须实现并做独立节点实验 |
| Arch 发行 | 基线没有安装包 | P7 完成普通用户安装、恢复与安全测试 |

## C-01：逻辑资产与去重

0006 曾将相同内容合并到一个 Document，并保留 `document_uploads`。v3 要求资产、权限和删除独立，因此在内容层上新增 Resource、ResourceVersion、UploadEvent；Document 只保留共享字节与缓存身份，不再代表授权主体。

相同字节仍可去重存储。未完成解析必须按资源分离任务和网关缓存身份，不能复用另一资产可撤销的文件凭证。缓存复用必须证明授权与产物身份一致，不能以“只解析一次”为理由合并任务主体。

## C-02：历史信息缺口

`document_uploads` 只有文档、用户和时间，无法恢复追加上传者当时的组织或文件名。0015 使用完整 `(document_id,user_id)` 的确定性 SHA-256 标识；首上传者沿用已有可信字段，其他记录保留在 `migration:unresolved` 组织，名称为 `Recovered document.pdf`。这些记录保留存储引用，待权威映射补齐后恢复，不能猜成首上传者的组织或泄露其名称。运维可先运行 `scripts/resolve_resource_migration.py manifest.json` 校验经控制面上传/成员记录核实的映射，确认后以 `--apply` 原子恢复；脚本不自行猜组织，也不跨域读控制面数据库。

0018 只为唯一、原始、同组织且时序成立的资产回填历史会话、解析、抽取上下文。歧义记录不借后来发布的同内容资源恢复访问。0017 不从可变的 Document.current_job_id 伪造历史固定版本。

## C-03：版本与知识

既有 ParseJob.document_version、Evidence 和 Citation 可继续作为出处基础；固定 ResourceVersion.parse_job_id 负责版本绑定。重新选择解析产物新增版本，旧版本不覆盖。

基线 WikiEntry/WikiSentence 没有完整修订工作流，需要新增 WikiRevision、DependencyManifest、ClaimEvidenceBinding、Compare-and-swap 更新、人工编辑保留与 stale 检查。旧知识按全局实体名合并会覆盖其他作者，必须补作用域隔离。

## C-04：任务和能力

基线任务枚举中的值不等于 handler 或部署能力已实现；联邦接单只能承诺实际可运行的能力。执行、路由、范围覆盖、交付、清理状态分开记录；复用现有持久队列和租约，避免另造重复真相。

## 验证口径

早期草案的“全部绿”“真库已验”和硬件阻塞结论不作为本轮验收证据。本轮以实际命令日志、临时 PostgreSQL、CPU 运行、模型记录、渲染结果和独立验收报告为准。mock 测试不能证明真实模型能力，单节点测试不能证明完整联邦范围。
