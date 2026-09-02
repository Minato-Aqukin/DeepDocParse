# 数据所有权与跨边界流程

> 依据：`MERGE-REFACTOR-PROPOSAL.md` §7、企业化边界 5/6/7/8。
> **一个数据对象只能有一个写入所有者。** 这份文件是那条规则的清单化落地，
> 也是 CI 里 `scripts/check_data_ownership.py` 的判据来源。

## 1. 两个 schema，两个数据库角色

同一个 PostgreSQL 集群，两个 schema，两个 role。**隔离靠数据库权限，不靠自觉。**

> ⚠️ **语料表在 `public`，不在名为 `corpus` 的 schema 里。**
> 这是既有实现（alembic 从一开始就建在 public），合仓没有搬动它 ——
> 搬 30 张表的 schema 需要一次独立的迁移与一轮对拍，不在本轮范围内。
>
> 这件事本身不影响所有权规则，但它**曾经让规则整个失效**：
> `database/control/0002_roles.sql` 里的授权写成了"如果 corpus schema 存在
> 就授权"，而那个 schema 从来不存在，于是那段 SQL 永远跳过。
> 也就是说"Go 对语料一个字都写不了"这句话，在真部署里从未生效过 ——
> 它只是一段读起来很像在做事的 SQL。2026-09-02 第一次真起全栈才发现
> （FINDINGS F-20）。现在授权对着 `public` 写，见 `database/corpus/grants.sql`。

实际生效的授权（`database/control/0002_roles.sql` + `database/corpus/grants.sql`）：

```sql
CREATE SCHEMA control;   -- Go control-api 写
-- 语料表在 public（历史原因，见上面的警告）

CREATE ROLE ddp_control LOGIN;   -- 口令由 control-migrate 按环境变量设置
CREATE ROLE ddp_corpus  LOGIN;   -- CREATE ROLE 不带口令，漏设的话服务连不上库

-- Go 拥有 control，对语料一个字都写不了
GRANT USAGE, SELECT, INSERT, UPDATE, DELETE ON control.* TO ddp_control;
REVOKE UPDATE, DELETE ON control.audit_events FROM ddp_control;  -- 审计只增不改
GRANT USAGE ON SCHEMA public TO ddp_control;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ddp_control;
-- ↑ 只给 USAGE，不给任何表权限：Go 想读语料只能走 corpus-api 的 HTTP

-- Python 拥有语料，对 control 只读它必须知道的那两张
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ddp_corpus;
GRANT USAGE  ON SCHEMA control TO ddp_corpus;
GRANT SELECT ON control.organizations, control.users TO ddp_corpus;
```

**服务进程必须用受限角色连库。** compose 里 control-api 连的是
`ddp_control`，corpus-api / corpus-worker 连的是 `ddp_corpus`；
只有两个**一次性迁移容器**用属主 `ddp`（建表要 DDL、设角色口令要 CREATEROLE，
这两样长跑进程都不该有）。
用超级用户连库的话，上面这些 GRANT/REVOKE 一条都不起作用 ——
边界 5 就退化成一条静态守卫的自觉。

**两条守卫，缺一不可**：

| 守卫 | 判据 | 它管什么 |
|---|---|---|
| `scripts/check_data_ownership.py` | 静态扫源码里的表名 | 有没有人**写了**越界的 SQL |
| `scripts/check_db_boundary.sh` | 对着真库跑 23 条断言 | 越界的 SQL **会不会被数据库拒绝** |

前者防意图，后者防疏漏。F-20 正是"前者全绿而后者不存在"的那段时间里
发生的：源码干干净净，而数据库权限从来没生效过。

后者已做变异确认：给 `ddp_control` 开一次 `documents` 的写权限，当场变红。
它还带一组**反哨兵**（该给的权限必须真的给了）—— 否则"角色连不上库"
会让每一条拒绝断言都"通过"，而系统其实是坏的。

## 2. 表归属

### control schema（Go 写，Python 只读上表两张）

| 表 | 内容 | 来源 |
|---|---|---|
| `organizations` | 组织 | 新建 |
| `users` | 账号、密码哈希、OIDC subject | 旧 `users` |
| `memberships` | 用户 × 组织 × 角色 | 新建 |
| `roles` / `role_permissions` | viewer / contributor / reviewer / admin | 新建 |
| `api_keys` | sk- key、作用域、过期、撤销、最后使用 | 旧 `api_keys` + 新增作用域/过期语义 |
| `quotas` | 配额定义（组织级 / key 级） | 旧 `api_keys.quota_pages` 拆出 |
| `usage_ledger` | 计量流水 | 旧 `usage_records` |
| `audit_events` | 审计（普通管理员不可改） | 新建 |
| `upload_sessions` | 直传会话：预签名、预期大小、MIME、finalize 状态 | 新建（§9.1） |
| `file_grants` | 稳定文件 URL 的凭证 | 旧 `file_tokens` |
| `control_outbox` | 出站事件 | 新建 |

### corpus schema（Python 写，Go 只通过 corpus-api 访问）

| 表 | 备注 |
|---|---|
| `documents` / `document_uploads` | `uploaded_by` → `actor_id`（**无 FK**，跨 schema 不设约束） |
| `parse_jobs` | |
| `chunks` | 向量索引，可重建缓存 |
| `evidence` / `citations` | **唯一实现留在 Python**（风险台账：Go 重写证据规则 → 假出处） |
| `agent_turns` / `assertions` / `retrieval_candidates` / `evidence_verifications` | |
| `knowledge_entities` / `graph_edges` / `wiki_entries` / `wiki_sections` / `wiki_sentences` / `knowledge_reviews` | |
| `conversations` / `messages` | 从旧 web 层迁入 corpus（它们绑 Document，属于语料） |
| `extraction_templates` / `extraction_runs` / `extraction_items` | 同上 |
| `tasks` | **新建**：持久任务真相（§10），claim + generation + lease |
| `corpus_outbox` | 出站事件 |

## 3. 跨 schema 外键的处理（必须在同一次改动里做完）

旧代码有 **9 处** `ForeignKey("users.id")`。跨 schema 硬外键会把两个服务
的发布顺序绑死，也让「Python 不得修改组织成员」这条规则失去数据库层保障。
一律改为**无约束的 `actor_id`**：

| 位置 | 旧列 | 新列 | 处理 |
|---|---|---|---|
| `ddp_core/models.py:99` | `documents.uploaded_by` FK users.id | `documents.actor_id` | 去 FK，保留索引 |
| `ddp_core/models.py:148` | `document_uploads.user_id` FK | `document_uploads.actor_id` | 去 FK |
| `ddp_core/models.py:456` | `evidence_verifications` 验证者 FK | `.actor_id` | 去 FK |
| `ddp_core/models.py:569` | `knowledge_reviews.reviewer_id` FK | `.reviewer_actor_id` | 去 FK |
| `ddp_corpus/models.py:54` | `api_keys.user_id` | — | 整表迁去 control |
| `ddp_corpus/models.py:73` | `conversations.user_id` FK | `conversations.actor_id` | 去 FK，表留在 corpus |
| `ddp_corpus/models.py:128` | `extraction_templates.user_id` FK | `.actor_id` | 去 FK |
| `ddp_corpus/models.py:153` | `extraction_runs.user_id` FK | `.actor_id` | 去 FK |
| `ddp_corpus/models.py:234` | `usage_records.user_id` | — | 整表迁去 control（`usage_ledger`） |

**引用完整性由对账兜底，不由外键兜底**：`scripts/reconcile_actors.py`
定期比对 corpus 里出现过的 `actor_id` 与 control 的 `users.id`，
孤儿 actor 记进报告（**不删数据** —— 删语料是不可逆的，见铁律 9）。

## 4. 禁止清单（CI 机械检查）

`scripts/check_data_ownership.py` 会红在下面任何一条：

1. Go 代码里出现 `corpus.` 开头的表名写操作（`INSERT`/`UPDATE`/`DELETE`）
2. Python 代码里出现对 `control.organizations` / `memberships` / `roles` 的写操作
3. corpus 模型里出现指向 control 表的 `ForeignKey`
4. 同一字段在两侧各存一份副本而没有对账脚本
5. 一次请求里出现跨服务的分布式事务（两个连接同时 `BEGIN`）

## 5. 跨边界流程：本地事务 + Outbox

一次请求只写自己那个 schema，业务数据与事件在**同一个本地事务**里提交，
再由投递器发出。消费者按事件 ID 幂等处理。

```
Go: BEGIN
      INSERT control.upload_sessions ... (finalize)
      INSERT control.control_outbox (id, type='DocumentSubmitted', payload)
    COMMIT
        │
        └─ outbox 投递器 → POST corpus-api /internal/events
                              │
                              └─ Python: BEGIN
                                           INSERT corpus.documents  (幂等：事件 ID 唯一键)
                                           INSERT corpus.tasks      (compile)
                                         COMMIT
```

事件类型（v1）：

| 事件 | 发出方 | 消费方 | 幂等键 |
|---|---|---|---|
| `DocumentSubmitted` | control | corpus | `event_id` |
| `DocumentDeleted` | control | corpus | `event_id` |
| `OrganizationCreated` | control | corpus | `event_id` |
| `UsageRecorded` | corpus | control | `event_id` |
| `DocumentIndexed` | corpus | control | `event_id` |

> `UsageRecorded` 的方向值得说明：计量的**真相**在 control（Go 扣配额、
> 出账单），但「这次解析用了几页」只有 corpus 知道。所以 corpus 把用量
> 作为事件发给 control，而不是自己去写 `usage_ledger` —— 否则就是两个
> 写入所有者（违反企业边界 5）。

## 6. 内部调用必带的上下文

所有服务间 HTTP 调用必须携带（§7.2）：

| 头 | 说明 |
|---|---|
| `X-Request-Id` | 请求 ID |
| `traceparent` | W3C trace context |
| `X-DDP-Service` | 调用方服务身份 |
| `X-DDP-Organization` | 组织上下文（单组织部署也要带，值为默认组织） |
| `X-DDP-Actor` | user id 或 api key id |
| `X-DDP-Actor-Kind` | `user` \| `api_key` \| `service` |
| `Idempotency-Key` | 写操作必带 |

corpus-api **不自己验用户凭据**：它只信任 control-api 下发的这组头，
且只接受来自内网 / 带服务凭据的连接。这条是「service 不感知用户」那条
旧边界的直接继承 —— 变的只是从 `SERVICE_TOKEN` 单一凭据升级成
带 actor 上下文的服务身份。
