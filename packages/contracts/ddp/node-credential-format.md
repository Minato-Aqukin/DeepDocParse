# DDP-NODE-CREDENTIAL v1 —— 节点请求证明与限定委托

> 机器可读契约：`schemas/ddp-node-credential/v1.json`（Claims / SignRequest /
> SignedCredential / PeerTrust / NodeIdentity），端点：`openapi/federation-tasks-v1.yaml`
> 的 `security: nodeCredential` 与 `openapi/discovery-v1.yaml` 的三个 `/internal/federation/*`。
> 计划依据：§2.1 身份分层、§5.1 接入与治理、§8.4 身份授权与转委托、§9.5；验收 T32 / T43。

## 它取代了什么

旧形态里协调者手持**对端的** `SERVICE_TOKEN` 与一个所有同伴共用的
`FEDERATION_PEER_TOKEN`，并在请求头里**自报** actor（组织、用户、角色）。于是：
任何一个同伴都能冒充任何其他同伴；持有对端内网服务凭据的节点能调用对端的
任何内部路由并自称 admin；远端 `alice` 与本地 `alice` 在执行者那里是同一个人。

## 一张凭证是什么

**单次、短期（≤120s）、只授权一个操作**的 Ed25519 签名正文，同时充当：

- **节点请求证明**：签名 + `request`（方法 / 路由路径 / 正文摘要）证明这次 HTTP
  请求出自 `issuer_node_id`；
- **限定委托**：`audience_node_id`（唯一接收方）、`actor`（发行者组织内的主体）、
  `operation`、`constraints`（root_task_id 必带；step_id / scope_ref /
  task_spec_digest 按操作必带）、`issued_at` / `expires_at`、`jti`。

## 私钥只有一个所有者

控制面（Go）持有 `NODE_IDENTITY_DIR` 里的种子（不变式 5）。语料服务出站时对**本节点**
控制面 `POST /internal/federation/node-credentials`（服务凭据）提交 `SignRequest`，
控制面填 issuer / 时间 / 128 位随机 jti 并签名。控制面拒签：audience 不是本组织已批准
成员、audience 是自己、actor 不属于本组织、形状越界；按 audience 限速；每次签发记一条
结构化日志（audience、operation、root_task_id、jti，**不记凭证本身**）。

## 规范序列化与线格式

```
payload   = JSON(claims)，键按字典序，无空白（Python: sort_keys + separators(",", ":")；
            Go: map[string]any 经 json.Marshal）
签名输入   = "ddp-node-credential/1\n" || payload
线格式     = base64url(payload) "." base64url(signature)     # 无 padding
请求头     = X-DDP-Node-Credential
```

所有字符串被契约收窄到 `[A-Za-z0-9._:@+=-]`（路径多一个 `/`、digest 为
`sha256:<hex>`），时间是整数秒 —— 这是两种语言的 JSON 编码器**必然**逐字节一致的
子集（Go 会转义 `< > &`，Python 不会）。验证方解码后**重新规范化并要求与收到的
payload 逐字节相等**：重复键、空白、换序、非规范 base64 一律 `credential_invalid`。
冻结夹具 `tests/fixtures/node-credential-v1.json` 同时被 Go
（`credential_crosslang_test.go`，重签必须得到同一 token）与 Python
（`ddp_core/tests/test_node_credentials.py`，必须验过同一 token 并拒绝同一组反例）读取。

## 验证顺序（接收方语料服务）

| # | 判定 | 失败 |
|---|---|---|
| 1 | 头存在、≤4096 字符、严格解码、结构合法、规范字节 | 401 `peer_unauthenticated`（缺头）/ `credential_invalid` |
| 2 | `audience_node_id` 等于本节点绑定身份 | 401 `credential_audience_mismatch` |
| 3 | `issued_at ≤ now+30s` 且 `expires_at > now` | 401 `credential_expired` |
| 4 | `operation` 等于端点的 `x-ddp-node-credential-operation` | 403 `credential_operation_denied` |
| 5 | `request` 的方法 / 路由路径 / 正文 sha256 等于这次请求 | 403 `credential_scope_denied` |
| 6 | 向本节点控制面取 `PeerTrust`：未登记或 pending | 401 `node_unknown` |
| 7 | 　　revoked | 401 `node_revoked` |
| 8 | 　　`authority_node_id` 不等于本节点绑定身份 | 503 `node_identity_mismatch` |
| 9 | 公钥派生的 node_id 等于 issuer（防改坏的成员行） | 401 `node_unknown` |
| 10 | Ed25519 验签 | 401 `credential_invalid` |
| 11 | 持久记下 `jti` 直到 `expires_at`（唯一约束仲裁并发） | 401 `credential_replayed` |
| 12 | 端点把被读写的行与 `constraints` 逐字比对 | 403 `credential_scope_denied` |
| 13 | **执行者自己的资源 ACL** | 照旧同形 404 |

1–5 不需要公钥，只能导致拒绝、不能导致放行；它们放在前面是为了不让发错地方的凭证
触发控制面查询。成员状态先于签名：已撤销节点的有效签名也不放行。

## 远端主体如何落到本地

执行者**不使用** `actor.organization_id` 作为本地组织，也不按 `subject` 找本地用户。
它构造 `Actor(id=peer-<sha256(issuer, 发行者组织, kind, subject)[:27]>, kind="peer",
organization_id=<PeerTrust.organization_id>, role="viewer")`：

- 本地组织是**批准该节点的那条成员记录**的组织；
- `peer-` 前缀 + 32 字符，永远不等于本地 32 位十六进制用户 id，
  所以远端同名用户继承不了本地同名用户的私有资产（T32）；
- 只读 viewer：看得到本组织已发布的集合与资源，看不到任何人的私有资源；
- 同一远端主体跨请求得到同一个 id，所以它自己的探测 / 受理 / 执行 / 证据集照常可读，
  另一个远端主体（哪怕同名、换了发行者）读不到。

受理时额外要求 `plan.root_coordinator_node_id == issuer_node_id`：一个节点不能以别的
协调者名义提交计划（`credential_scope_denied`）。

## 重放、吊销与缓存

- **重放**：每张凭证只能用一次。`federation_credential_nonces(jti)` 主键仲裁；
  过期行由联邦清扫循环删除。凭证在请求处理失败后也已作废，重试必须重新签发。
- **吊销**：控制面管理员 revoke 之后，接收方最迟在
  `FEDERATION_PEER_KEY_CACHE_SECONDS`（默认 5，上限 60，0 = 每次都查）内拒绝该节点的
  新请求；只缓存 approved 记录，未知 / pending / revoked 从不缓存。已经签出并在途的
  凭证最多再活 120 秒，但它仍要在接收方重新查信任记录。**不承诺撤销能收回已经传输的字节**（§8.4）。
- **出站转发**：凭证只放请求头；出站客户端 `follow_redirects=False`、`trust_env=False`，
  重定向不会把凭证带到第二个监听器（`test_federation_ssrf.py`）；即使被截获，
  audience 与请求绑定也让它在别处和别的请求上无效。

## 本节点身份统一

一个自治中心只有一个持久 `node_id`（§2.1）：控制面密钥派生的那个。语料服务与 worker
启动时向 `GET /internal/federation/identity` 绑定；`BUNDLE_NODE_ID` 只作为可选的
固定值核对，不一致即 `node_identity_mismatch`（503，所有联邦端点与出站都停），
尚未绑定成功即 `node_identity_unavailable`。ScopeManifest 的本地成员判定、Bundle 与
发布目录的 `origin_node_id`、探测 / 受理回执里的节点身份全部取自这一个绑定值。
签发响应的 `issuer_node_id` 与信任记录的 `authority_node_id` 都要与之核对。

## 共享口令档位

`FEDERATION_PEER_AUTH=shared_token_insecure` 保留旧的共享口令 + 服务凭据 + actor 头，
只给没有控制面的开发夹具：**必须同时 `ALLOW_INSECURE_DEFAULTS=true` 才能启动**，
`/readyz` 的 `federation.peer_auth` 与 `federation.degraded=shared_peer_token` 如实报出。
默认 `node_credential`。

## 已知局限

- 首发单组织：控制面只在默认组织的成员目录里查信任记录；同一节点被多个组织批准时的
  audience 组织选择没有定义。
- 控制面自己的目录对等读（`/api/v1/federation/members`、`/collections`）与 Go 出站
  目录展开仍是共享 `X-DDP-Peer-Token`，没有迁到本契约。
- 密钥轮换：node_id 由公钥派生，换钥即换身份；没有轮换协议。
