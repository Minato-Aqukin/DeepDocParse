-- Upload identity must exist before a non-transactional S3 allocation starts.
-- A lost S3 reply is UNKNOWN, never permission to allocate again.
ALTER TABLE control.upload_sessions
    ADD COLUMN create_idempotency_key TEXT,
    ADD COLUMN request_digest TEXT,
    ADD COLUMN allocation_state TEXT NOT NULL DEFAULT 'ready'
        CHECK (allocation_state IN ('pending','allocating','ready','unknown')),
    ADD COLUMN part_size BIGINT,
    ADD COLUMN reserved_pages INTEGER NOT NULL DEFAULT 0 CHECK (reserved_pages >= 0),
    ADD COLUMN finalize_digest TEXT;
CREATE UNIQUE INDEX upload_create_actor_idem_idx ON control.upload_sessions
    (organization_id, actor_kind, actor_id, create_idempotency_key)
    WHERE create_idempotency_key IS NOT NULL;
-- Finalize is scoped to the initiating actor, never another actor's key.
DROP INDEX control.upload_idem_idx;
CREATE UNIQUE INDEX upload_idem_idx ON control.upload_sessions
    (organization_id, actor_kind, actor_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
