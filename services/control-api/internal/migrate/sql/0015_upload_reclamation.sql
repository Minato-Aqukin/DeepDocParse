-- Keep terminal upload identity and durable cleanup failures independently of status.
ALTER TABLE control.upload_sessions
    ADD COLUMN reclaimed_at TIMESTAMPTZ,
    ADD COLUMN reclaim_error TEXT,
    ADD COLUMN reclaim_attempted_at TIMESTAMPTZ;
CREATE INDEX upload_reclamation_idx ON control.upload_sessions (updated_at)
    WHERE reclaimed_at IS NULL AND status IN ('failed', 'expired', 'ready');
