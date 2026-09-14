# DeepDocParse corpus MCP v1

状态：**v1 工具签名冻结；v1.2 增补固定解析与资源上下文出参**。作用域是**调用者有权读到的那部分语料**，
不要求先指定文档。所有成功返回的知识结论都带
`evidence_id + page_idx + bbox + page_size + crop_url`；
无法定位时必须显式 `resolved=false` 或 `unsupported=true`。

## 传输与身份（v1.2）

MCP 服务**不做用户鉴权，也不再自己读数据库与对象存储**。它是一层适配：

```
MCP 客户端 --sk-key--> control-api（验 key / 配额 / 限速）
                         |  剥掉客户端的同名头，填入 actor 上下文 + 服务凭据
                         v
                     mcp 服务（本契约）
                         |  原样转发 actor 上下文 + 自己的服务凭据
                         v
                     corpus-api  /internal/mcp/*（授权与 /api/* 同一条链）
```

因此：

- 外部客户端连接 control-api 的 `/mcp/`，只发送 `Authorization: Bearer <sk-user-key>`。
- MCP 服务内部收到的每次工具调用**必须**带 `Authorization: Bearer <SERVICE_TOKEN>` 与一组
  `X-DDP-Organization / X-DDP-Actor / X-DDP-Actor-Kind / X-DDP-Role`
  （用 API key 调用时还有 `X-DDP-User`，它是这把 key 背后的真实主体）。
  这些头**由入口下发，客户端不能自带** —— 入口会无条件剥掉同名头。
- **缺服务凭据或四项必填 actor 头是错误，不是降级**：工具返回 error，不返回空结果。
  API key 若缺少可信 `X-DDP-User`，不能获得用户私有资源权限。
  把"没有身份"渲染成"语料里没有"会让一次鉴权配置错看起来像一份空语料。
- 未携带入口服务凭据的直连工具调用被拒；初始化/工具清单不等于读取资源的许可。
- MCP 部署只持有上游地址与服务凭据，不配置数据库或对象存储密钥。

## 授权

- `search` / `ask` 的候选在**排序与截取前**按可读固定 `parse_job_id` 收作用域。
  相同字节共用 Document，不代表另一资产的解析、生成证据或文件名可读。
- `get_evidence` 按证据的固定解析版本判权。**无权与不存在同形（`not_found`）**。
  取裁图后的权限复核发生在返回像素之前。
- `read_wiki` / `graph_neighbors` 复用知识平面的资源及固定版本依赖判据，
  返回前再次复核生成物和出处权限；来源撤销不能经知识文本或 snippet 绕过。
- `resource_id / source_version_id / parse_revision / filename` 来自授权资产版本。
  同一解析对应多份可读资产时，`copies` 列出全部合法上下文；不会因共用 Document
  就默认选取别人的资源，也不会仅因有多份合法副本而返回 409。
- 模型派发前及返回后都复核来源。任一实际进入 prompt 的证据失权，整份回答
  返回错误；不能删除引用后仍返回来源内容，也不能重新编号使 `[n]` 指向别的证据。
- 文件名及 `copies` 在响应前刷新，已经撤销的资产别名不会残留。

## 工具

### `search(query, limit=10)`

返回跨语料混合检索结果（作用域 = 调用者可读的固定解析版本）：

```json
{"results":[{"evidence_id":"...","document_id":"...","parse_revision":"...",
"resource_id":"...","source_version_id":"...","filename":"manual.pdf",
"copies":[{"resource_id":"...","source_version_id":"...","filename":"manual.pdf"}],"page_idx":0,
"bbox":[0,0,10,10],"page_size":[612,792],"crop_url":"...",
"snippet":"...","score":0.03,"similarity":0.82,"source_type":"source"}],
"degraded":null,"scope":{"authorized_parse_revisions":1}}
```

`scope.authorized_parse_revisions` 是本次调用可读的固定解析数量，不是全库总数。
零作用域和有作用域但未命中都返回明确形状；空问题为 `degraded=empty_query`。

`crop_url` 是**绝对地址**（MCP 按部署的对外基址补全），指向受鉴权保护的稳定裁图路径，
不是对象存储的预签名地址。

### `ask(question)`

返回 DDP-Agent v1 `Assertion[]`，每条包含 `text / evidence_ids / verification /
unsupported / citations`。不得退回无类型的整段字符串。
无可用证据时返回一条 `unsupported=true` 的断言加 `degraded=no_hits`，
**不会**脱离证据作答。返回还包含与 `search` 同义的 `scope`；
生成不可用返回 `assertions=[] / degraded=answer_unavailable`，不能伪装成文档中没有答案。

### `get_evidence(evidence_id)`

返回证据元数据、原文和裁图。HTTP/JSON 结果包含 `crop_url`；MCP 响应同时附带
原生 image content（裁图存在时），让外部 agent 能自行核对。

`crop_degraded` 区分「这条证据本来就没有裁图」与「裁图取不到」：

| 值 | 含义 |
|---|---|
| `null` | 没有降级：要么图已随响应返回，要么这条证据本就没有 `crop_key` |
| `crop_store_unavailable` | 有 `crop_key`，但对象存储没配/依赖没装，拿不到像素 |
| `crop_read_failed` | 有 `crop_key`，对象存储可达但这一次读取失败 |

**取不到图不许静默退化成"没有图"** —— 外部 agent 会据此以为这条证据无法核对。

### `read_wiki(entry_id_or_title)`

返回 DDP-Graph v1 Wiki 形状。每个句子必须有有效 `evidence_ids`，否则显式
`unsupported=true`；冲突句以 `conflict_group` 并列。

### `graph_neighbors(entity_id_or_name, depth=1)`

返回中心节点、N 跳节点与边。每条有效边带证据详情；`depth` 范围 `1..3`
（越界返回 `{"status":"invalid_depth"}`，不调用后端）。

## 兼容工具

`ask_document(file_url, question)` 保留原签名并标 deprecated，不删除。
**v1.1 收紧了它的取数边界**（越权修复，不是签名变更）：

- 只受理**本部署之外**的 http(s) 地址。指向本部署自身服务、对象存储、
  稳定文件 URL（`/files/{token}`）、回环或内网地址的一律拒绝：
  一个裸 URL 表达不了"我有权读它"，而那些地址背后是受资源 ACL 保护的内容。
  **已入库的文档请用 `search` / `get_evidence`**。
- 解析走语料平面的 `/v1/parse*` 并带上调用者身份，不再直连模型网关。
  网关有解析缓存，直连意味着"猜中一个已被解析过的地址"就能拿到
  别人那次解析的全文；语料平面按主体分域，并在取结果前要求这次任务对本人可见。
- 因此这个工具产生的解析**会记在调用者名下并计量**，与对外 `/v1/parse` 一致。

## 错误与降级

- 查无实体、wiki 或证据：结构化 `not_found`，不得编造空壳内容。
- 无身份 / 凭据不对：工具报错（不是空结果，也不是匿名放行）。
- embedding/rerank/VLM 不可用：结果仍可返回，但 `degraded` 必须给稳定原因码
  （取值来自 `packages/contracts/enums.yaml` 的 `degraded`，不得自造）。
- 数据库或对象存储不可用：工具失败并给明确错误；不得返回看似成功的空数组。

## 真实环境验收

运行 `scripts/e2e_mcp.py --report /tmp/mcp-v3-report.json`，脚本只经过
`CONTROL_BASE_URL`（默认 `http://127.0.0.1:8080`）的 `/mcp/` 用户认证入口。
不再直连 9100、不伪造 `X-DDP-*`、不接受 SERVICE_TOKEN 代替用户 key，也不按
URL 哈希猜 Redis 的旧索引键。

环境需预先存在一份私有、固定解析索引就绪的资源，以及依赖它的 Wiki 和图谱实体：

| 环境变量 | 用途 |
|---|---|
| `MCP_API_KEY` | 资源所有者的用户 API key |
| `MCP_OTHER_API_KEY` | 另一个无源资源权限用户的 API key |
| `MCP_E2E_QUERY` | 能命中资源的检索问题；不得含下面的私有答案标记 |
| `MCP_E2E_EXPECTED_TEXT` | 原文中的确定事实，用于检查正向结果与反向泄漏 |
| `MCP_E2E_EVIDENCE_ID` | 上述事实的固定证据 ID |
| `MCP_E2E_WIKI` | 引用这条证据的 Wiki ID 或标题 |
| `MCP_E2E_ENTITY` | 引用这条证据的图谱实体 ID 或名称 |
| `MCP_E2E_REQUIRE_CROP=1` | 可选；要求 `get_evidence` 真实返回原生图片内容 |

脚本实际调用全部五个工具，验证所有者能取得指定证据，另一用户不能得到私有
证据、原文或依赖知识，并验证匿名入口被拒。它不上传、不发布资源。
退出码 `0` 表示全部通过，`1` 表示实测失败，`2` 表示缺前置条件；缺配置不会
被记成跳过后通过。报告记录检查结果、时间和 Python/FastMCP 版本，不保存 key
或私有原文。模型、向量后端与部署版本仍须由本次部署验收记录提供。

这组验收不代表 deprecated `ask_document` 的远程 URL/VQA/GPU 管线已通过；
本机回环 fixtures 已不属于该工具允许输入，相关兼容流程由其独立测试覆盖。
