# 本机工作区内容子集（local-content-subset）

本机工作区（`python/ddp_local` 回环运行时）实现中心内容接口的子集：
**同路径、同形状**（`packages/contracts/openapi/content-v1.yaml`），Web 页面一套代码
跑在两种数据源上。差异只在这里列出的这些，其余一律与中心一致。

约定：子集之外的中心 `/api/*` 操作一律 `404 {"error":{"code":"not_supported_locally",…}}`。
`not_supported_locally` 与其它四个桌面错误码（`no_active_source`、
`approved_plan_required`、`source_signed_out`、`source_changed`）一样，
只在 `packages/contracts/enums.yaml#source_error` 里定义，不要另抄一份。

## 实现的操作

- `GET /api/auth/me` —— 单一属主：本地只有一个 owner，role 为有全部内容权限的
  角色（如 admin）。无组织概念，`organization_id` 为本地固定值。
- 资源：`GET /api/resources?scope=mine&offset&limit`（`scope=site_public` → 空列表，
  本机无公开目录）；`DELETE /api/resources/{id}`；追加版本走上传
  `target_resource_id`；bundle 导出按 bundle-v1 实现（若成本高可先不做，
  缺席即 404 `not_supported_locally`）。
- 上传（control-v1 形状）：`POST /api/uploads` → 会话的分片 URL 是**同源相对路径**
  （`/api/uploads/{id}/parts/{n}`），`PUT` 这些分片，`POST /api/uploads/{id}/finalize`，
  `GET /api/uploads/{id}` 带 `ingest_status` → 建资源/版本并走现有运行时逻辑解析。
  预签名直传对象存储那条路本机没有（没有对象存储可签）。
- 文档（一个本地版本 ≈ 一份文档）：`GET /api/documents`、
  `GET /api/documents/stats/summary`、`GET /api/documents/{id}`
  （含 `resource_id`/`source_version_id`）、`/pages`、`/layout`、`/result`（能推导的才给）、
  `GET /api/documents/{id}/download-url` → `{"url": "/api/documents/{id}/source", …}` 相对地址、
  `GET /api/documents/{id}/source` 返回原件字节、`DELETE`、`GET /jobs`。
  reparse/reindex 若无意义可回 `not_supported_locally`。
- 检索：`GET /api/search?q&doc`。
- 证据：`GET /api/evidence/{id}`（`/backlinks` 若便宜就给）；裁图（crops）本机不做 ——
  Web 的 EvidencePreview 走降级展示（无图、有原文与定位），见下。
- 会话与问答：`POST /api/documents/{id}/conversations`、
  `GET /api/conversations?document=`、`GET /api/conversations/{cid}/messages`、
  `DELETE /api/conversations/{cid}`、`POST /api/conversations/{cid}/ask`（SSE，
  事件名与形状与中心一致：`meta`、`delta`、`citations`、`assertions`、`done`、`error`）。
- Wiki（wiki-v1 形状）：`GET/POST /api/wikis`、`GET /api/wikis/{id}`、
  `GET /api/wikis/{id}/revisions/{rid}`、`POST /api/wikis/{id}/revisions`、
  `PATCH /api/wikis/{id}/pages/{key}`。
- `GET /api/v1/capabilities` 增加 `content_features: SourceFeature[]`
 （含 `federation_tasks`，因为本机计划账本存在），以及与 `client_handshake` 同源的
  `identity`（environment/workspace/authority）与 `profile`（issuer/subject）。

## 不实现的操作（404 not_supported_locally）

- `PATCH /api/resources/{id}` publication（本机无公开/组织，无处可发布）。
- `POST /api/wikis/{id}/publish`（同上）。
- reparse / reindex（若本机运行时无此概念）。
- 其余 Web 可能调用的 `/api/*`：一律 404 `not_supported_locally`。

## 本机语义差异

- 单一属主：无登录、无组织、无成员，`auth/me` 永远是 owner。
- 无公开/组织：`site_public` 为空；发布类操作不支持（上节）。
- 资源来源：bundle 导入的资源以 `copied_from = remote:<authority_node_id>:<resource_id>`
  保留存量 bundle 的来源身份（与 resource-policy-format 的占位来源语义一致），
  本机原件为 null。每个固定版本返回 `federation_input_allowed`：
  存量 `source_json.authority_node_id` 不等于本机环境时为 false，否则为 true。
  这是现有本机许可解析器的能力投影，不改变来源策略，不把导入副本伪装成本机属主原件；
  false 版本不得作为任何联邦计划的锁定输入，包括只锁摘要的问答/Wiki 计划。
  两类选择器都须显示禁用原因；中心可省略此字段，批准与派发仍以服务端实时来源策略为准。
  草稿中仅明确返回 false 的已就绪输入可随策略提示移除；没有出现在已就绪列表中的引用
  （例如重建索引中或未落在当前分页窗口）仍须保留在草稿里，不得伪报为策略拒绝。
  生成计划时仍只锁定当前返回的已就绪版本。
- 问答不伪装流式：本地模型一次产出答案，`ask` 以**一个 `delta`** 发出全文
  （事件序列仍是 meta → delta → citations → assertions → done），前端照常拼接显示。
- 生成 stays `execution_policy=local_only, allow_remote=false`：本机问答绝不外发。
- 无裁图：`citation.crop_url` 为 null，`crop_url` 指向的 crops 路径回 404
  `not_supported_locally`；EvidencePreview 显示原文与定位，不显示"截图失败"。
- 问答不跨轮继承本机不支持时：按中心同一规则降级（`no_evidence_in_turn` /
  `inherited_evidence_incomplete` 拒绝），不凭空补答。
- 编译降级：`compile_degraded` 只输出 `enums.yaml#compile_degraded` 里的取值，
  运行时内部原因不外泄成"未知取值"。
- Wiki 过期：`revision.stale_reasons` 只用 `enums.yaml#wiki_stale_reason` 的取值 ——
  `source_withdrawn`、`source_unavailable`、`parse_revision_changed`、
  `source_digest_changed`，以及同一资源出现更新的已就绪版本而页面依赖都不在最新版上时的
  `source_version_changed`（在最新版上重建即解除）。从 bundle 导入的外部引用
  （本机无对应版本）不算过期，审阅状态随依赖本身显示。
- 既有本机私有路由（宿主用的 `/api/v1/*`：handshake、plans、models 等）保持可用，
  与本子集无冲突；但**渲染进程经宿主 `/api` 代理只能到 `GET /api/v1/capabilities`**，
  其余 `/api/v1/*` 一律 404 `not_supported_locally`（例：计划批准只能走原生对话框）。

## 校验

- `python/ddp_local` 的镜像测试用 content-v1 的同一套 schemas 断言响应形状
  （与 `services/corpus-api/tests/test_content_contract.py` 对称）。
- `scripts/check_content_contract.py` 钉住"契约声明的每条读端点都有实现"；
  `GET /api/documents/{id}/source` 那条中心豁免（LOCAL_ONLY_PATHS），只由本机实现。
