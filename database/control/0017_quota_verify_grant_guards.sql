-- 用量账非负、verify 领取租约、默认授权钉死、稳定凭证有界四组存储层守卫。
--
-- 与 0002 的关系：0002_roles.sql 的文本一个字不动 —— migrate.go 把文件字节的
-- sha256 记进 control.schema_migrations，已应用的迁移改一个字就会
-- Up 硬失败 / Check 报 Drifted。所以默认授权的显式钉死（FOR ROLE ddp）落在这
-- 个新迁移里，而不是回写 0002。0002 当年由属主 ddp 跑，裸 ALTER DEFAULT
-- PRIVILEGES 实际已是对 ddp 生效；这里再显式声明一次是幂等的加固。
--
-- 幂等：约束加 DO 块判 pg_constraint，列加 IF NOT EXISTS，授权/回填语句
-- 本身可重放。只跑一次（账本保证），但重放也不炸。

-- 用量账只 INSERT 不 UPDATE，但负数会把配额"退回来"。
-- handler 侧已拒负数，这里是数据库层的最后一道：
-- 新行 pages/requests 必须非负。NOT VALID —— 老库里可能已有脏行，
-- 约束只对新插入生效，不校验历史（否则 ALTER 直接失败，迁移跑不过去）。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'usage_ledger_nonnegative_ck') THEN
        ALTER TABLE control.usage_ledger
            ADD CONSTRAINT usage_ledger_nonnegative_ck CHECK (pages >= 0 AND requests >= 0) NOT VALID;
    END IF;
END
$$;
-- verify 领取租约。PendingVerification 用
-- UPDATE ... FOR UPDATE SKIP LOCKED RETURNING 一次只让一个副本领到同一行，
-- 租约窗内（verifyClaimLease，见 store/uploads.go）其他副本不再重复拉取全对象做 Digest。
-- verify_attempts 计数每次领取：Digest 一直失败的行计数涨到上限后由
-- FailStalledVerifications 置 failed 并释放 reserved_pages 占用，
-- 而不是永远 verifying 把配额吃光。
ALTER TABLE control.upload_sessions
    ADD COLUMN IF NOT EXISTS verify_claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verify_attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS upload_verify_claim_idx ON control.upload_sessions (updated_at)
    WHERE status = 'verifying';

-- 稳定文件凭证必须有界。FileGrantByToken 与 StableGrantFor 的读路径
-- 早已把过期当无效，但 StableGrantFor 插入的新行从不写 expires_at（NULL 永不过期）。
-- 之后的新行由 Go 侧写 expires_at = now() + 24h；这里给列一个默认值兜底
-- （绕过 Go 路径的手工 INSERT），并把历史遗留的 NULL 行回填成
-- created_at + 24h —— 超过 24h 没续的凭证就地过期，调用方重新 StableGrantFor。
ALTER TABLE control.file_grants
    ALTER COLUMN expires_at SET DEFAULT now() + interval '24 hours';
UPDATE control.file_grants
    SET expires_at = created_at + interval '24 hours'
    WHERE expires_at IS NULL;

-- 默认授权显式钉到建表角色 ddp。
-- 裸 ALTER DEFAULT PRIVILEGES 只对"执行它的那个角色"生效，换个角色跑迁移
-- 就悄悄失效；FOR ROLE ddp 与 corpus 侧 grants.sql 同式，意图一眼可见。
ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA control
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ddp_control;
GRANT USAGE ON SCHEMA public TO ddp_control;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ddp_control;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ddp_control;
ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA public
    REVOKE ALL ON TABLES FROM ddp_control;
