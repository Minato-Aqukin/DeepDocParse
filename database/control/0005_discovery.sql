-- Control-owned identities contain public material only. Private keys live in NODE_IDENTITY_DIR.
CREATE TABLE control.node_identity (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    node_id TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    descriptor_revision BIGINT NOT NULL DEFAULT 1,
    descriptor_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE control.node_directories (
    organization_id TEXT PRIMARY KEY REFERENCES control.organizations(id) ON DELETE CASCADE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1)
);
CREATE TABLE control.node_members (
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    descriptor JSONB NOT NULL,
    descriptor_revision BIGINT NOT NULL CHECK (descriptor_revision >= 1),
    state TEXT NOT NULL CHECK (state IN ('pending','approved','revoked')),
    visible_to_org BOOLEAN NOT NULL DEFAULT false,
    allowed_subjects TEXT[] NOT NULL DEFAULT '{}',
    revision BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id,node_id)
);
CREATE TABLE control.member_snapshots (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES control.organizations(id) ON DELETE CASCADE,
    authority_node_id TEXT NOT NULL,
    caller_scope_hash TEXT NOT NULL,
    registry_revision BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    first_cursor TEXT NOT NULL,
    terminal_cursor TEXT NOT NULL
);
CREATE TABLE control.member_snapshot_pages (
    snapshot_id TEXT NOT NULL REFERENCES control.member_snapshots(id) ON DELETE CASCADE,
    cursor TEXT NOT NULL,
    next_cursor TEXT,
    members JSONB NOT NULL,
    PRIMARY KEY(snapshot_id,cursor)
);
