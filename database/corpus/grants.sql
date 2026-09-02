-- 语料表的授权 —— **企业边界 5 的另一半。**
--
-- `database/control/0002_roles.sql` 管的是"Go 写不了语料"；这个文件管的是
-- "Python 写得了语料、而 Go 一张表都碰不到"。两半必须都在，否则：
--
--   * 少了那一半：Go 能直接 UPDATE 语料表，单写所有者形同虚设
--   * 少了这一半：**corpus-api 连自己的表都读不了**
--
-- ## 两个真实故障，都是 2026-09-02 第一次真起全栈时抓到的
--
-- 1. alembic 用超级用户 `ddp` 跑，建出来的表归 `ddp` 所有；服务进程用的是
--    `ddp_corpus`，它对这些表一个权限都没有。表现是：迁移全部成功、
--    每个容器都 healthy、上传直传摘要校验全通过，而 `/internal/events`
--    一律 500 `permission denied for table processed_events` ——
--    **文档永远不入库，而所有健康检查都是绿的**。
--
-- 2. `0002_roles.sql` 里那段"corpus schema 存在才授权"**永远不会执行**：
--    语料表根本不在名为 corpus 的 schema 里，它们在 `public`。
--    也就是说"Go 对语料一个字都写不了"这句话，在真部署里从来没有生效过 ——
--    它只是一段读起来很像在做事的 SQL。
--
-- 所以这里对着**表真正所在的 schema**（public）授权，不再对一个
-- 不存在的 schema 做条件判断。
--
-- 幂等，每次迁移之后都跑一遍：新加的表要重新 GRANT，
-- 而 ALTER DEFAULT PRIVILEGES 只对**之后**创建的对象生效。

-- ---- Python 拥有语料 ----
GRANT USAGE ON SCHEMA public TO ddp_corpus;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ddp_corpus;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ddp_corpus;

-- 之后新建的表也自动带上（`ddp` 是建表的那个角色，所以 FOR ROLE 是它）
ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ddp_corpus;
ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ddp_corpus;

-- ---- Go 对语料一个字都写不了 ----
-- 只给 USAGE（它要能解析限定名），不给任何表权限。
-- REVOKE 写在 GRANT 之后是**故意的**：PostgreSQL 的 public schema 历史上
-- 对 PUBLIC 角色很宽松，显式撤一遍比假设默认值安全。
GRANT USAGE ON SCHEMA public TO ddp_control;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ddp_control;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ddp_control;
ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA public
    REVOKE ALL ON TABLES FROM ddp_control;
