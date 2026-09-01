-- control schema —— **Go control-api 是这里唯一的写入者。**
--
-- 边界规则见 docs/refactor/DATA-OWNERSHIP.md。数据库角色在
-- 0002_roles.sql 里授权：Python 侧对本 schema 只有 organizations / users
-- 两张表的 SELECT，写一个字都不行 —— 隔离靠权限，不靠自觉。
--
-- 与 corpus schema **没有任何外键**：跨 schema 硬外键会把两个服务的发布
-- 顺序绑死，也让"Python 不得修改组织成员"失去数据库层保障。
-- 引用完整性由 scripts/reconcile_actors.py 对账兜底（只报告，不删数据）。

CREATE SCHEMA IF NOT EXISTS control;

-- ---------------------------------------------------------------- 组织

CREATE TABLE control.organizations (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    -- slug 供将来多组织 SaaS 做子域名/路径路由用；单组织部署恒为 'default'
    slug         TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ
);

-- ---------------------------------------------------------------- 用户

CREATE TABLE control.users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT UNIQUE,
    -- 本地密码登录用。OIDC 用户这一列为 NULL —— **不要填一个假哈希**：
    -- 那会让"这个账号能不能用密码登录"变成一个猜谜题
    password_hash TEXT,
    -- OIDC 的 (issuer, subject)。subject 才是稳定标识，email 会变
    oidc_issuer   TEXT,
    oidc_subject  TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ,
    CONSTRAINT users_oidc_pair CHECK (
        (oidc_issuer IS NULL) = (oidc_subject IS NULL)
    ),
    -- 至少要有一种登录方式，否则这是一个谁也登不进去的账号
    CONSTRAINT users_has_credential CHECK (
        password_hash IS NOT NULL OR oidc_subject IS NOT NULL
    )
);

CREATE UNIQUE INDEX users_oidc_idx ON control.users (oidc_issuer, oidc_subject)
    WHERE oidc_subject IS NOT NULL;

-- ------------------------------------------------------------ 成员与角色

-- 角色是**有序的**：数字越大权限越多，鉴权判断因此是一次比大小，
-- 而不是一张要维护的权限矩阵。加角色 = 在中间插一个 rank。
CREATE TABLE control.roles (
    name  TEXT PRIMARY KEY,
    rank  INTEGER NOT NULL UNIQUE,
    label TEXT NOT NULL
);

INSERT INTO control.roles (name, rank, label) VALUES
    ('viewer',      10, '只读成员'),
    ('contributor', 20, '贡献者'),
    ('reviewer',    30, '复核员'),
    ('admin',       40, '管理员');

CREATE TABLE control.memberships (
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES control.users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL REFERENCES control.roles(name),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE INDEX memberships_user_idx ON control.memberships (user_id);

-- ---------------------------------------------------------------- API key

CREATE TABLE control.api_keys (
    id                 TEXT PRIMARY KEY,
    organization_id    TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    user_id            TEXT NOT NULL REFERENCES control.users(id) ON DELETE CASCADE,
    name               TEXT NOT NULL DEFAULT 'default',
    key_prefix         TEXT NOT NULL,
    -- sha256 而非 bcrypt：每个对外请求都要验一次，bcrypt 会直接压垮代理路径。
    -- key 是 32 字节随机串，不存在弱口令问题
    key_hash           TEXT NOT NULL UNIQUE,
    -- 最小权限：只做解析的集成不该能问答。空数组 = 全部禁用（默认拒绝）
    scopes             TEXT[] NOT NULL DEFAULT '{}',
    quota_pages        INTEGER,
    used_pages         INTEGER NOT NULL DEFAULT 0,
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
    expires_at         TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX api_keys_org_idx  ON control.api_keys (organization_id);
CREATE INDEX api_keys_user_idx ON control.api_keys (user_id);

-- ------------------------------------------------------------ 配额与计量

CREATE TABLE control.quotas (
    organization_id TEXT PRIMARY KEY REFERENCES control.organizations(id) ON DELETE CASCADE,
    pages_limit     INTEGER,              -- NULL = 不限
    period_days     INTEGER NOT NULL DEFAULT 30,
    period_start    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 计量流水。**只 INSERT，不 UPDATE** —— 账要能重算。
-- 用量的真相在这里，而"这次解析用了几页"只有 corpus 知道，
-- 所以它是通过 UsageRecorded 事件送过来的，不是 Python 直接写这张表。
CREATE TABLE control.usage_ledger (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    actor_id        TEXT,
    actor_kind      TEXT NOT NULL,
    api_key_id      TEXT REFERENCES control.api_keys(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    pages           INTEGER NOT NULL DEFAULT 0,
    requests        INTEGER NOT NULL DEFAULT 1,
    -- 幂等键：同一个事件重投不得记两笔账。这是 outbox 消费者的命门
    event_id        TEXT UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX usage_org_created_idx ON control.usage_ledger (organization_id, created_at DESC);
CREATE INDEX usage_actor_idx       ON control.usage_ledger (actor_id, created_at DESC);

-- ---------------------------------------------------------------- 审计

-- **不可由普通管理员修改**：没有 UPDATE/DELETE 接口，
-- 0002_roles.sql 只给 ddp_control 授 INSERT + SELECT。
CREATE TABLE control.audit_events (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id        TEXT,
    actor_kind      TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    request_id      TEXT,
    -- **绝不放**：原文全文、JWT、API key、SERVICE_TOKEN、预签名 URL 的查询串
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_org_at_idx ON control.audit_events (organization_id, at DESC);
CREATE INDEX audit_action_idx ON control.audit_events (action, at DESC);

-- ------------------------------------------------------------ 上传与文件

CREATE TABLE control.upload_sessions (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    actor_id        TEXT NOT NULL,
    actor_kind      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'created',
    object_key      TEXT NOT NULL,
    upload_id       TEXT,                 -- 对象存储的 multipart upload id
    filename        TEXT NOT NULL,
    mime            TEXT NOT NULL,
    declared_size   BIGINT NOT NULL,
    actual_size     BIGINT,
    declared_sha256 TEXT,
    -- 服务端自己流式算出来的。**与 declared 不一致就作废整个会话** ——
    -- 信客户端声明的哈希等于没有校验
    verified_sha256 TEXT,
    engine          TEXT,
    options         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    -- finalize 必须幂等：重试不得创建两份任务
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX upload_idem_idx ON control.upload_sessions (organization_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX upload_status_idx ON control.upload_sessions (status, expires_at);

-- 稳定文件 URL 的凭证。**路径必须永远稳定** —— doc_hash 在没有 doc_id 时
-- 回退成 sha256(file_url)，URL 一变幂等与向量索引全失效（ADR #11/#12）。
CREATE TABLE control.file_grants (
    token           TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    document_id     TEXT NOT NULL,        -- corpus 侧的 id，**不设外键**（跨 schema）
    object_key      TEXT NOT NULL,
    mime            TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'source',
    expires_at      TIMESTAMPTZ,
    revoked         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX file_grants_doc_idx ON control.file_grants (document_id);

-- ---------------------------------------------------------------- Outbox

-- 本地事务 + Outbox：业务数据与事件在**同一个事务**里提交，再由投递器发送。
-- 消费者按 id 幂等处理。没有它就只能寄望于跨服务分布式事务，
-- 而那在一次 HTTP 请求里是做不对的。
CREATE TABLE control.control_outbox (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    type            TEXT NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 投递状态。失败要能看见次数与原因，不能只是"还没成功"
    delivered_at    TIMESTAMPTZ,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_pending_idx ON control.control_outbox (next_attempt_at)
    WHERE delivered_at IS NULL;

-- ------------------------------------------------------------ 迁移账本

CREATE TABLE control.schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum   TEXT NOT NULL
);
