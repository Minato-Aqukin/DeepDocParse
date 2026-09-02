# DeepDocParse 控制面配置参考

Go 控制面的全部配置项。取自 `services/control-api/internal/config/config.go`，
**本文件由脚本生成，不要手改** —— 改注释请改源码，然后重跑
`python scripts/gen_config_docs.py`。

配置来源：环境变量（Go 侧不读 `.env` 文件 —— 那是 pydantic-settings 的行为，
Go 这边由容器/systemd 注入环境变量）。

**占位密钥会拒绝启动**：`JWT_SECRET` 是 change-me 等于任何人都能给任意
user_id 伪造一个有效会话，且运行时不报任何错。一次性容器 / CI 可用
`ALLOW_INSECURE_DEFAULTS=true` 显式跳过 —— 逃生口必须显式且留痕。

共 **38** 项。

## 监听

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CONTROL_ADDR` | `string` | `":8080"` | 监听地址。容器里通常保持 :8080，对外端口由编排层映射 |

## 数据库

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CONTROL_DATABASE_URL` | `string` | `"postgres://ddp_control:ddp@127.0.0.1:15432/deepdocparse"` | control schema 的连接串。**必须用 ddp_control 角色连** —— 它对 corpus 一个字都写不了，这是"一个数据对象只能有一个写入所有者"的物理保障 （database/control/0002_roles.sql） |
| `CONTROL_DB_MAX_CONNS` | `int` | `20` | 连接池上限。**这是全站雪崩的一个入口**：每个副本都会占满它， 副本数 × MaxConns 必须小于 PG 的 max_connections，否则扩容会打挂数据库 |
| `CONTROL_DB_MIN_CONNS` | `int` | `2` | 常驻的空闲连接数。设 0 会让每个突发请求都先付一次建连的钱 |

## 会话与凭据

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `JWT_SECRET` | `string` | `placeholder` | 签发/校验用户会话的密钥。**占位值或短于 32 字节会拒绝启动**： 它是 change-me 等于任何人都能给任意 user_id 伪造一个有效会话，且运行时零报错 |
| `JWT_TTL_MINUTES` | `int` | `60*24*7` | 登录态有效期（单位：分钟） |
| `BCRYPT_COST` | `int` | `12` | bcrypt 成本。登录是低频操作，成本函数拖慢的是攻击者； 调低于 10 基本等于没有慢哈希 |
| `REGISTRATION_MODE` | `string` | `"open"` | 注册模式：open（任何人可注册）/ invite（仅 admin 添加）/ closed（只走 OIDC）。 企业部署应当是 closed 或 invite |
| `DEFAULT_MEMBER_ROLE` | `string` | `"contributor"` | 新成员的缺省角色。**第一个注册的用户无视这一项直接成为 admin** —— 否则一个全新部署会没有任何人能管理它 |

## OIDC（企业登录，可选）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `OIDC_ISSUER` | `string` | `""` | IdP 的 issuer。留空 = 不启用 OIDC，只用本地账号 |
| `OIDC_CLIENT_ID` | `string` | `""` | OIDC 客户端 ID |
| `OIDC_CLIENT_SECRET` | `string` | `""` | OIDC 客户端密钥 |
| `OIDC_REDIRECT_URL` | `string` | `""` | 回调地址，必须与 IdP 上登记的一致 |

## 上游服务

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CORPUS_URL` | `string` | `"http://127.0.0.1:8081"` | 语料 API（services/corpus-api） |
| `GATEWAY_URL` | `string` | `"http://127.0.0.1:9000"` | 模型网关（services/model-gateway） |
| `MCP_URL` | `string` | `"http://127.0.0.1:9100"` | 语料级 MCP（services/mcp） |
| `SERVICE_TOKEN` | `string` | `placeholder` | 服务间凭据。**占位值会拒绝启动**：它是内网服务面唯一的鉴权， 留着 change-me 等于 /v1/* 与语料 API 无鉴权开放。 生产优先 mTLS 或短期服务 token，这是最低限度的那一层 |

## 对象存储

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `OBJECT_ENDPOINT` | `string` | `"127.0.0.1:19000"` | 服务自己访问对象存储的地址（内网） |
| `OBJECT_PUBLIC_ENDPOINT` | `string` | `"127.0.0.1:19000"` | **浏览器可达**的地址，预签名 URL 用它签。与上面用同一个的话， 容器里签出来的 URL 里带着 `minio:9000`，浏览器一访问就是 DNS 失败 —— 而这只在真部署里才暴露 |
| `OBJECT_ACCESS_KEY` | `string` | `"minioadmin"` | 对象存储访问密钥 |
| `OBJECT_SECRET_KEY` | `string` | `"minioadmin"` | 对象存储密钥。**默认值 minioadmin 会拒绝启动** |
| `OBJECT_BUCKET` | `string` | `"deepdocparse"` | 桶名 |
| `OBJECT_SECURE` | `bool` | `false` | 走 https 则置 true |
| `PRESIGN_TTL_SECONDS` | `int` | `900` | 预签名有效期。**泄露一个签名 URL 的代价与它的 TTL 成正比**， 所以超过 2 小时会拒绝启动（单位：秒） |

## 上传

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `MAX_UPLOAD_BYTES` | `int` | `200*1024*1024` | 单文件上限（单位：字节） |
| `UPLOAD_PART_SIZE` | `int` | `16*1024*1024` | multipart 分片大小。**必须 >= 5MiB**（S3 兼容实现的硬性下限）—— 写小了会在 complete multipart 时才报错，那时文件已经传完了 |
| `UPLOAD_TTL_SECONDS` | `int` | `24*3600` | 上传会话有效期，过期未完成会被标成 expired（**不删对象**， 回收交给带宽限期的 GC）（单位：秒） |
| `ALLOWED_UPLOAD_MIME` | `list[str]` | `"application/pdf"` | 允许上传的 MIME **白名单**。空列表会拒绝启动。 白名单而不是黑名单：上传 text/html 并 inline 打开就是本站同源 XSS |

## 限速

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `REDIS_URL` | `string` | `""` | 留空则退回单进程内存计数，并在启动日志里明确警告 —— **多副本部署下那等于实际限速 = 配置值 × 副本数** |
| `DEFAULT_RATE_LIMIT_PER_MIN` | `int` | `60` | 新建 API key 的缺省限速（次/分钟）（单位：次/分钟） |
| `LOGIN_RATE_LIMIT_PER_MIN` | `int` | `10` | 登录/注册这类未鉴权端点的限速（次/分钟/IP）。 目的不是精确公平，是让暴力破解从"几分钟"变成"几个月"（单位：次/分钟） |
| `QA_RATE_PER_MIN` | `int` | `20` | 问答限速（次/分钟/actor）。它会打一次 chat 模型 + 若干次视觉核对（单位：次/分钟） |
| `KNOWLEDGE_RATE_PER_MIN` | `int` | `2` | 图谱/wiki 生成限速（次/分钟/actor）。**很贵**：一次要把几十条证据送进模型（单位：次/分钟） |
| `EXTRACT_RATE_PER_MIN` | `int` | `6` | 批量抽取限速（次/分钟/actor）。一次 = N 个字段 × (检索 + 模型调用)（单位：次/分钟） |

## 其它

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CORS_ORIGINS` | `list[str]` | `"http://localhost:5173,http://127.0.0.1:5173"` | 允许的浏览器来源。**不要用 `*`**：配合 credentials 时浏览器会直接拒绝， 而且那等于放弃同源保护 |
| `PUBLIC_BASE_URL` | `string` | `"http://127.0.0.1:8080"` | 本服务对外可达的地址，拼稳定文件 URL 用 |
| `ALLOW_INSECURE_DEFAULTS` | `bool` | `false` | 显式跳过占位密钥检查。**只给一次性容器与 CI 用** —— 逃生口必须显式且留痕 |
| `OUTBOX_INTERVAL_SECONDS` | `int` | `2` | outbox 投递间隔。调大会让"上传完到文档出现"的延迟变长（单位：秒） |

<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->
