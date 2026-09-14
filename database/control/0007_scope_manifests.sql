-- Scope manifests are frozen caller-visible directory metadata, never corpus content.
CREATE TABLE control.scope_manifests (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    caller_scope_hash TEXT NOT NULL,
    member_snapshot_id TEXT NOT NULL REFERENCES control.member_snapshots(id),
    manifest JSONB NOT NULL,
    manifest_digest TEXT NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    first_cursor TEXT NOT NULL,
    terminal_cursor TEXT NOT NULL,
    total_targets INTEGER NOT NULL CHECK (total_targets >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX scope_manifests_caller ON control.scope_manifests(organization_id,caller_scope_hash,created_at);
CREATE TABLE control.scope_target_pages (
    scope_id TEXT NOT NULL REFERENCES control.scope_manifests(id) ON DELETE CASCADE,
    cursor TEXT NOT NULL,
    next_cursor TEXT,
    targets JSONB NOT NULL,
    PRIMARY KEY(scope_id,cursor)
);
-- Persist the exact source snapshot needed for live authorization checks; a newer
-- collection catalog must not be substituted when this one expires or is revoked.
CREATE TABLE control.scope_catalog_sources (
    scope_id TEXT PRIMARY KEY REFERENCES control.scope_manifests(id) ON DELETE CASCADE,
    origin_node_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    terminal_cursor TEXT NOT NULL
);
