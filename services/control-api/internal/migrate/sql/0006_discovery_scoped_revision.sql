-- Directory revisions observable by a caller must not count hidden members' changes.
-- Keep the internal global lock/revision, but publish independent scoped view revisions
-- and per-member revision counters. Existing snapshot cursors must obtain a new scope
-- so old global counters are not exposed after this privacy upgrade.
ALTER TABLE control.node_members ADD COLUMN public_revision BIGINT NOT NULL DEFAULT 1 CHECK(public_revision >= 1);
CREATE TABLE control.node_directory_views (
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    caller_scope_hash TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK(revision >= 1),
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(organization_id,caller_scope_hash)
);
UPDATE control.member_snapshots SET expires_at=LEAST(expires_at,now());
