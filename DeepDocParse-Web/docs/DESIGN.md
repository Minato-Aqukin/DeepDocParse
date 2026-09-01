# DeepDocParse-Web 深化设计（M6）

> ⚠️ **本文写于 M6，其中的租户模型已被 `plan.md` §2 已定 2 推翻**（2026-08-26，阶段 1b）：
> 一次部署 = 一份语料 = 一个知识库，文档**不属于任何用户**，`user_id` 已改名
> `uploaded_by`（仅归属署名），去重变全局，全站唯一残留的授权是删除权限。
> 下面凡是提到"按 user 过滤""文档属于用户"的地方都以此为准；
> 已就地标注的三处见 §数据模型与 §检索。
>
> ⚠️ **出处已经搬家了**（阶段 2b–4，2026-08-27）：出处不再住在
> `messages.citations` / `extraction_items.fields[].citations` 这两处 JSON，
> 而是 `evidence` + `citations` 两张表（写：`app/evidence.py::record_evidence`，
> 读：`load_citations`）。**那一列和那个键已经删了**（迁移 0009，不可逆）。
> 下面凡是描述"出处存在 JSON 列里"的地方一律作废 —— 对外响应的形状没变，
> 是 `_fields_out` / `list_messages` 现拼出来的。
>
> ⚠️ **模块位置也变了**（阶段 2a）：本文提到的 `app/types.py` / `app/search.py` /
> `app/chunking.py` 连同 models / tokenize / rerank 已迁入 `DeepDocParse/gateway/ddp_core/`，
> 两个仓库共用同一份。**设计结论没变，只是文件换了地方** ——
> 下面的路径已就地改成 `ddp_core/*`，`app/` 下不再有它们的副本。


> 状态：**已实现并通过真机 e2e**（单测 67 例、前端类型检查、parse/qa 两段 e2e 全绿）。
> 本文档随实现同步维护；与代码不符时以代码为准并回来改这里。
> 上位文档：[../../ARCHITECTURE.md](../../ARCHITECTURE.md)（ADR #14–#16 是本文档的结论）
> 当前实现基线：M5（`3d41af0`）——后端 21 个端点、34 例单测，前端 5 个页面，真机 e2e 全绿。

## 0. 为什么要做这一轮

M5 证明了链路可用，但作为产品有两个结构性缺口：

1. **核心卖点在 Web 端缺席**。ARCHITECTURE.md §1 承诺三种形态（Web 产品 / API 产品 / Agent 生态），
   其中"带出处的文档问答"只有 API 与 MCP 能用。归档下来的 `layout.json`（页码 + bbox）
   目前**没有任何消费方**——前端原文栏是个 `<iframe>`，坐标信息全浪费了。
2. **`tasks` 一张表同时是"一份文件"和"一次解析"**。`UNIQUE(user_id, doc_id, origin)`
   堵死了换参数重解析；问答绑定、版本对比、跨文档搜索也都卡在这个模型上。

目标部署形态是**多副本生产**，因此每一部分都要回答"水平扩展下怎么正确"。

---

## 1. 三条总体判断

### 1.1 检索索引放 Web 层，service 保持无状态（ADR #14）

service 的向量索引是可重建缓存、TTL 24h（ADR #6），Web 的结果是永久的。
直接复用 `ask_document` 有三个硬伤：返回纯文本（出处要正则解析、做不了高亮）、
超 24h 触发重新解析、无法多轮。

→ Web 层自建持久索引（Postgres + pgvector），service 的结构一行不改。

**刻意接受的重复**：两层各存一份向量索引。service 那份服务 MCP/API 调用方（24h 缓存），
Web 这份服务 Web 用户（永久）。换来的是 service 无状态不被破坏。

### 1.2 数据模型拆 Document / ParseJob（ADR #15）

```
User ─┬─ Document (一份文件，内容 sha256 唯一)
      │     ├─ ParseJob*     一次解析（engine/options/status/result_prefix）
      │     ├─ Chunk*        来自 current_job，带 embedding
      │     ├─ FileToken*    稳定原件 URL
      │     └─ Conversation* ─ Message*
      ├─ ApiKey*
      └─ UsageRecord*
```

### 1.3 分块在本层做，service 一行不改（ADR #16 / #17）

**解耦优先**：契约保持冻结、不新增端点，本层自己实现结构感知分块。

输入不是向 service 现取，而是**本层已归档的 `layout.json`**（`results/{job_id}/layout.json`）。
这比"调 service 取 chunks"更解耦，也更健壮：不依赖 24h 暂存窗口，永久副本在手，
任何时候都能重建索引（换 embedding 模型、调分块参数都不用重新解析）。

代价是 mineru `middle_json` 的解析规则在本层也有一份。缓解措施：
- 只依赖契约承诺的字段（`pdf_info[].page_idx / page_size / para_blocks[].bbox / lines[].spans[].content`）
- `chunking.py` 对缺字段全部容错（缺 bbox 仍出块，只是不能裁剪；缺 page_size 记 None）
- 单测用真实 `layout.json` 样本固化格式（`backend/tests/fixtures/layout-*.json`）。
  **2026-08-18 更正**：这条曾经只是说法 —— `test_chunking.py` 全是 `_page()` 合成的样本，
  而合成样本永远长成我们以为的样子，检测不到上游格式漂移。真机产物与对应用例已补上，
  格式本身也升格成了显式契约（`../DeepDocParse/docs/layout-format.md`）

**上游可替换（ADR #17）**：embedding 与 chat 端点走独立配置项，缺省指向 service，
但可直连 TEI / vLLM / 任何 OpenAI 兼容服务。本层因此不绑定 DeepDocParse 的部署形态。

---

## 2. 数据模型与迁移

### 2.1 documents

| 列 | 类型 | 说明 |
|---|---|---|
| id | String(32) PK | |
| uploaded_by | FK users.id, idx | **1b 起：仅归属署名**，不是可见性边界。全部上传者见 `document_uploads` |
| doc_id | String(64), idx | 文件内容 sha256（同时作为契约 `doc_id` 传给 service） |
| origin | String(8) | `web` \| `external`，沿用 M5 语义 |
| filename / mime / size_bytes | | |
| object_key | String(512) | 空串 = 外部提交，文件不在本层 |
| page_count | Integer | 取自 current_job |
| current_job_id | String(32) FK parse_jobs.id, null | "当前生效"的解析版本 |
| index_status | String(16) | `none` \| `pending` \| `indexing` \| `ready` \| `failed` |
| index_error | Text null | 索引失败原因，**要在 UI 上可见** |
| deleted_at | timestamptz null | 软删除；对象由 GC 任务回收 |
| created_at / updated_at | | |

约束：~~`UNIQUE(user_id, doc_id, origin)`~~ → **1b 起 `UNIQUE(doc_id, origin)`**（全局去重：一次部署 = 一份语料）、~~`INDEX(user_id, deleted_at, created_at)`~~ → `INDEX(deleted_at, created_at)`。

### 2.2 parse_jobs

| 列 | 说明 |
|---|---|
| id / document_id(idx) / api_key_id(null) | 触发者：Web 为 None |
| engine / options(JSON) / options_hash(String(64)) | `sha256(engine + canonical_json(options))` |
| service_task_id(null, idx) / status / error / page_count | status 同 M5 五态（含 `archiving`） |
| result_prefix(null) | MinIO `results/{job_id}/` |
| archived_at / created_at / updated_at | |

约束：`UNIQUE(document_id, options_hash)` —— 同参数重解析幂等命中已有 job，换参数才建新行。

### 2.3 chunks

| 列 | 说明 |
|---|---|
| id / document_id(idx) / parse_job_id | 换 current_job 时整体替换 |
| seq | 文档内顺序 |
| page_idx / bbox(JSON) / page_size(JSON) | 出处三件套，由 `ddp_core/chunking.py` 从归档的 layout.json 算出 |
| text / char_len | char_len 用于 prompt 预算 |
| embedding | `Vector(1024)` null，见 §2.5 |

索引：
- `HNSW (embedding vector_cosine_ops)` —— 近邻检索
- `GIN (to_tsvector('simple', text))` —— 关键词/混合检索（中文用 `simple` 配置，不装分词插件）
- `BTREE (document_id, page_idx, seq)` —— 按页渲染取数

### 2.4 conversations / messages / 既有表改造

- `conversations`：id / user_id / document_id / title(200，取首个问题前 40 字) / created_at / updated_at
- `messages`：id / conversation_id(idx) / role(`user`\|`assistant`) / content /
  `verified`(Bool) / `degraded`(String(32) null) / created_at
  （**出处不在这张表上** —— 见顶部推翻说明，它住在 `evidence` / `citations`）
- `file_tokens.task_id` → `document_id`；新增 `scope`（`source` \| `share`）
- `usage_records.task_id` → `parse_job_id`；`kind` 增加 `qa` / `embed`

### 2.5 pgvector 与 SQLite 单测并存（必须先做对）

M5 的单测能在 SQLite in-memory 跑完是很值钱的资产（34 例，不用起 PG/MinIO），不能因为 pgvector 丢掉。

- `ddp_core/types.py::Vector`：`TypeDecorator`，`load_dialect_impl` 在 PG 上返回
  `pgvector.sqlalchemy.Vector(dim)`，其它方言退回 `JSON`（存 float 数组）。
  `Base.metadata.create_all` 在 SQLite 上照样能建表。
- 检索抽成协议 `ddp_core/search.py::SearchIndex`（`upsert(document_id, chunks)` /
  `search(document_id, vector, text, top_k)`），与 M5 的 `Storage` 协议同套路：
  `PgVectorIndex` 走 SQL，`MemoryIndex` 供单测注入（纯 Python 余弦 + 子串匹配）。
- compose 镜像换 `pgvector/pgvector:pg16`（当前是 `postgres:15-alpine`）。
- **维度写死 1024**（bge-m3），配置项 `embedding_dim`。换模型维度变化必须整体重建：
  service 侧用"索引名带维度"（`chunks_idx_d{dim}`）打补丁的做法不适合关系库，
  本层宁可提供一条 `reindex-all` 运维命令。

### 2.6 迁移 `0002_split_document_parsejob.py`

```
1. CREATE EXTENSION IF NOT EXISTS vector;
2. CREATE TABLE documents / parse_jobs / chunks / conversations / messages
3. INSERT INTO documents SELECT id, user_id, doc_id, origin, filename, mime, size_bytes,
       object_key, page_count, 'none', created_at, updated_at FROM tasks;
   -- document.id 沿用 task.id：外键回填与 MinIO 已有的 results/{task_id}/ 前缀都不用动
4. INSERT INTO parse_jobs SELECT <新 uuid>, id, api_key_id, engine, options,
       sha256(engine||canonical(options)), service_task_id, status, error, page_count,
       result_prefix, archived_at, created_at, updated_at FROM tasks;
5. UPDATE documents SET current_job_id = (SELECT id FROM parse_jobs WHERE document_id = documents.id);
6. ALTER file_tokens ADD document_id / scope; 回填; DROP COLUMN task_id;
7. ALTER usage_records ADD parse_job_id; 回填; DROP COLUMN task_id;
8. DROP TABLE tasks;
```

**迁移不搬 MinIO 对象**：搬对象是不可逆操作，不值得为命名一致性冒险。
老数据的 `result_prefix` 保持指向 `results/{原 task_id}/`（= 新的 `document_id`），
新建的 job 才用 `results/{job_id}/`。

**验收**：`alembic check` 无漂移、`downgrade` 能回 0001；迁移前后 documents 行数 == 原 tasks 行数，
且每个 document 都有 `current_job_id`；单测仍全部跑 SQLite。

---

## 3. 分块（`ddp_core/chunking.py`，阶段 2a 前是本层的 `app/chunking.py`）

**DeepDocParse 仓库零改动。** 输入是本层归档的 `results/{job_id}/layout.json`。

规则（与出处定位强相关，不可随意改）：
- 只在页内合并，chunk **永不跨页**——出处必须能落到唯一页码
- 相邻块合并到 `max_chars=800` 上限；bbox 取合并块的外接矩形
- 每个 chunk 带 `page_size`：裁剪时要用它换算坐标，缺它会在 CropBox 偏移/旋转页上裁错
- 空文本块跳过；缺 bbox 仍出块（只是不能裁剪）

只依赖契约承诺的版面字段：`pdf_info[].page_idx / page_size / para_blocks[].bbox / lines[].spans[].content`。
单测用真实 `layout.json` 样本固化格式，mineru 升级后这条测试先红。

---

## 4. 索引管线

### 4.1 状态机

```
archive_job 成功 -> index_status='pending' -> enqueue index_document(document_id, job_id)
index_document:  pending -> indexing -> ready
                                    \-> failed（index_error 落库，UI 可见，可手动 reindex）
```

手动入口：`POST /api/documents/{id}/reindex`。

### 4.2 index_document 流程

```
1. claim: UPDATE documents SET index_status='indexing'
          WHERE id=:id AND index_status IN ('pending','failed')
          -- rowcount==0 直接返回：多副本/重复投递天然幂等（沿用 M5 归档 claim 的套路）
2. 从 MinIO 读 results/{job_id}/layout.json -> `ddp_core/chunking.py` 分块
   -- 读的是本层永久副本，不碰 service，也不受 24h 窗口约束
3. 批量向量化：按 embedding_batch_size=16 切分，逐批 POST {embedding_url}
   -- TEI 的 max-client-batch-size 是 32，service 侧 worker 已因整批被拒踩过坑
4. 一个事务内：DELETE FROM chunks WHERE document_id=:id; 批量 INSERT
   -- 先删后插：重解析后残留的旧块会被检索命中（service 侧 save_chunks 同样的教训）
5. index_status='ready', index_error=NULL
6. usage_records += (kind='embed', requests=批次数)
```

**失败不自动重试**：任何一步异常 → `index_status='failed'` + 原因落库，不抛给 ARQ。
ARQ 重试会重跑全量 embedding，成本高且大概率同样失败；改由用户/运维显式 reindex。

### 4.3 混合检索 + RRF

```sql
-- 向量路
SELECT id, page_idx, bbox, text, embedding <=> :qvec AS dist
FROM chunks WHERE document_id = :doc ORDER BY dist LIMIT 8;
-- 关键词路
SELECT id, ts_rank_cd(to_tsvector('simple', text), q) AS rank
FROM chunks, websearch_to_tsquery('simple', :q) q
WHERE document_id = :doc AND to_tsvector('simple', text) @@ q
ORDER BY rank DESC LIMIT 8;
```

融合用 Reciprocal Rank Fusion：`score(c) = 1/(60 + rank_vec) + 1/(60 + rank_kw)`，取 top 4。
选 RRF 而不是加权分数：向量距离与 `ts_rank` 量纲完全不同，加权要调两个超参且换模型就失效；
RRF 只看名次，无量纲、无需调参。

**相关性下限**（`qa_min_similarity`，缺省 0.45）：向量路要求余弦超过它才算命中。
没有这个下限，`LIMIT k` 永远返回东西——问一个与文档无关的问题也会拿到几条不相干的"出处"，
裁剪成功时还标着"已做视觉验证"，出处就成了假证据。取值依据 bge-m3 实测分布：
相关问题的最佳命中 0.72~0.79，无关问题 0.25~0.38。

**关键词路的现状要说清楚**：`websearch_to_tsquery` 用 AND 连接所有 token，且 `simple` 配置
不做中文分词（整句被当成一个 token）。所以**自然语言提问时关键词路基本不命中，QA 实际以向量路为主**；
`/api/search` 收的是关键词式输入，不受影响。要让中文关键词路真正起作用需要装分词插件
（zhparser / pg_jieba），列为后续项。这条影响两件事：
① 「混合检索」在 QA 场景名不副实；② `embedding_unavailable` 降级时中文提问其实检索不到内容——
但降级标记是如实打的（`qa.py` 里 `embedding_unavailable` 优先于 `no_hits`），不属于静默降级。

跨文档搜索（`/api/search`）用同一套 SQL，去掉 `document_id` 过滤，按文档分组。**1b 起不再加 `user_id` 过滤** —— 语料是整个部署共享的，检索天然跨全语料。

**验收**：归档后 60s 内 `index_status='ready'` 且 chunks 行数 == 契约返回块数、embedding 非空；
重解析后不会跨版本混检；索引失败在 UI 上可见且带原因。

---

## 5. 问答（本轮核心）

### 5.1 接口

| 方法 | 路径 |
|---|---|
| POST | `/api/documents/{id}/conversations` |
| GET | `/api/conversations?document=` |
| GET | `/api/conversations/{cid}/messages` |
| POST | `/api/conversations/{cid}/ask` （SSE） |
| DELETE | `/api/conversations/{cid}` |
| GET | `/api/documents/{id}/crops/{crop_key}` （JWT，惰性生成 + MinIO 缓存） |

SSE 帧：

```
event: meta       data: {"retrieval":{"chunk_ids":[…]}}   （message_id 在 done 帧给——
                                                          回答落库发生在流结束之后）
event: delta      data: {"text":"部分回答"}        （多帧）
event: citations  data: {"citations":[{…}]}
event: done       data: {"message_id":"…","verified":true,"degraded":null}
event: error      data: {"message":"…","code":"index_not_ready"}
```

前端用 `fetch` + `ReadableStream` 消费——`EventSource` 发不出 Authorization 头。

### 5.2 处理流程

```
1. JWT 鉴权 + 会话归属校验
2. 用户级 QA 令牌桶（Redis）+ index_status 检查
3. 落 user message（请求作用域 session）
4. embed(question) -> 混合检索 -> top 4 chunks
5. 裁剪 top-1（最多 2 个）chunk 的 bbox 区域
   缓存键 results/{job_id}/crops/{page_idx}_{sha1(bbox)[:12]}.png，命中即用
6. 组 prompt -> service POST /v1/chat/completions (stream=true)
7. 边收边转 SSE delta 转发
8. 流结束：另开 session 落 assistant message + evidence/citations + usage(kind='qa')
```

### 5.3 Prompt 与预算

```
system: 只依据【资料】回答；资料中没有的必须回答"文档中未找到"。引用用 [1][2] 标注。
user:   [image: crop of page N]（有裁剪时）
        【资料】[1](第 3 页) …  [2](第 7 页) …
        【最近对话】最多 6 条
        【问题】…
```

chunk 文本合计上限 6000 字符（按 `char_len` 累加截断），对话历史上限 6 条。
M5 那种"短文档全文即证据"的路径**不能**沿用到 Web：会让长文档超上下文且账单失控。

### 5.4 降级路径，全部必须可见

| 情况 | 行为 | 返回 |
|---|---|---|
| `index_status != ready` | 拒绝并说明 | `event: error`, code=`index_not_ready` |
| 检索零命中 | 回答"未在本文档中找到相关内容" | `degraded="no_hits", citations=[]` |
| **问题向量化失败** | **只走关键词路**（绝不用零向量顶替） | `degraded="embedding_unavailable"` |
| VQA 不可用（dev 常态） | 纯文本问答 | `degraded="vision_unavailable"` |
| 非 PDF 无法裁剪 | 跳过视觉验证 | `degraded="crop_unsupported"` |
| 裁剪渲染失败 | 同上 | `degraded="crop_failed"` |
| 上游中途断流 | 保留已产出文本 + error 帧 | `degraded="upstream_error"`, code=`upstream_interrupted` |
| 客户端断开 | 已产出文本照样落库（尽力标记） | `degraded="client_aborted"`（不保证，见下） |

以上除 `index_not_ready` 外都令 `verified=false`。

**零向量替代是禁止的**：向量化失败时若拿全零向量继续检索，`<=>` 给不出有意义的名次，
等于把任意 N 条 chunk 以满权重灌进 RRF —— 结果照返、用户无感，比丢掉语义检索更糟。

**`client_aborted` 是尽力而为**：客户端断开是否会在生成器里抛异常取决于 ASGI 服务器
（进程内传输下流往往是"正常结束"，标记就打不上）。真正的保证是 `finally` 里
`asyncio.shield` 住的落库——不管走哪条路，已产出的文本都不丢。

M4a 的教训是"静默退回 BM25 没人知道"，这里不能重演成"静默不做视觉验证"。前端必须在回答上打标。

### 5.5 三个已知会踩的坑

1. **SSE 生成器里 DB session 已关闭**——M5 在 proxy 上踩过（见 `app/routers/proxy.py` 模块 docstring）。
   第 8 步必须 `async with get_sessionmaker()() as s:` 新开会话，不能捕获依赖注入的那个。
2. **客户端中断**：用户关页面 → 生成器被取消。`finally` 里把已累积文本落库并标
   `degraded="client_aborted"`，否则用户回来看到一条只有提问没有回答的会话。
3. **坐标换算**：沿用 `mcp_server/server.py::_crop_page_region` 的同一套
   `sx = img.width / page_size[0]` 比例。缺 `page_size` 退回 pdfium 页尺寸会在
   CropBox 偏移/旋转页上裁错——所以 chunks 必须带 `page_size`。

新增后端依赖：`pypdfium2`（裁剪，service 侧同款）、`pgvector`。

---

## 6. 前端

### 6.1 路由

| 路由 | 变化 |
|---|---|
| `/documents` | 由 `/dashboard` 改造：批量上传、跨文档搜索、状态筛选、分页 |
| `/documents/:id` | **三栏工作台**（全新） |
| `/documents/:id/versions` | 解析版本对比、设为当前、删除（新增） |
| `/search` | 全局搜索结果，按文档分组（新增） |
| `/settings` | 默认解析引擎/参数（新增） |
| `/login` `/keys` `/usage` | 保留 |

### 6.2 三栏工作台

```
DocumentWorkbench.vue          三栏 + 可拖拽分隔条 + activePage/activeHighlight
├── PageRail.vue               左：页码缩略图（IntersectionObserver 懒加载）
├── PdfCanvas.vue              中左：pdfjs 渲染 + bbox 高亮层
│     props { src, page, highlights }   emits { pageChange, regionClick }
├── ResultPane.vue             中右：按页分组结果 / Markdown 源码 双视图
│     props { pages, activePage }       emits { blockClick(page, bbox) }
└── AskPanel.vue               右：会话式问答（可折叠）
      ├── ConversationList.vue
      ├── MessageStream.vue    流式打字机
      └── CitationChip.vue     点击 -> 同时驱动 PdfCanvas 与 ResultPane
```

### 6.3 坐标换算与高亮

- bbox 基于 `page_size`（PDF 点，72dpi），canvas 是渲染像素：
  `sx = canvas.width / page_size[0]`、`sy = canvas.height / page_size[1]`
- 高亮层是覆盖在 canvas 上的绝对定位 `div`，用 `transform: scale()` 跟随缩放。
  **不要画在 canvas 上**：重绘一次就没了，也做不了 hover/点击。
- 为什么弃用 `<iframe>`：iframe 内是浏览器自带 PDF 阅读器，拿不到坐标系，做不了叠加。

### 6.4 对齐粒度：页级，不是字符级

markdown 是生成文本，与 layout 块逐字符映射不可靠（表格、公式、跨栏都会错位）。
`GET /api/documents/{id}/pages` 返回按页分组的块，中栏按页渲染并插页锚点；
块级高亮只用于两种确定场景——问答出处、用户点击选中。
强行做字符级会得到"时对时错"的高亮，比不做更糟。

### 6.5 批量上传与性能

- 并发 3（简单信号量，不引库）；逐项进度，单项失败可重试，不中断整批
- 前端先用 `crypto.subtle.digest` 算 sha256 做秒传提示
- 中栏按页虚拟滚动，只渲染视口 ±2 页（200 页文档 DOM 才不炸）
- pdfjs worker 用 Vite `?worker` 导入，不依赖 CDN（离线自部署必须）

新增前端依赖：`pdfjs-dist`、`@vueuse/core`。
保留 M5 已验证的两条约束：Markdown 必过 DOMPurify（含现有的 KaTeX 占位符方案，不要动）；
受 JWT 保护的图片走 blob 取回。

---

## 7. 多副本生产改造

| 事项 | 实现 |
|---|---|
| 限速 | `REDIS_URL` 配了就用 Redis 令牌桶（`EVAL` Lua 原子脚本，键 `rl:{key}`，TTL 120s），否则进程内滑窗。接口都是 `await check(key, limit)` |
| 对账选主 | 每副本都跑对账循环，但每轮先抢 `SET ddp:reconcile:tick NX EX` 锁；抢不到就跳过这一轮 |
| 归档 / 索引并发 | DB claim（`UPDATE ... WHERE status IN (...)`）—— 重复执行是空转，多副本天然安全 |
| 对象回收 | 软删除 + `gc.collect_deleted_objects()`，跟在对账循环里跑，删完把 `object_key` 置空作标记 |
| 探针 | `/healthz`（存活，不查依赖）+ `/readyz`（探 PG / MinIO / service / Redis，逐项报状态） |
| 可观测 | `prometheus-fastapi-instrumentator` 的 `/metrics`，与 service 侧口径一致 |

**与初版设计的偏差**：没有引入独立的 ARQ worker 进程。原因是归档与索引都已经用 DB claim
做成幂等的，重复执行只是空转；再加一把 Redis 锁就消掉了"N 个副本重复全表扫描"这唯一的浪费。
多引入一个部署单元（worker 镜像 + 编排 + 独立监控）在当前任务量下成本大于收益。
若将来索引任务重到会拖慢 API 进程（长文档 embedding 占住事件循环），
再把 `index_document` / `collect_deleted_objects` 平移到 ARQ —— 两者都是纯函数式的
`(session, storage, http, id)` 签名，平移不需要改逻辑。

---

## 8. 里程碑与验收

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| M6-a | 模型拆分 + 迁移 + 路径迁到 `/api/documents/*`（旧路径 301 兼容一版） | 单测改造后全绿；真 PG 上迁移前后数据一致 |
| M6-b | 本层分块（读归档 layout.json）+ pgvector 索引 + 归档链追加向量化 | e2e 断言 1；索引失败可见 |
| M6-c | 问答后端（SSE + 出处 + crop 缓存） | e2e 断言 2、3 |
| M6-d | 三栏工作台：pdfjs + bbox 联动 + 问答面板 | UI 验证通过；200 页文档不卡 |
| M6-e | 重解析与版本、批量上传/导出、跨文档搜索 | e2e 断言 4、5 |
| M6-f | Redis 令牌桶限速、对账选主锁、GC、metrics、`/readyz`（**不含独立 ARQ worker**，见 §7） | 双副本限速、GC 回收 |

### 单测矩阵（沿用 M5 装配：SQLite in-memory + respx + Memory 替身）

| 部分 | 用例要点 |
|---|---|
| 迁移 | 模型与迁移无漂移；`Vector` 在 SQLite 上能建表 |
| 契约 | （service 仓库）chunks 返回形态、未就绪 409 |
| 索引 | 批量切分；重复投递只索引一次；失败落 `failed` 且带原因；先删后插 |
| 问答 | SSE 帧序列完整；三种降级各一例；**流结束后消息确实落库**（防 session 复用回归）；客户端中断落库 |
| 前端 | `npm run build` 类型检查；坐标换算纯函数单测 |
| 多副本 | 令牌桶 Lua 边界（恰好用完、跨窗口恢复） |

### e2e 新增断言（扩 `scripts/e2e_web.py`）

1. 归档后 chunks 有向量
2. 问答 citations 页码与 `layout.json` 对得上；crop 图片可取回且非空
3. 停掉 VQA 后回答仍返回且 `degraded="vision_unavailable"`
4. 换参数重解析产生新 ParseJob，旧版本仍可读，切 current_job 后结果随之变化
5. 跨文档搜索命中另一篇文档
6. **起两个 backend 副本**，限速在两副本间共享生效
7. 软删除后 GC 确实清掉了 MinIO 对象

每阶段收尾按项目硬性流程：自验（pytest 全绿 + 真机 e2e）→ 子 agent 独立验收 → 修阻塞项 →
README 勾选 → commit → push。

---

## 9. 风险与取舍

| 取舍 | 理由 |
|---|---|
| 两层各存一份向量索引 | service 那份是 24h 可重建缓存，本层是永久索引；换 service 无状态不被破坏（ADR #6 不动） |
| 对齐做到页级而非字符级 | markdown 是生成文本，逐字符映射会时对时错，比不做更糟 |
| 索引失败不自动重试 | 重试要重跑全量 embedding，成本高且大概率同样失败；改为显式可见 + 手动 reindex |
| 问答只带最近 6 轮 + top4 chunk | 不做全文摘要记忆；成本与准确性的权衡等有真实用量再调 |
| 迁移不搬 MinIO 对象 | 搬对象不可逆，`result_prefix` 保持指向原前缀即可。**读归档一律走 `storage.prefix_of(job)`**，不要用 `job.id` 拼——迁移过来的 job 两者不同 |
| QA 事实上以向量路为主 | 中文分词要装 PG 插件，本轮不做；降级标记如实，不算静默（见 §4.3） |
| embedding 维度写死 1024 | 换模型需 `reindex-all`；关系库里用"索引名带维度"打补丁不合适 |

**最大风险**：引入 pgvector 后单测不能再跑 SQLite。已用 `TypeDecorator` + `SearchIndex` 协议规避，
这是 §2.5 必须先做对的地方——做砸了整个 M6 的测试反馈速度都会塌。
