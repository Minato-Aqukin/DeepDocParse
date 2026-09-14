-- 文件凭证是有主体的委托，不是组织内共享的永久下载后门。
-- 旧 token 未记录主体，不能猜测所有者，兑换时默认拒绝；重新授权取得新 token。
ALTER TABLE control.file_grants ADD COLUMN subject_id TEXT NOT NULL DEFAULT '';
ALTER TABLE control.file_grants ADD COLUMN resource_id TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX file_grants_live_subject_idx
ON control.file_grants (organization_id, document_id, scope, subject_id, resource_id)
WHERE revoked = FALSE AND subject_id <> '';

