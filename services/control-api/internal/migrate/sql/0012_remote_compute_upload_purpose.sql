-- File-compute temporary upload purpose (0035 companion to corpus 0035).
--
-- 0012 follows DirectoryTrustFinish 0011. Control SQL files apply in filename
-- order with one transaction per migration (internal/migrate/migrate.go), so
-- this file must sort after 0011 and only ADD columns/constraints/indexes.
--
-- Existing permanent upload behavior is unchanged: purpose defaults to
-- 'permanent' and remote_compute_id defaults to NULL. temporary_compute
-- uploads must bind the waiting persistent compute record created via
-- POST /api/v1/remote-compute; they never become permanent corpus assets by
-- default and are cleaned by reference-safe GC after ack/TTL/cancel/failure.
ALTER TABLE control.upload_sessions
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'permanent'
        CHECK (purpose IN ('permanent', 'temporary_compute')),
    ADD COLUMN remote_compute_id TEXT;
CREATE INDEX upload_remote_compute_idx ON control.upload_sessions
    (organization_id, remote_compute_id)
    WHERE remote_compute_id IS NOT NULL;
