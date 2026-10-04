-- 对等子树快照只存目录元数据；调用链与预算绑定快照，不接触 corpus 表。
CREATE TABLE control.subtree_snapshots (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    issuer_node_id TEXT NOT NULL,
    request_binding TEXT NOT NULL,
    page_size INTEGER NOT NULL CHECK (page_size BETWEEN 1 AND 100),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL CHECK (octet_length(metadata::text) <= 8388608)
);
CREATE INDEX subtree_snapshots_owner ON control.subtree_snapshots(organization_id, issuer_node_id, created_at);
CREATE INDEX subtree_snapshots_expiry ON control.subtree_snapshots(expires_at);
CREATE TABLE control.subtree_snapshot_pages (
    snapshot_id TEXT NOT NULL REFERENCES control.subtree_snapshots(id) ON DELETE CASCADE,
    cursor TEXT NOT NULL,
    targets JSONB NOT NULL,
    next_cursor TEXT,
    PRIMARY KEY (snapshot_id, cursor)
);
