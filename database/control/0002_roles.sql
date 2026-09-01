-- 数据库角色与授权 —— **企业边界 5 的物理落点。**
--
-- 「一个数据对象只能有一个写入所有者」这条规则如果只写在文档里，
-- 迟早会有人为了图快在 Python 里直接 UPDATE 一下 control.memberships。
-- 这个文件让那件事在数据库层面**做不到**。
--
-- 幂等：可以重复执行（角色已存在时跳过创建）。

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ddp_control') THEN
        CREATE ROLE ddp_control LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ddp_corpus') THEN
        CREATE ROLE ddp_corpus LOGIN;
    END IF;
END
$$;

-- ---- Go 拥有 control ----
GRANT USAGE ON SCHEMA control TO ddp_control;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA control TO ddp_control;
ALTER DEFAULT PRIVILEGES IN SCHEMA control
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ddp_control;

-- **审计表是个例外：只准插入和查询。**
-- 审计日志不可由普通管理员修改 —— 连服务自己都不给 UPDATE/DELETE，
-- 那样"改审计"就必须走 DBA 通道并留下痕迹。
REVOKE UPDATE, DELETE ON control.audit_events FROM ddp_control;

-- ---- Python 对 control 只读，且只读两张 ----
-- 为什么需要这两张：corpus 侧要把 actor_id 渲染成用户名（上传者署名、复核人）。
-- **再多一张都要先在 docs/refactor/DATA-OWNERSHIP.md 里写清理由** ——
-- 每多一条只读依赖，就多一条把 Go 的 schema 变更传染到 Python 的路径。
GRANT USAGE  ON SCHEMA control TO ddp_corpus;
GRANT SELECT ON control.organizations, control.users TO ddp_corpus;

-- ---- Go 对 corpus 一个字都写不了 ----
-- 只给 USAGE，不给任何表权限：Go 想读语料只能走 corpus-api 的 HTTP。
-- （corpus schema 由 alembic 建，这里只管授权。schema 还不存在时跳过，
--   让两边的迁移顺序互不依赖。）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'corpus') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA corpus TO ddp_control';
    END IF;
END
$$;
