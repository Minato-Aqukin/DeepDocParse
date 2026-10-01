# DDP discovery control v1

控制域拥有持久节点身份、管理员批准的直接成员目录、成员快照与 [ScopeManifest](scope-control-format.md)。它不拥有资源 ACL、集合内容或跨节点执行任务。节点被批准不授予任何资源权限；不扫描地址。已批准成员的租约由目录自动 pull 续期，不允许同伴 push 修改管理员目录（写端点仅接受管理员会话）。

## 身份与认证握手

`GET /api/v1/federation/node` 无鉴权，只返回 `authority_node_id`、`public_key`（Ed25519 base64）、`key_fingerprint` 和 `descriptor`（DDP NodeDescriptor）；不含组织/用户。身份由公钥 SHA256 导出：`node-` + 前 48 个小写十六进制字符。地址改变、重启、升级不改变身份。

`NODE_IDENTITY_DIR` 保存权限 0600 的 Ed25519 seed，目录权限 0700，必须持久挂载并与数据库一致备份。数据库只保存公钥/指纹，不存或返回私钥。已有数据库身份但 seed 遗失、权限不安全、公钥不匹配均拒绝启动；不得静默生成新身份。新中心/恢复副本不得同时以同一 authority 身份独立运行。

`GET /api/v1/capabilities` 与 `/api/v1/client/handshake`、`/api/v1/federation/capabilities` 是同一个认证处理器。浏览器会话或带 `read` scope 的用户 API key 可调用。返回 `protocol_version: ddp-client/1`、`identity: {environment_id,authority_node_id,workspace_id}`、`profile: {issuer,subject}`；environment_id/issuer 是当前节点，workspace_id 是已验证组织，subject 是真实用户 ID。调用者提供的内部身份头一律剥除。

`profiles` 来自固定配置的 corpus `/internal/capabilities`，使用服务凭据和已验证 actor，禁止重定向。上游未接线、不可达、格式不符或过期时返回 `capability_status: unknown`、空 profiles、`accepting_admissions: false`，不将配置或 GPU 存在误报 ready。未实现的 client snapshot/events/receipt 投影不宣称支持。

## 管理员目录

仅管理员会话可以 `POST /api/v1/federation/nodes`，body 为 `{descriptor,public_key,visible_to_org?,allowed_subjects?}`。public_key 必须匹配 node_id；descriptor 的 revision 和 valid_until 必须有效，协议包含 ddp-discovery/1，端点只接受不含凭据/query/fragment 的 HTTP(S) URL。登记为 pending，不发出任何请求。`visible_to_org` 默认 false；allowed_subjects 是本组织真实成员 ID。配置变更必须更高 descriptor revision；同 revision 仅允许配置/公钥/共享范围不变且 valid_until 严格增加的租约刷新，保留原审批。已撤销 ID 不可重用（需另行密钥轮换协议）。仅允许本中心到成员的直接无环路径，不接受客户端指定 next-hop/path。

`POST .../nodes/{node_id}/approve` 将 pending 变成 approved。`POST .../nodes/{node_id}/revoke` 从 pending/approved 变成 revoked（幂等）。`GET .../nodes` 仅管理员列出配置。NodeDescriptor、派生 RouteRecord、审批状态分开返回。`configured=true` 只表示批准的配置；`health=unknown`、`accepting_admissions=false`，续期成功不等于可接单。管理员批准并不证明远端持有私钥；后续请求仍须使用限定节点凭据。

## 持久成员快照

`POST /api/v1/federation/member-snapshots` body `{page_size?: 1..100, ttl_seconds?: 30..3600}`。服务端在目录修订锁内冻结调用者可见的 approved 成员，按 node_id 稳定排序和去重。响应包含 `snapshot_id,authority_node_id,caller_scope_hash,registry_revision,created_at,expires_at,first_cursor,terminal_cursor`。caller_scope_hash 由真实用户、组织、角色、凭据类型/ID/scope 计算，拒绝客户端指定；没有未授权成员数或全局计数。目录成员与内容索引修订不混用。

`registry_revision` 属于 `(organization_id, caller_scope_hash)` 下的持久可见视图：首次观测为 1；仅当该调用者下一次创建快照时，可见成员集合/描述/成员修订与上次观测不同时加 1。它不是目录写入次数，也不跨调用者比较。不可见成员的登记、审批、更新、撤销不改变此值。时间、分页大小、TTL 和服务重启不改变同一视图修订。成员 `revision` 使用单个成员自己的计数，不携带组织目录计数。内部全目录锁仍用于冻结一致的成员集合，管理接口的 `registry_revision` 仅供管理员追踪目录修改，与快照的可见视图修订不是同一序列。

数据库迁移 `0006_discovery_scoped_revision` 为可见视图创建 control 独占表，并使升级前携带全目录计数的旧快照过期；旧游标须重新建范围，不能沿用其旧计数。用户 API key 的撤销或当前 scope/角色变化会在重新鉴权时拒绝访问原快照；同一用户的会话与不同 API key 也不能互换游标。

`GET .../member-snapshots/{snapshot_id}/members?cursor=...` 在数据库核对 snapshot + caller_scope_hash + cursor。省略 cursor 从 first_cursor 开始。每页返回快照元数据与 `members,next_cursor,complete`。游标是持久不透明随机值，不可移植到其他 snapshot/主体。末页 next_cursor 指向稳定 terminal_cursor；请求 terminal_cursor 永远返回空 members、next_cursor=null、complete=true。空目录直接 first_cursor=terminal_cursor。过期 410 `scope_expired`；错误 snapshot/cursor/主体均 404，不能借此枚举其他范围。

目录变化不改变旧快照成员集合或 registry_revision。新成员仅进入新快照；撤销成员保留旧快照位置与 node_id，状态 `revoked` 且不再返回描述/路径，不能悄悄从分母删除。未实现下级枚举的成员显式 `expansion_state: unexpanded_subtree`，直接可枚举成员为 `not_requested`；本端只保证本目录枚举终止，不声称递归全局覆盖。

## 配对前持有密钥证明

公开 `GET /api/v1/federation/node?challenge=<nonce>` 支持单个32..64字符、无padding且规范编码的base64url nonce。除公共描述外返回 `proof: {schema: ddp-node-proof/1,nonce,node_id,endpoint,issued_at,expires_at,signature}`。endpoint只来自 `PublicBaseURL`（去末尾 `/`），不读取 Host、Forwarded 或客户端指定地址。部署使用规范ASCII HTTP(S) URL；国际域名先转换为punycode。

签名消息是UTF8 `JSON.stringify([schema,nonce,node_id,endpoint,issued_at,expires_at])`；Go关闭HTML转义以与JS一致。时间是UTC RFC3339整秒，有效60秒；signature是Ed25519签名的raw URL base64。public_key仍是标准带padding的base64。Provider在读取用户secret前验证公钥摘要派生node_id、保存的节点、当前随机nonce、配置endpoint、时间窗和签名；仅返回可复制的公钥或node_id不能通过配对。证明不授予资源访问权限，也不能代替后续已认证profile与workspace核对。

## 自动租约续期（directory pull）

目录每 `DISCOVERY_RENEWAL_INTERVAL_SECONDS`（默认60，范围1..120，低于公开描述300秒TTL）调度已批准直接成员。使用独立的短轮询（最多1秒，不超过续期间隔的四分之一）领取到期行，成功后的下一次到期是成功时间加配置间隔，不因错过同频ticker而翻倍。最多四个并发请求，每个两秒超时；单次领取最多32个到期成员，满批立即继续领取，直到非满批或ctx取消再等待轮询。多副本使用数据库行锁领取。pending / revoked 不领取；请求与回写之间若管理员改动或撤销，旧观测不得回写。

目标只由**已批准登记**的唯一 federation 端点去末尾 `/` 后加 `/node` 得到；不读取拉回描述里的地址作下一跳，不跟重定向、不读代理环境变量。响应 authority_node_id / public_key 与批准值逐字一致，描述身份、公钥、协议和有效期验证通过，revision与配置不变，`publisher_signature` 必须由批准的 Ed25519 公钥验过；只有 valid_until 严格增加且不超过当前观测时间加10分钟（5分钟发布TTL加有界时钟偏移余量）才刷新。拉取及事务回写均检查此上限，防止任意远未来租约在失败后长期假新鲜。配置变更仍需人工更高修订登记和批准。

`publisher_signature` 使用标准带padding base64，非空值必须为严格规范base64、解码恰好64字节（拒绝换行和非零padding bits）。签名消息为 `ddp-node-descriptor/1\n` 加 UTF8 无空白、无HTML转义JSON数组：
`[schema,node_id,protocol_versions,controlled_endpoints.map(e=>[e.purpose,e.url]),auth_methods,enumerate_members,catalog_events,revision,valid_until]`。
签名描述的所有字符串字段限定ASCII（码点0..127）；端点URL仍须满足既有HTTP(S)无凭据/query/fragment限制。这样Go、JavaScript与Python不会在U+2028/U+2029或非ASCII转义上产生歧义。时间规范为UTC RFC3339**整秒** `YYYY-MM-DDTHH:MM:SSZ`，发布时去除小数秒，验签不接受小数秒；数组顺序与配置一致，不包括签名本身。ASCII控制字符按JSON标准使用短转义或小写 `\\u00xx`，引号/反斜杠正常转义，`/ < > &` 不转义。Python等价为 `json.dumps(array, ensure_ascii=False, separators=(",", ":"))`，JavaScript为 `JSON.stringify(array)`。该签名绑定完整描述和租约，不证明资源权限、健康或接单意愿。

失败持久保留 `renewal_attempts,renewal_last_attempt_at,renewal_next_attempt_at,renewal_last_error,renewal_last_success_at`，按间隔指数退避，上限120秒；日志 `discovery renewal` 含 node_id / outcome / error（不含凭据）。失败不改 descriptor.valid_until，过期仍由 API 的 descriptor / route.valid_until 与范围展开显示为过期，不能靠最近一次成功假称新鲜。成功清除连续失败计数/原因。旧分页快照的冻结描述/有效期不被延长；重新创建快照才看到新租约。可通过成员行的成功时间与 valid_until 增长、失败原因/计数以及上述日志验证运行。
