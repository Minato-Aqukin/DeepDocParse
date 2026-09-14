# P2/P3 本地 Wiki 修订与模型回执验证

2026-09-13。范围为本地 Python runtime、共享纯函数、HTTP/CLI；不代表 Electron UI、中心/联邦 Wiki 或全 P3/P5 完成。当前已经真实验证 **两页、六条有原始出处的句子、两条模型选择的原文关系句，以及人工编辑 CAS 与重启读取**。关系能力明确限定为 `model_selected_source_statement/1`。

## 本地工作流与固定接口

SQLite 从 user_version=1 事务迁移至 2，保存 Wiki 资产、不可变 Revision、页面/关系、原文依赖、人工编辑与逐次模型调用。沿用现有 tasks/outputs/receipts，不增加第二套任务账本。Wiki 修订与 task.succeeded 在同一事务提交；失效基准、无效出处或取消不会留下孤立修订。

| 操作 | HTTP |
|---|---|
| 创建 | `POST /api/v1/wikis` |
| 列表、当前详情 | `GET /api/v1/wikis`、`GET /api/v1/wikis/{id}` |
| 修订列表、固定详情 | `GET /api/v1/wikis/{id}/revisions`、`GET /api/v1/wikis/{id}/revisions/{rev}` |
| 基准修订重建 | `POST /api/v1/wikis/{id}/revisions` |
| 人工段落编辑 | `PATCH /api/v1/wikis/{id}/pages/{page_key}` |
| 调用记录元信息 | `GET /api/v1/tasks/{task_id}/wiki-attempts` |

所有新写操作都必须传 `Idempotency-Key`。请求与返回字段见 [local-api.md](../../python/ddp_local/docs/local-api.md)。旧 `POST /api/v1/wiki` 保持既有请求/回答字段，同时把成功的有引文草稿登记为 Wiki/Revision，新增 wiki_id/revision_id；旧英文无引文句仍然拒绝，不借新流程放行。

列表默认 50、最大 100 条，只返回 Wiki/当前 Revision 摘要，不累计整页正文。返回 `visible_total`（游标锚点窗口内当前工作区可见资产数）、`has_more`、`next_cursor`；游标绑定工作区与列表种类，锚点排除分页开始后新建的资产。当前修订摘要可以随编辑改变，不把跨页结果描述为全库同时刻快照。历史修订本身不可变。完整 revision 最大 2 MiB，完整持久输出最大 4 MiB；超预算明确失败，不能存完后再静默截断。

生成时冻结实际输入版本、原文 Evidence、内容摘要与 parse revision，模型返回后再次检查。页面与关系只能引用已经取得的原始 Evidence，生成 Wiki 不进入原始 FTS 索引。DependencyManifest 保守保留所有送给生成器的原文上下文；SQLite FK 阻止删除仍被历史 Wiki 引用的 evidence/version，重建这些 evidence 同样返回 source_in_use。本地没有资源删除、Wiki 删除或发布接口，不据此宣称完整 GC/发布工作流已经实现。

人工段落使用 kind=human、source_type=generated、unsupported=true、evidence_ids=[]、review_state=unreviewed 和本地创建者信息；不能自己添加原始证据标签。编辑生成新修订，比较 current_revision_id；重建保留同页面的人工段落，规划中不再出现的人工页会被保留并报告合并冲突，超过页面预算则明确失败。历史原文绑定变化会在读取时标 stale，旧修订不被重写；本地暂未提供同一资源追加新版本/撤销的公开 API，因此不把 SQL 负例测试当成完整 T53 用户流程。

## 真实模型记录

使用此前完整校验的 Qwen3-1.7B Q8_0 与 llama.cpp b10809 CPU，8 线程、0 GPU 层、8192 context。摘要与运行环境见 [模型安装报告](P3-LOCAL-MODEL-VALIDATION-v3.md)。进入无特权 user/network namespace 后只启用 loopback，无外部路由，无下载或远端 fallback。

最终协议 `ddp-wiki-generation/5`，解码器 `wiki-json/2-relations-array-envelope`。模型先规划页面，再写带原始引用的页面，最后从原始证据的候选关系句中选择编号；三个调用总 completion 预算为 4096（1024 + 2048 + 1024）。候选句来自同一句中实际出现的两页原文实体词，保留原始文字与引用；方向只表达实体在原句中的顺序（source_mention_order），不自动推断因果/本体关系。模型不能自造谓词、端点或引用。原句匹配和结构绑定不等于通用语义验证，所有产物仍 needs_review。

最终真实构建耗时 **26.46 秒**，不是吞吐/SLA：

- 两页：Aurora Pipeline: Collector / Aurora Pipeline: Validator。
- 六条生成句子均与对应原文相符，并有 source_version/digest/bbox。
- 模型选择的两条关系：`Collector forwards each batch to Validator for schema checks.` 与 `Validator receives batches from Collector.`，分别回到 PDF 第 1 页与第 2 页。
- 人工编辑创建新修订；旧基准编辑返回 revision_conflict。关闭 runtime、重新打开同一工作区后，原修订、人工段落、当前修订指针与成功任务回执均仍存在。

输入：[实际 PDF](artifacts/aurora-wiki-source.pdf)。输出：[最终原始运行、三次真实模型输入/输出和重启结果](artifacts/local-wiki-real-v5-final.json)。每个测试输出都来自实际模型，没有将协议 fixture 或缓存重放冒充真实推理。

## 失败历史与改动依据

共保留六轮明确的协议/解码实验，合计 15 次实际模型调用；没有悄悄重跑同一配置直到成功。

| 轮次 | 实际问题 | 处理与结果 |
|---|---|---|
| v1 | 写作回显内部 page_key 规划对象 | 拒绝；v2 改为只传编号页面 |
| v2 | 谓词回显占位词 relationship | 早期评测器误标 passed，人工复核撤销关系通过结论；原报告保留 |
| v3 | 两页正确但关系数组为空 | 两页草稿可保存，T51 关系验证失败 |
| v4 | 关系输出有错误引用、方向与不连接两页的谓词 | 拒绝；限定为模型选择已有原文关系句 |
| v5 | 模型正确选择 [1,2]，但返回裸 JSON 数组 | object-only 解码器拒绝；只补等价关系数组外壳解析 |
| v5-final | 同一 v5 生成 profile，加数组外壳适配 | 完整两页/关系/编辑/重启验证通过 |

所有原始 JSON 保留在 artifacts/local-wiki-real-*.json；[复核结论清单](artifacts/local-wiki-adjudication-v3.json) 明确区分原脚本状态与最终验收判断。仅关系阶段允许裸整数数组与 selected_relations 对象等价；未知编号、对象数组、虚构引用仍拒绝。规划/页面阶段仍要求对象，未放松其规则。

## 持久模型命令与验证

`POST /api/v1/models/{id}/install`、`start` 与 `/api/v1/models/stop` 使用同一稳定 key/真实任务回执，结果增加 task_id。下载进度仍进入持久事件；取消保留真实 partial 状态，只有新的显式操作键才续传。回复丢失后同键重放不下载、不重启。回执表示历史命令结果，实时运行状态必须读取 /models；例如过去 start 成功，runtime 重启后仍如实显示 stopped。崩溃过期的 model_* 任务标 execution_interrupted，不自动重发。受控模型的随机端口/别名不参与稳定意图摘要，用户显式改模型或外部端点仍改变意图。

本地完整测试 **56 passed**，共享 core **71 passed**，ruff F/B 通过。覆盖两真实 SQLite 连接的 CAS、模型运行中人工编辑胜出、冻结原文变更零发布、跨工作区/跨 Wiki ID、不可变历史/原文删除约束、迁移回滚、分页锚点/游标边界、安装取消续传与启动回执重启对账。安装传输与许多异常输出测试使用协议 fixture；真实模型能力只以上述实际模型报告计数。

运行：

```bash
.venv/bin/python -m pytest python/ddp_local/tests -q
.venv/bin/python -m pytest python/ddp_core/tests -q
unshare --user --map-root-user --net .venv/bin/python python/ddp_local/scripts/eval_wiki.py \
  --workspace /tmp/ddp-real-model-workspace --output /tmp/new-wiki-evaluation.json
```

两个 Python 测试包要分别执行，或显式使用对应 pytest 配置；混在一次根目录 pytest 会跳过 local 的 asyncio_mode=auto 配置，不能据此诊断成运行时故障。完整 Electron 点击路径、中心/联邦 Wiki、多源更新授权/发布、删除/GC、自由语义关系质量及 GPU profile 不由本报告验收。
