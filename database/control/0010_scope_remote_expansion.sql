-- Recursive directory expansion (P6): freeze child-directory references and the
-- local authorization root of every discovered remote origin.
--
-- `child_manifests` is the queryable copy of the manifest's child-directory
-- references (ddp-scope-coverage/1#ScopeManifest). The manifest JSON stays the
-- response source; this column keeps the expansion auditable per scope.
--
-- Remote targets discovered through a child directory have no local
-- node_members row. `scope_remote_sources` records the approved direct member
-- through which the origin entered the scope, so target reads re-check a real
-- local authorization root instead of treating a discovered origin as revoked
-- or inventing a registration for it.
ALTER TABLE control.scope_manifests
    ADD COLUMN child_manifests JSONB NOT NULL DEFAULT '[]'::jsonb;
UPDATE control.scope_manifests
    SET child_manifests = manifest->'child_manifests'
    WHERE jsonb_typeof(manifest->'child_manifests') = 'array';
ALTER TABLE control.scope_manifests
    ADD CONSTRAINT scope_manifests_child_manifests_array
    CHECK (jsonb_typeof(child_manifests) = 'array');

-- Peer member snapshots need their own fixed page size to reject a changed
-- limit on continuation; user snapshots keep the historical default.
ALTER TABLE control.member_snapshots
    ADD COLUMN page_size INTEGER NOT NULL DEFAULT 50
    CHECK (page_size BETWEEN 1 AND 100);

CREATE TABLE control.scope_remote_sources (
    scope_id TEXT NOT NULL REFERENCES control.scope_manifests(id) ON DELETE CASCADE,
    origin_node_id TEXT NOT NULL,
    via_node_id TEXT NOT NULL,
    PRIMARY KEY (scope_id, origin_node_id)
);
