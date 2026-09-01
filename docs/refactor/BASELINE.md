# 重构基线（旧系统冻结记录）

> 依据：`MERGE-REFACTOR-PROPOSAL.md` §11.1
> 记录时间：2026-09-01
> 这份文件是**对拍与对账的比较基准**。切换完成前不得修改；有修正另起一节追加。

## 1. 源仓库与提交

| 仓库 | 冻结提交 | 备注 |
|---|---|---|
| `Minato-Aqukin/DeepDocParse`（service） | `b1b04b23a8ce17c574b5318ff946cd98c803c385` | 合仓前最后一次提交，含 PDFium 串行化修复 |
| `Minato-Aqukin/DeepDocParse-Web`（web） | `95f3855f04009a40ff399376e8fd31837ea11620` | |

两段历史已用 `git filter-repo --to-subdirectory-filter` 加前缀后合入本仓库，
再由重组提交移进目标布局。**抽查已做**：各随机抽 5 个旧 commit，
比较其 tree 与 monorepo 中同名 commit 的子树 —— 10/10 逐字节一致。
author / author-date / message 全部保留（16 组 author+date 组合完整存在）。

## 2. 契约摘要（sha256）

```
436c1987bfcd83cd7ec8564973e6a66c735ee1d484c9b43a5dd96d1e5544fe9f  openapi.yaml            (v1.1)
1e449f4b64021a1cdafb5fcaf845624aa99fa196f881a8c0516b8368c014c046  docs/layout-format.md   (DDP-Layout v1.1)
1fff6fc8c375c4664a4aa760bebfd08b1a9e63e6ed021e333868dc741cb073ae  docs/extract-format.md  (DDP-Extract v1)
180f7d2a7f374ad5358199be9ff76e94f7b14c09ca187f00b4ab452fd5022ba3  docs/compile-format.md
85da0e27da11623be7697ab46486d7c9a61583ecabc7f5cbfe0d742af4d6213b  docs/agent-format.md
76c0ab3f5a1562e140ffdba1f05bf147c4d8e46dafed5a8a12f7d0ae0b5aefdf  docs/graph-format.md
360cf4411f83e9a0d2f412b94e179786022a4c0741de85db63e2145bbf53e3bd  docs/mcp-tools.md
a891399613db0debe1fc641c6d88ad682c2a332372be4cd587d757936ae167f0  docs/mineru-api-contract.md
3d7d21d37d2f46099e7b3283e840d4b4d5da35a412a38d80130b509402be622b  models.yaml
b5e4ab22219992d7f0ec6b3cbd49be3fe2233a6e9e9c4af49610123eff6ca9a1  models.cpu.yaml
26dff8179cee8b329ae9b7e5437d6ba5242721cde07fdfa56dd30ec9c9f27481  models.dev-host.yaml
98e0247220fe958502ceba766f5915cb44e62c777fe9926c39f38d3c5d6a321d  models.autodl.yaml
f95d06201425d8290bbed910af1833e799025f85c48d3d3f604cd77fd4571ddb  models.local.yaml
```

对外 API 面（必须继续兼容，见 §12.2 对拍）：

- service `/v1/*`：`parse` · `parse/{id}` · `parse/{id}/result` · `chat/completions` ·
  `models` · `embeddings` · `extract` · `extract/{id}` · `extract/{id}/result` · `rerank`
- web `/api/*`：52 条路由（清单见 `docs/refactor/INVENTORY.md` §3）
- web `/v1/*` 对外代理 + `/mcp` 反代
- MCP 五工具：`search` · `ask` · `get_evidence` · `read_wiki` · `graph_neighbors`
  （外加 deprecated 的 `ask_document`）

## 3. 数据库

- Alembic head：`0012_knowledge_layer`（12 个 revision，线性）
- 扩展：`vector` 0.8.6（pgvector）
- 表：见 `python/ddp_core/ddp_core/models.py`（语料 16 张）+ 旧 web 层 7 张
  （`users` `api_keys` `conversations` `messages` `extraction_templates`
  `extraction_runs` `extraction_items` `file_tokens` `usage_records`）

## 4. 测试基线

> §12.1：测试数量只许因**明确合并重复测试**而下降，且必须有等价覆盖证明。

| 套件 | 数量 | 命令 |
|---|---:|---|
| service 契约测试 | **208 passed / 6 skipped** | `cd gateway && pytest -q` |
| web backend | **285 passed** | `cd backend && pytest -q` |
| 前端组件单测 | 26 | `npm run test:unit` |
| 前端 e2e（Playwright，对 `dist/` 跑） | 69 | `npm run test:e2e` |
| **合计** | **588 passed / 6 skipped** | |

那 6 个 skip 是 `models.cpu.yaml` 没有 vqa 段导致的**显式 skip**，不是恒真的绿。

守卫脚本（全部必须继续存在且有效）：

- `scripts/check_contract.py` —— openapi.yaml ←→ 网关实际端点
- `scripts/check_blocktype_parity.py` —— 两侧块类型归一化判据一致
- `scripts/check_chunk_regression.py` —— 分块规则回归
- `scripts/gen_config_docs.py --check` ×2 —— 配置文档与 config.py 同步

## 5. 对象存储清单

**未采集。** 本机 docker 未运行，MinIO 里没有可用数据；旧生产快照也不在本机。

> ⛔ 这是一个**未关闭的前置条件**：`§11.4` 要求迁移器至少做三次全量演练
> （空库 / 生产快照 / 对抗数据集），其中生产快照那一轮需要真实的
> PostgreSQL dump 与 MinIO 清单。切换窗口开始前必须补齐：
>
> ```bash
> pg_dump  --format=custom deepdocparse > baseline/pg.dump
> mc ls --recursive --json localmin/deepdocparse > baseline/minio.jsonl
> ```

## 6. 运行环境基线

- 本机：Arch(CachyOS) · Ryzen 7 255 · **核显 780M，无 N 卡** · 22G RAM + 22G swap
- Python 3.14.7（系统）· Node 26.7 · Go **1.27.0**（本轮装到 `~/.local/opt/go`）
- 端口：9000 gateway · 9100 mcp · 6379/16379 redis · 18000 mineru · 18001 VQA ·
  18080 TEI · 18081 fixtures · 8080 web · 5173 前端 · 15432 PG · 19000/19001 MinIO
- **本机跑不了的**：mineru pipeline（CUDA）、VQA 平面、TEI 加速 —— 需要 GPU 机器

## 7. 已知的旧系统缺陷（不得在新系统中复现）

从 `CLAUDE.md` / `plan.md` 的历次验收里提炼，新系统必须有对应守卫：

1. 顶层包名都叫 `app`，从错误 cwd 启动会**静默**导入错包 → 新系统三个 Python 服务包名互不相同
2. 关键词检索路是 OR 语义，改成 `websearch_to_tsquery` 会让中文关键词路静默变死
3. 视觉核对必须按 `vision` 能力词筛模型；漏筛则每条好出处都被判 `parse_mismatch`
4. 抽取平面不能复用 OCR 专用模型（`no_instruct` 能力词），否则能力缺失伪装成 `not_found`
5. 两个 vLLM 共卡必须钉"先后"且等 `/health`，否则后分配者必算出负 KV
6. `EXTRACT_MISMATCH_THRESHOLD` / `QA_PARSE_MISMATCH_THRESHOLD` = 0.55
   （标定样本是 born-digital 英文单栏，**仍需用含中文/代码/公式的样本重标定**）
7. 向量检索静默退回 BM25 —— 只能靠 `metrics:retrieval:vector` 计数验证
8. 换 tokenizer 会静默毁掉关键词路（索引时与查询时的 backend 必须一致）
9. PDFium 不是线程安全的，并发渲染直接段错误杀掉 worker 进程
